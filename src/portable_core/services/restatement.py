"""Config rows dated into history: refused, or restated on request. ADR 0025.

A tax rate schedule and a return policy are effective-dated so that a new row
cannot quietly rewrite what was already reported (ADR 0010). A row dated on or
before something already derived would do exactly that, and until this module
it did half of it: the row was stored and nothing derived from it changed.
`pt tax` went on reporting the tax estimated at the old rate, and only
`pt validate` noticed.

So a row that would change a stored figure is refused, and the refusal says
which figures and by how much. With ``restate=True`` the row is recorded and
the figures restated in the same transaction:

* **A tax rate schedule** is read by replay when it estimates the tax on a
  disposition, so the ledger is replayed (``ReplayEngine.rebuild``).
* **A return policy** is read only when a snapshot classifies a large flow,
  and a snapshot depends on the prices it recorded, which replay does not
  read (ADR 0024). Each affected snapshot is derived again with its recorded
  prices held fixed, so no valuation history is lost.

The caller holds the transaction. A refusal raises, and the transaction
rolling back takes the stored row, and any rebuild, with it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from portable_core.decimals import money_context, to_text
from portable_core.domain.models import RealizedGain, ReturnPolicy, TaxRateSchedule
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_RESTATES_HISTORY
from portable_core.persistence.repositories import Repositories
from portable_core.services import snapshots
from portable_core.services.replay import ReplayEngine

__all__ = ["Recorded", "record_policy", "record_rate_schedule"]

ZERO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class Recorded:
    """The row stored, and one sentence per restatement it made."""

    row_id: int
    restated: tuple[str, ...] = ()


#: A realized gain as a report reads it: no surrogate ids, which a rebuild
#: reassigns, and no ``rate_id``, which names the schedule rather than a figure.
_GainFigures = tuple[
    date, int, str, Decimal, Decimal, Decimal, bool, object, object, object, object
]


def record_rate_schedule(
    repos: Repositories, schedule: TaxRateSchedule, *, restate: bool
) -> Recorded:
    """Store *schedule*, restating the realized gains it changes when told to.

    Raises:
        ValidationError: when the account already has a schedule effective on
            that date, or (``PT-E-RESTATES-HISTORY``) when the schedule changes
            the estimated tax on a gain already recorded and *restate* is
            False.
    """
    account = repos.accounts.get(schedule.account_id)
    name = account.name if account is not None else str(schedule.account_id)
    on = schedule.effective_from.isoformat()
    if any(
        existing.effective_from == schedule.effective_from
        for existing in repos.accounts.rate_schedules(schedule.account_id)
    ):
        raise ValidationError(
            f"{name} already has a tax rate schedule effective {on}",
            remedy=(
                "Schedules are effective-dated and are not edited. A schedule for "
                "a later date replaces this one from that date on."
            ),
            account=name,
            effective_from=on,
        )

    before = _gains(repos, schedule.account_id)
    rate_id = repos.accounts.add_rate_schedule(schedule)
    if not any(
        txn.trade_date >= schedule.effective_from
        for txn in repos.transactions.in_ledger_order(account_id=schedule.account_id)
    ):
        return Recorded(rate_id)

    replayed = ReplayEngine(repos).rebuild()
    after = _gains(repos, schedule.account_id)
    gone = Counter(before) - Counter(after)
    new = Counter(after) - Counter(before)
    if not gone and not new:
        return Recorded(rate_id)

    with money_context():
        was = sum((_tax(g) for g in gone.elements()), ZERO)
        now = sum((_tax(g) for g in new.elements()), ZERO)
    dates = sorted(g[0] for g in new.elements()) or sorted(g[0] for g in gone.elements())
    span = _span(dates[0], dates[-1])
    count = max(sum(gone.values()), sum(new.values()))
    noun = "realized gain" if count == 1 else "realized gains"
    change = (
        f"the estimated tax on {count} {noun} in {name} dated {span} "
        f"({to_text(was)} becomes {to_text(now)})"
    )
    if not restate:
        raise ValidationError(
            f"a tax rate schedule effective {on} changes {change}",
            code=E_RESTATES_HISTORY,
            remedy=(
                "Rates are effective-dated so that history is not restated by "
                "accident. If this corrects the rate in force then, pass --restate "
                "to record it and restate them."
            ),
            account=name,
            effective_from=on,
            gains=count,
            estimated_tax_was=to_text(was),
            estimated_tax_now=to_text(now),
        )
    return Recorded(rate_id, (f"restated {change}", *replayed.warnings))


def record_policy(repos: Repositories, policy: ReturnPolicy, *, restate: bool) -> Recorded:
    """Store *policy*, restating the snapshots it changes when told to.

    A policy governs from its date until the next one, so only snapshots in
    that window can change, and only their large-flow flags: nothing else in
    a snapshot reads the policy.

    Raises:
        ValidationError: when a policy is already effective on that date, or
            (``PT-E-RESTATES-HISTORY``) when the policy changes a stored
            snapshot and *restate* is False.
    """
    on = policy.effective_from.isoformat()
    existing = repos.policies.all()
    if any(p.effective_from == policy.effective_from for p in existing):
        raise ValidationError(
            f"a return policy is already effective {on}",
            remedy=(
                "Policies are effective-dated and are not edited. A policy for a "
                "later date replaces this one from that date on."
            ),
            effective_from=on,
        )

    policy_id = repos.policies.add(policy)
    later = [p.effective_from for p in existing if p.effective_from > policy.effective_from]
    changed = snapshots.rederived_differently(
        repos, policy.effective_from, min(later) if later else None
    )
    if not changed:
        return Recorded(policy_id)

    names = {account.account_id: account.name for account in repos.accounts.all()}
    accounts = sorted({str(names.get(s.account_id)) for s in changed})
    span = _span(min(s.snapshot_date for s in changed), max(s.snapshot_date for s in changed))
    noun = "valuation snapshot" if len(changed) == 1 else "valuation snapshots"
    change = (
        f"the large-flow flags of {len(changed)} {noun} of {', '.join(accounts)} dated {span}"
    )
    if not restate:
        raise ValidationError(
            f"a return policy effective {on} changes {change}",
            code=E_RESTATES_HISTORY,
            remedy=(
                "Policies are effective-dated so that history is not restated by "
                "accident (PORT-GIPS-B03). If this is the policy that applied then, "
                "pass --restate to record it and restate them; they keep the prices "
                "they recorded."
            ),
            effective_from=on,
            snapshots=len(changed),
        )
    for snapshot in changed:
        repos.valuations.save_snapshot(snapshot)
    return Recorded(policy_id, (f"restated {change}, keeping the prices they recorded",))


def _gains(repos: Repositories, account_id: int) -> list[_GainFigures]:
    return [_figures(gain) for gain in repos.lots.realized_gains(account_id=account_id)]


def _figures(gain: RealizedGain) -> _GainFigures:
    return (
        gain.disposition_date,
        gain.instrument_id,
        str(gain.holding_period),
        gain.proceeds,
        gain.cost_basis,
        gain.gain,
        gain.is_taxable,
        gain.federal_rate,
        gain.state_rate,
        gain.niit_rate,
        gain.estimated_tax,
    )


def _tax(figures: _GainFigures) -> Decimal:
    tax = figures[-1]
    return tax if isinstance(tax, Decimal) else ZERO


def _span(first: date, last: date) -> str:
    return first.isoformat() if first == last else f"{first.isoformat()} to {last.isoformat()}"
