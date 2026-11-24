"""The time-weighted return, from stored valuation snapshots. ADR 0040.

`pert twr` is a thin layer over :func:`time_weighted_return`. Everything that
decides a number is here, cited to the requirement it serves:

* **Input is stored snapshots, checked** (§2). Nothing is priced and nothing
  is written. A day with no snapshot is refused rather than interpolated
  (``PORT-GIPS-A03``), and each snapshot used is re-derived first (ADR 0024).
* **One formula** (§3): Modified Dietz in calendar days, an inflow counted at
  the start of its day and an outflow at the end. Over one day it is the
  exact daily return; over more it is the gap fallback ``PORT-GIPS-B06``
  permits. The method is recorded per sub-period (``PORT-GIPS-B05``).
* **Levels** (§5): an account's flows at account level; the portfolio's,
  summed over accounts, at portfolio level, where a transfer between the
  owner's accounts is no flow at all (``PORT-GIPS-B02``).
* **Bases** (§8): each fee a basis does not deduct is undone as an outflow at
  the end of its day (``PORT-GIPS-D01``). Margin interest is never undone.
* **Linking** (§9): geometric, in :data:`RETURN_CONTEXT` (``PORT-GIPS-B04``).
* **One walk, many windows** (ADR 0045 §1). :func:`walk` reads and checks a
  level's days once; :func:`window` gives the return over any stretch of
  them. :func:`time_weighted_return` is a walk over one window, so a window
  of a longer walk and `pert twr` over the same dates are one code path, and
  agree value for value and refusal for refusal.
* **Estimates only where measurement refuses** (ADR 0044). Under an estimate
  limit, a day the measured path cannot use, and the bridge needs, is
  valued with carried prices (:mod:`portable_core.services.estimates`). Its
  sub-periods are marked, and so is any result they are linked into.
"""

from __future__ import annotations

import calendar
import decimal
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from portable_core.domain.enums import FeeClass, FlowLevel, TransactionType
from portable_core.domain.models import Account, Transaction, ValuationSnapshot
from portable_core.domain.periods import TrackRecord
from portable_core.domain.returns import (
    BASES,
    Basis,
    BasisReturn,
    CarriedHolding,
    Disclosures,
    Estimate,
    EstimatedDay,
    ReturnMethod,
    SubPeriod,
    TimedFlow,
    TimeWeightedReturn,
)
from portable_core.errors import (
    DataUnavailableError,
    GipsRefusalError,
    PortableError,
    ValidationError,
)
from portable_core.errors.kinds import (
    E_ESTIMATE_LIMIT,
    E_FEE_EMBEDDED_IN_CASH,
    E_GIPS_NO_FLOW_POLICY,
    E_GIPS_VALUATION_GAP,
    E_PRICE_STALE,
    E_RETURN_BLOCKED,
    E_RETURN_BREAK,
    E_RETURN_NOTHING_HELD,
    E_RETURN_UNDEFINED,
    E_RETURN_UNPRICED_TRADE,
    E_SNAPSHOT_MISSING,
    E_SNAPSHOT_STALE,
)
from portable_core.persistence.repositories import Repositories
from portable_core.services import price_acceptance, snapshots
from portable_core.services.estimates import PERT_STALENESS_DAYS, DayEstimator
from portable_core.services.price_acceptance import PriceLine
from portable_core.services.replay import effective_ledger
from portable_core.services.snapshots import SnapshotProblem
from portable_core.services.valuation import NO_MARKET_PRICE

__all__ = [
    "RETURN_CONTEXT",
    "Walk",
    "dietz",
    "ending_values",
    "fee_addback",
    "link",
    "time_weighted_return",
    "track_records",
    "walk",
    "window",
]

#: Every return is computed in this context, never the thread's (ADR 0040 §9).
#: decimal128's precision, half-even, and every signal that would make a
#: return quietly wrong trapped.
RETURN_CONTEXT: Final[decimal.Context] = decimal.Context(
    prec=34,
    rounding=decimal.ROUND_HALF_EVEN,
    traps=[decimal.InvalidOperation, decimal.DivisionByZero, decimal.Overflow],
)

_ONE: Final = Decimal(1)
_ZERO: Final = Decimal(0)
_DAY: Final = timedelta(days=1)


#: What each basis undoes (PORT-GIPS-D01, ADR 0040 §8). Transaction costs and
#: embedded fund fees are deducted at every basis and undone by none.
_UNDONE: Final[dict[Basis, frozenset[FeeClass]]] = {
    Basis.NET_OF_ALL_COSTS: frozenset(),
    Basis.NET_OF_FEES: frozenset({FeeClass.OTHER_ADMIN}),
    Basis.NET_OF_EXTERNAL_COSTS_ONLY: frozenset(
        {FeeClass.OTHER_ADMIN, FeeClass.INTERNAL_MGMT_COST}
    ),
    Basis.GROSS_OF_FEES: frozenset(
        {FeeClass.OTHER_ADMIN, FeeClass.INTERNAL_MGMT_COST, FeeClass.EXTERNAL_MGMT_FEE}
    ),
}

_TRADES: Final = frozenset(
    {
        TransactionType.BUY,
        TransactionType.SELL,
        TransactionType.SELL_SHORT,
        TransactionType.BUY_TO_COVER,
    }
)


class _Undefined(Exception):
    """A sub-period whose denominator is not positive."""


# ── the arithmetic ───────────────────────────────────────────────────────────


def dietz(
    beginning: Decimal,
    ending: Decimal,
    flows: Iterable[tuple[date, Decimal]],
    *,
    after: date,
    through: date,
) -> Decimal:
    """The return from the end of *after* to the end of *through*. ADR 0040 §3.

    ::

               V_E - V_B - sum(CF)                 D - t
        r  =  ---------------------- ,     w  =  -------
               V_B + sum(w * CF)                    D

    in calendar days, *D* = *through* - *after*. A flow on day *d* is taken
    at the start of the day if it is an inflow (``t = d - after - 1``) and at
    the end if it is an outflow (``t = d - after``). With *D* = 1 that is the
    exact daily return; with more it is Modified Dietz, as the archived 2011
    Guidance Statement on Calculation Methodology writes it.

    Computed multiplied through by *D*, so that the final division is the
    only rounding: a weight such as 20/30 is never rounded before it is used,
    and inputs in whole cents give the exact quotient, correctly rounded.

    Raises:
        _Undefined: the denominator is zero or negative.
    """
    span = (through - after).days
    with decimal.localcontext(RETURN_CONTEXT):
        days = Decimal(span)
        total = _ZERO
        weighted = _ZERO
        for on, amount in flows:
            elapsed = (on - after).days - (1 if amount > 0 else 0)
            total += amount
            weighted += (days - Decimal(elapsed)) * amount
        denominator = beginning * days + weighted
        if denominator <= 0:
            raise _Undefined
        return (ending - beginning - total) * days / denominator


def link(returns: Iterable[Decimal]) -> Decimal:
    """Geometric linking: ``prod(1 + r) - 1``. ``PORT-GIPS-B04``. Never a sum."""
    with decimal.localcontext(RETURN_CONTEXT):
        growth = _ONE
        for value in returns:
            growth *= _ONE + value
        return growth - _ONE


def fee_addback(txn: Transaction, basis: Basis) -> Decimal:
    """What *basis* undoes of *txn*'s fees and commissions. ADR 0040 §8.

    The row's class decides, wherever the fee sits -- on a fee row or on a
    trade. Margin interest is a financing cost, deducted at every basis
    (PORT-GIPS-D01, D04), and its rows carry ``internal_mgmt_cost`` from
    `pt cash margin-interest`; that class is ignored here, on purpose, rather
    than undoing it at gross. The ledger is append-only, so the rows keep it.

    Raises:
        GipsRefusalError: a cash fee classed ``embedded_fund_fee``. That fee
            is inside a fund's NAV and is never paid in cash; deducting a
            cash row of it as well is a double deduction (PORT-GIPS-D05).
    """
    if txn.txn_type is TransactionType.MARGIN_INTEREST:
        return _ZERO
    amount = txn.fees + txn.commissions
    if amount == 0 or txn.fee_class is None:
        return _ZERO
    if txn.fee_class is FeeClass.EMBEDDED_FUND_FEE:
        raise GipsRefusalError(
            f"txn {txn.txn_id} on {txn.trade_date.isoformat()} is a cash fee classed "
            "embedded_fund_fee",
            requirement="PORT-GIPS-D05",
            code=E_FEE_EMBEDDED_IN_CASH,
            remedy=(
                "A fund's expenses are inside its NAV and already in its price. A cash "
                "row of them deducts them twice. Reverse the row, and enter the fee "
                "again under the class it is."
            ),
            txn_id=txn.txn_id,
        )
    return amount if txn.fee_class in _UNDONE[basis] else _ZERO


# ── the return ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Day:
    """One valuation day at the level of the return."""

    on: date
    value: Decimal
    usable: bool
    flows: tuple[TimedFlow, ...]
    #: Per basis, the fees undone on this day.
    undone: dict[Basis, Decimal]
    #: Every snapshot the day needs is stored and still holds (ADR 0024). A
    #: day that is not may read as holding nothing when it did not.
    reliable: bool = True
    #: Every snapshot the day needs is stored and complete.
    complete: bool = True
    #: The age of the oldest price the day's snapshots recorded.
    oldest_price_days: int = 0
    #: Valued with carried prices, under an estimate limit (ADR 0044 §2).
    estimated: bool = False

    @property
    def carried_only(self) -> bool:
        """Unusable only because a recorded price is older than `pert` measures with."""
        return self.complete and self.oldest_price_days > PERT_STALENESS_DAYS


@dataclass(frozen=True, slots=True)
class _Piece:
    """The stretch between two consecutive usable days, and what it gives.

    One of three things: a sub-period; nothing, when the level held nothing
    and nothing moved (ADR 0040 §4); or the refusal that a window covering
    the stretch raises.
    """

    after: date
    through: date
    subperiod: SubPeriod | None
    refusal: Exception | None
    #: A day it reads, either end included, has a snapshot missing or stale.
    uncertain: bool = False

    @property
    def held_nothing(self) -> bool:
        """Nothing held and nothing moving: a gap in the track record (ADR 0045 §4).

        Never on a stretch read from a missing or stale snapshot. Its zero may
        be the absence of a value rather than a value of nothing, and a break
        there would clip every row to a record that did not begin. A window
        over the stretch is refused for the snapshot before it is cut.
        """
        return self.subperiod is None and self.refusal is None and not self.uncertain


@dataclass(frozen=True, slots=True)
class _Linked:
    """What a window measures, once days with nothing held are set aside."""

    subperiods: list[SubPeriod]
    start: date
    end: date
    before: tuple[date, date] | None
    after: tuple[date, date] | None
    estimate: Estimate | None


@dataclass(frozen=True, slots=True)
class Walk:
    """A level's days, from the eve of its first day to *through*, read and checked once.

    ADR 0045 §1. A walk raises only what would refuse every window it could
    serve. Anything else it records against the account, the day, the row or
    the stretch it belongs to. :func:`window` raises it for a window that
    covers it, in the order `pert twr` has always checked. So a window of a
    long walk is, value for value and refusal for refusal, the return of a
    walk over that window alone.
    """

    repos: Repositories
    level: FlowLevel
    account: Account | None
    names: dict[int, str]
    #: Each account begun by *through*, with the day it began.
    begun: dict[int, date]
    #: The eve of the first day a window may start on.
    anchor: date
    through: date
    stored: dict[tuple[int, date], ValuationSnapshot]
    #: ADR 0024's checks on every stored snapshot read, in reporting order.
    problems: tuple[SnapshotProblem, ...]
    #: ADR 0031: per account name, the line its returns are blocked from.
    blocked: dict[str, PriceLine]
    rows: tuple[Transaction, ...]
    #: Rows whose fee no basis can treat (PORT-GIPS-D05), by txn id.
    fee_refusals: dict[int, GipsRefusalError]
    days: tuple[_Day, ...]
    pieces: tuple[_Piece, ...]
    #: The configured `staleness_tolerance_days`, named in a refusal it explains.
    configured_staleness: int
    #: Values a day with carried prices when a window under an estimate limit
    #: asks, and keeps the answer (ADR 0044 §2).
    estimator: DayEstimator


def time_weighted_return(
    repos: Repositories,
    *,
    end: date,
    start: date | None = None,
    account: Account | None = None,
    staleness_days: int,
    estimate: Decimal | None = None,
) -> TimeWeightedReturn:
    """The cumulative TWR of *account*, or of the portfolio, through *end*.

    *start* defaults to inception, and a start before inception moves to it;
    the result carries both. Every refusal is a place where a plausible
    number would otherwise have been printed (ADR 0040 §2, §4, §6, §7, §12).

    A walk over the period alone, and its one window (ADR 0045 §1). The
    checks that need no walk are made first, so a refusal that needs none
    costs none.

    *staleness_days* is the configured `staleness_tolerance_days`. `pert`
    measures only with prices at most :data:`PERT_STALENESS_DAYS` old,
    whatever it says, and names it in a refusal caused by the difference
    (ADR 0044 §1).

    *estimate* is the largest share of a day's value that may be carried, as
    a fraction. None measures only. See :func:`window`.
    """
    accounts = [account] if account is not None else repos.accounts.all()
    _, first_day, _ = _open(repos, _begun(repos, accounts, end), start, end)
    walked = walk(
        repos, first=first_day, through=end, account=account, staleness_days=staleness_days
    )
    return window(walked, start=start, end=end, estimate=estimate)


def walk(
    repos: Repositories,
    *,
    first: date | None = None,
    through: date,
    account: Account | None = None,
    staleness_days: int,
) -> Walk:
    """Read and check *account*'s days, or the portfolio's, from *first* to *through*.

    *first* is the earliest day any window of the walk will start on, and
    inception when omitted. A start before inception moves to inception, as
    a window's does. Every window must lie inside the walk (:func:`window`).
    *staleness_days* is the configured tolerance, as for
    :func:`time_weighted_return`.
    """
    level = FlowLevel.ACCOUNT if account is not None else FlowLevel.PORTFOLIO
    accounts = [account] if account is not None else repos.accounts.all()
    names = {target.account_id: target.name for target in accounts}
    begun = _begun(repos, accounts, through)
    if not begun:
        raise _nothing_to_measure(through)
    inception = min(begun.values())
    first_day = max(first, inception) if first is not None else inception
    if first_day > through:
        raise ValueError(f"a walk from {first_day} to {through} covers no day")
    anchor = first_day - _DAY

    stored = repos.valuations.series(sorted(begun), anchor, through)
    rows = tuple(
        txn
        for txn in effective_ledger(repos.transactions.in_ledger_order())
        if first_day <= txn.trade_date <= through and txn.account_id in begun
    )
    problems = tuple(snapshots.problems_between(repos, set(begun), anchor, through))
    days, fee_refusals = _days(
        stored, begun, rows, level, anchor, through, {problem.on for problem in problems}
    )
    return Walk(
        repos=repos,
        level=level,
        account=account,
        names=names,
        begun=begun,
        anchor=anchor,
        through=through,
        stored=stored,
        problems=problems,
        blocked=price_acceptance.blocked_from(
            # ADR 0044 §1: the tie check in `pert` reads the same fixed age.
            price_acceptance.check_recorded(repos, staleness_days=PERT_STALENESS_DAYS)
        ),
        rows=rows,
        fee_refusals=fee_refusals,
        days=days,
        pieces=_pieces(repos, days, staleness_days),
        configured_staleness=staleness_days,
        estimator=DayEstimator(repos, names=names, begun=begun, stored=stored),
    )


def window(
    walked: Walk, *, start: date | None, end: date, estimate: Decimal | None = None
) -> TimeWeightedReturn:
    """The return of *walked*'s level from *start* through *end*. ADR 0045 §1.

    It is exactly what a walk over this window alone would give: the same
    checks, in the order `pert twr` has always made them, on the window's own
    accounts and days.

    Under *estimate*, the largest share of a day's value that may be carried
    (ADR 0044 §4), a day the measured path refuses for want of a price is
    valued with carried prices, where the window needs it (:func:`_linked`).
    Every check before the days is made as without it: an estimate supplies
    a price and nothing else. When nothing needs estimating, the result is
    the measured one, unmarked.

    Raises:
        ValueError: the window does not lie inside the walk. That is a
            mistake in the caller, not in the portfolio.
    """
    if end > walked.through:
        raise ValueError(f"the window ends {end}, after the walk's {walked.through}")
    repos = walked.repos
    # An account begun after the window ends takes no part in it.
    begun = {account_id: first for account_id, first in walked.begun.items() if first <= end}
    requested, first_day, inception = _open(repos, begun, start, end)
    anchor = first_day - _DAY
    if anchor < walked.anchor:
        raise ValueError(f"the window starts {first_day}, before the walk's first day")

    stored = {
        key: snapshot
        for key, snapshot in walked.stored.items()
        if key[0] in begun and anchor <= key[1] <= end
    }
    _require_snapshots(stored, begun, walked.names, anchor, end)
    _require_checked(
        [
            problem.text
            for problem in walked.problems
            if problem.account_id in begun and anchor <= problem.on <= end
        ]
    )
    _require_unblocked(walked.blocked, begun, walked.names, end)

    rows = [
        txn
        for txn in walked.rows
        if first_day <= txn.trade_date <= end and txn.account_id in begun
    ]
    # Fees are charged to the row's own account; a transfer's other side pays
    # none of them.
    _refuse_unpriced_trades(repos, rows)
    for txn in rows:
        refusal = walked.fee_refusals.get(txn.txn_id)
        if refusal is not None:
            raise refusal

    linked = _linked(walked, anchor, end, estimate)
    cumulative = tuple(
        BasisReturn(basis, link(sub.return_for(basis) for sub in linked.subperiods))
        for basis in BASES
    )
    measured = [txn for txn in rows if linked.start <= txn.trade_date <= linked.end]
    return TimeWeightedReturn(
        level=walked.level,
        account=walked.account.name if walked.account is not None else None,
        requested_start=requested,
        start=linked.start,
        end=linked.end,
        requested_end=end,
        inception=inception,
        cumulative=cumulative,
        subperiods=tuple(linked.subperiods),
        nothing_held_before=linked.before,
        nothing_held_after=linked.after,
        disclosures=_disclosures(
            repos,
            stored,
            measured,
            linked.subperiods,
            linked.start,
            linked.end,
            ending=linked.subperiods[-1].ending_value if linked.estimate else None,
        ),
        estimate=linked.estimate,
    )


def ending_values(walked: Walk, result: TimeWeightedReturn) -> dict[int, Decimal]:
    """Each account's value on *result*'s last day, as the return valued it.

    The stored value, or, on a day the result estimated, the value with its
    carried prices (ADR 0044 §2).
    """
    on = result.end
    if result.estimate is not None and any(day.on == on for day in result.estimate.days):
        return {account_id: day.value for account_id, day in walked.estimator.on(on).items()}
    return {
        account_id: snapshot.ending_market_value
        for (account_id, day), snapshot in walked.stored.items()
        if day == on and walked.begun[account_id] <= on
    }


def track_records(walked: Walk) -> tuple[TrackRecord, ...]:
    """The walk's track records, in order. ADR 0045 §4, PORT-GIPS-H06.

    A record begins on the first day after a stretch with nothing held, or at
    the first piece. It ends on the last day before the next such stretch,
    the day its value went to nothing. The termination of a record is decided
    by the days up to the next stretch alone, so walking further, past a
    restart, never moves it (PORT-GIPS-A04).
    """
    records: list[TrackRecord] = []
    inception: date | None = None
    for piece in walked.pieces:
        if piece.held_nothing:
            if inception is not None:
                records.append(TrackRecord(inception, piece.after))
                inception = None
        elif inception is None:
            inception = piece.after + _DAY
    if inception is not None:
        records.append(TrackRecord(inception, None))
    return tuple(records)


def _begun(repos: Repositories, accounts: Sequence[Account], end: date) -> dict[int, date]:
    """Each account with a ledger row on or before *end*, and its first day."""
    begun: dict[int, date] = {}
    for target in accounts:
        # Either side of a row: an account funded only by a transfer has no
        # row of its own, and leaving it out would lose the money from the
        # portfolio without a flow.
        first = repos.transactions.first_activity_date(target.account_id)
        if first is not None and first <= end:
            begun[target.account_id] = first
    return begun


def _open(
    repos: Repositories, begun: dict[int, date], start: date | None, end: date
) -> tuple[date, date, date]:
    """The checks a window needs before any day: (requested start, first day, inception)."""
    if not begun:
        raise _nothing_to_measure(end)
    inception = min(begun.values())
    requested = start if start is not None else inception
    first_day = max(requested, inception)
    if first_day > end:
        raise ValidationError(
            f"--from {first_day.isoformat()} is after --to {end.isoformat()}",
            remedy="Check the order of the dates.",
        )
    # First: a policy in force on the first day stays in force (some policy
    # does) on every later one, and without one no flow can be called large.
    if repos.policies.in_force(first_day) is None:
        raise GipsRefusalError(
            f"no return policy is in force on {first_day.isoformat()}",
            requirement="PORT-GIPS-B03",
            code=E_GIPS_NO_FLOW_POLICY,
            remedy=(
                "Define the large-cash-flow threshold with `pt policy set`. GIPS defines "
                "the term and supplies no number, so a missing one is an error, not a "
                "zero -- even where daily valuation makes it informational."
            ),
        )
    return requested, first_day, inception


def _nothing_to_measure(end: date) -> ValidationError:
    return ValidationError(
        f"nothing to measure: no ledger rows on or before {end.isoformat()}",
        remedy="A return needs capital in the account. Check --to and --account.",
    )


# ── the checks ───────────────────────────────────────────────────────────────


def _require_snapshots(
    stored: dict[tuple[int, date], ValuationSnapshot],
    begun: dict[int, date],
    names: dict[int, str],
    anchor: date,
    end: date,
) -> None:
    """Every day an account needs a value for has a stored one. PORT-GIPS-A03."""
    gaps: list[str] = []
    for account_id, first in sorted(begun.items()):
        missing = [
            day for day in _span(max(anchor, first), end) if (account_id, day) not in stored
        ]
        for low, high in _ranges(missing):
            gaps.append(
                f"`pt value --from {low.isoformat()} --to {high.isoformat()} "
                f'-a "{names[account_id]}"`'
            )
    if gaps:
        raise DataUnavailableError(
            f"{len(gaps)} span(s) of days have no stored valuation snapshot",
            code=E_SNAPSHOT_MISSING,
            remedy=(
                "A return is computed from stored snapshots only, and a missing day "
                "is never interpolated (PORT-GIPS-A03). Value them first: "
                + "; ".join(gaps[:5])
                + (f"; and {len(gaps) - 5} more" if len(gaps) > 5 else "")
            ),
            spans=gaps,
        )


def _require_checked(problems: Sequence[str]) -> None:
    """Every snapshot used still re-derives as stored. ADR 0024, ADR 0040 §2."""
    if problems:
        raise ValidationError(
            f"{len(problems)} stored snapshot(s) in the period no longer hold",
            code=E_SNAPSHOT_STALE,
            remedy="; ".join(problems[:5]) + ("; and more" if len(problems) > 5 else ""),
            problems=list(problems),
        )


def _require_unblocked(
    blocked: dict[str, PriceLine],
    begun: dict[int, date],
    names: dict[int, str],
    end: date,
) -> None:
    """An unresolved statement line blocks returns from its date. ADR 0031."""
    for account_id in sorted(begun):
        line = blocked.get(names[account_id])
        if line is not None and line.statement_date <= end:
            raise ValidationError(
                f"{names[account_id]}: returns are blocked from "
                f"{line.statement_date.isoformat()}, where the statement's {line.symbol} "
                f"line is {line.status}",
                code=E_RETURN_BLOCKED,
                remedy=(
                    "`pt price check` lists it. Fix the price, or accept the difference "
                    "with `pt price accept` (ADR 0031)."
                ),
                account=names[account_id],
                statement_date=line.statement_date.isoformat(),
                symbol=line.symbol,
            )


def _refuse_unpriced_trades(repos: Repositories, rows: Sequence[Transaction]) -> None:
    """A trade for cash in an instrument valued at zero. ADR 0040 §12, ADR 0028.

    Its cost would read as a loss on the day, and a sale's proceeds as a gain.
    """
    for txn in rows:
        if txn.txn_type not in _TRADES or txn.instrument_id is None:
            continue
        instrument = repos.instruments.get(txn.instrument_id)
        if instrument is not None and not instrument.has_market_price:
            raise ValidationError(
                f"txn {txn.txn_id} on {txn.trade_date.isoformat()} trades "
                f"{instrument.symbol} for cash, and {instrument.symbol} is valued at "
                "zero for want of a market price",
                code=E_RETURN_UNPRICED_TRADE,
                remedy=(
                    "The trade would read as a loss of its whole cost, or a gain of its "
                    "whole proceeds, on the day. Measure a period that excludes it, or "
                    "price the instrument (ADR 0028)."
                ),
                txn_id=txn.txn_id,
                symbol=instrument.symbol,
            )


# ── the series ───────────────────────────────────────────────────────────────


def _days(
    stored: dict[tuple[int, date], ValuationSnapshot],
    begun: dict[int, date],
    rows: Sequence[Transaction],
    level: FlowLevel,
    anchor: date,
    end: date,
    stale: set[date],
) -> tuple[tuple[_Day, ...], dict[int, GipsRefusalError]]:
    """Each day from *anchor* to *end*, summed across the accounts begun by then.

    *stale* holds the days on which a stored snapshot no longer holds.

    Also the rows whose fee no basis can treat, which every window holding
    them refuses (PORT-GIPS-D05). Their fees are left out of the day, which
    no window then reads.
    """
    fees_on: dict[date, dict[Basis, Decimal]] = {}
    refused: dict[int, GipsRefusalError] = {}
    for txn in rows:
        try:
            each = {basis: fee_addback(txn, basis) for basis in BASES}
        except GipsRefusalError as refusal:
            refused[txn.txn_id] = refusal
            continue
        undone = fees_on.setdefault(txn.trade_date, dict.fromkeys(BASES, _ZERO))
        for basis in BASES:
            undone[basis] += each[basis]

    series: list[_Day] = []
    for day in _span(anchor, end):
        held = [
            stored.get((account_id, day))
            for account_id, first in sorted(begun.items())
            if first <= day
        ]
        present = [snapshot for snapshot in held if snapshot is not None]
        flows = tuple(
            TimedFlow(txn_id=flow.txn_id, on=flow.flow_date, amount=flow.amount, is_large=False)
            for snapshot in present
            for flow in snapshot.flows
            if flow.level is level
        )
        with decimal.localcontext(RETURN_CONTEXT):
            value = sum((snapshot.ending_market_value for snapshot in present), _ZERO)
        # A day missing a snapshot is unusable. A window holding it is refused
        # for the snapshot before it reads any day.
        complete = len(present) == len(held) and all(s.is_complete for s in present)
        oldest = max((p.staleness_days for s in present for p in s.prices), default=0)
        series.append(
            _Day(
                on=day,
                value=value,
                usable=complete and oldest <= PERT_STALENESS_DAYS,
                flows=flows,
                undone=fees_on.get(day, dict.fromkeys(BASES, _ZERO)),
                reliable=len(present) == len(held) and day not in stale,
                complete=complete,
                oldest_price_days=oldest,
            )
        )
    return tuple(series), refused


def _pieces(
    repos: Repositories,
    days: Sequence[_Day],
    configured_staleness: int,
    known: dict[tuple[date, date], _Piece] | None = None,
) -> tuple[_Piece, ...]:
    """Cut the series at every usable day and compute each piece. ADR 0040 §3 to §6.

    A piece that cannot be computed keeps its refusal rather than raising it:
    only a window that covers the piece is refused for it.

    *known* holds pieces already computed, by their two ends. A piece between
    the same two usable days covers the same days, since every usable day is
    a cut, so it is reused rather than computed again; a new one is added.
    """
    pieces: list[_Piece] = []
    begin: _Day | None = None
    between: list[_Day] = []
    for day in days:
        if begin is None:
            # Nothing before the first usable day is ever a window's start.
            if day.usable:
                begin = day
            continue
        between.append(day)
        if not day.usable:
            continue
        reused = known.get((begin.on, day.on)) if known is not None else None
        if reused is not None:
            pieces.append(reused)
            begin, between = day, []
            continue
        uncertain = not all(d.reliable for d in (begin, *between))
        try:
            subperiod = _subperiod(repos, begin, day, between)
        except (PortableError, AssertionError) as refusal:
            carried = [d for d in between if not d.usable and d.carried_only]
            if carried and isinstance(refusal, GipsRefusalError):
                refusal = _carried_in_gap(refusal, carried, configured_staleness)
            piece = _Piece(begin.on, day.on, None, refusal, uncertain)
        else:
            piece = _Piece(begin.on, day.on, subperiod, None, uncertain)
        if known is not None:
            known[(piece.after, piece.through)] = piece
        pieces.append(piece)
        begin, between = day, []
    return tuple(pieces)


def _linked(walked: Walk, anchor: date, end: date, limit: Decimal | None) -> _Linked:
    """The window's sub-periods, from the walk's pieces. ADR 0045 §4, PORT-GIPS-H06.

    The window's two ends must be usable, so each is a cut. The pieces
    between them are whole and lie wholly inside the window.

    A track record ends where the level holds nothing and nothing moves,
    and begins again at the next day that is not so. In order:

    * a window with nothing held at all is refused: it has no return, and
      the empty product of no sub-periods would print as zero;
    * a window that holds one record's end and the next one's beginning is
      refused, because linking the two manufactures a return across days on
      which nothing was invested;
    * days with nothing held before the record begins move the start to
      where it begins, and days after it ends move the end to where it ends
      (PORT-GIPS-H02). Both are carried.

    A piece that cannot be computed is not one with nothing held, so its
    refusal follows these. Pricing more days would not remove a break.

    Under an estimate *limit*, the days the measured path cannot use are
    valued with carried prices, and only those (ADR 0044 §1): the window's
    two ends if they are unusable, and then, inside a gap the bridge cannot
    cross, the days it needs (:func:`_bridged`). A gap the bridge crosses is
    bridged, measured. The breaks are found first, as without the limit,
    and no day is estimated to cross one.
    """
    span = walked.days[(anchor - walked.anchor).days : (end - walked.anchor).days + 1]
    estimated: dict[date, EstimatedDay] = {}
    if limit is not None:
        ends = [day.on for day in (span[0], span[-1]) if not day.usable]
        estimated.update(_estimate(walked, ends, limit))
    days = _valued(span, estimated)
    for day, which in (
        (days[0], "the day before the period"),
        (days[-1], "the period's last day"),
    ):
        if day.usable:
            continue
        if day.carried_only:
            raise _carried(day, which, walked.configured_staleness)
        raise _gap(day.on, day.on, f"{which} cannot be valued")

    known = {(piece.after, piece.through): piece for piece in walked.pieces}
    if estimated:
        pieces = list(_pieces(walked.repos, days, walked.configured_staleness, known))
    else:
        pieces = [p for p in walked.pieces if anchor <= p.after and p.through <= end]
    held = [index for index, piece in enumerate(pieces) if not piece.held_nothing]
    if not held:
        raise _nothing_held(walked, anchor + _DAY, end)
    first, last = held[0], held[-1]
    for index in range(first + 1, last):
        if pieces[index].held_nothing:
            raise _break(walked, pieces, index)

    measured = pieces[first : last + 1]
    if limit is not None:
        measured = _bridged(walked, span, measured, estimated, known, limit)
    subperiods: list[SubPeriod] = []
    for piece in measured:
        if piece.refusal is not None:
            raise piece.refusal
        assert piece.subperiod is not None  # held_nothing is false and no refusal
        subperiods.append(piece.subperiod)
    start, through = measured[0].after + _DAY, measured[-1].through
    # ADR 0044 §3: the result is estimated exactly when a sub-period linked
    # into it is, and it names every estimated day such a sub-period ends on.
    used = sorted(
        {on for sub in subperiods if sub.is_estimated for on in (sub.after, sub.through)}
        & estimated.keys()
    )
    return _Linked(
        subperiods=subperiods,
        start=start,
        end=through,
        before=(anchor + _DAY, measured[0].after) if first > 0 else None,
        after=(through + _DAY, end) if last < len(pieces) - 1 else None,
        estimate=(
            Estimate(limit, tuple(estimated[on] for on in used))
            if used and limit is not None
            else None
        ),
    )


def _bridged(
    walked: Walk,
    span: Sequence[_Day],
    measured: Sequence[_Piece],
    estimated: dict[date, EstimatedDay],
    known: dict[tuple[date, date], _Piece],
    limit: Decimal,
) -> list[_Piece]:
    """*measured*, with each gap the bridge cannot cross cut where it needs a value.

    ADR 0044 §1. The days are each month end inside the gap and the valuation
    a large flow inside it requires (:func:`_unbridgeable`). They are valued
    with carried prices into *estimated*, and the gap is cut at them. What is
    left between the cuts is bridged by Modified Dietz, or, if a large flow's
    test now finds one inside a shorter stretch, cut again. A day estimated
    is always one the measured path could not use, so this ends.
    """
    lo, hi = measured[0].after, measured[-1].through
    pieces = list(measured)
    while True:
        days = _valued(span, estimated)
        needed: set[date] = set()
        for piece in pieces:
            if isinstance(piece.refusal, GipsRefusalError) and (
                piece.refusal.code == E_GIPS_VALUATION_GAP
            ):
                needed |= _bridge_needs(walked.repos, days, piece)
        needed -= estimated.keys()
        if not needed:
            return pieces
        estimated.update(_estimate(walked, needed, limit))
        days = _valued(span, estimated)
        pieces = [
            piece
            for piece in _pieces(walked.repos, days, walked.configured_staleness, known)
            if lo <= piece.after and piece.through <= hi
        ]


def _bridge_needs(repos: Repositories, days: Sequence[_Day], piece: _Piece) -> set[date]:
    """The days inside *piece*, a gap the bridge refused, that it needs valued."""
    offset = days[0].on
    begin = days[(piece.after - offset).days]
    end = days[(piece.through - offset).days]
    covered = days[(piece.after - offset).days + 1 : (piece.through - offset).days + 1]
    flows = _timed_flows(repos, begin, end, covered)
    return {needed for needed, _ in _unbridgeable(piece.after, piece.through, flows)}


def _valued(span: Sequence[_Day], estimated: dict[date, EstimatedDay]) -> list[_Day]:
    """*span*, each estimated day usable at its value with carried prices."""
    if not estimated:
        return list(span)
    return [
        replace(day, value=estimated[day.on].value, usable=True, estimated=True)
        if day.on in estimated
        else day
        for day in span
    ]


def _estimate(walked: Walk, days: Iterable[date], limit: Decimal) -> dict[date, EstimatedDay]:
    """*days* valued with carried prices, each within *limit*. ADR 0044 §2, §4.

    The share of a day is its carried holdings' value, unsigned, over the
    level's value that day. A day whose value is not positive has no share,
    as ``level5_share`` has none, and is refused with the day over the
    limit, earliest first.
    """
    wanted = sorted(set(days))
    walked.estimator.load(wanted)
    found: dict[date, EstimatedDay] = {}
    for on in wanted:
        accounts = walked.estimator.on(on)
        carried = [holding for day in accounts.values() for holding in day.carried]
        with decimal.localcontext(RETURN_CONTEXT):
            value = sum((day.value for day in accounts.values()), _ZERO)
            if value <= 0:
                raise _no_share(walked, on, value, limit)
            share = sum((abs(holding.value) for holding in carried), _ZERO) / value
            holdings = tuple(
                replace(holding, share=abs(holding.value) / value) for holding in carried
            )
        if share > limit:
            raise _over_limit(walked, on, share, limit, holdings)
        found[on] = EstimatedDay(on=on, value=value, share=share, holdings=holdings)
    return found


def _no_share(walked: Walk, on: date, value: Decimal, limit: Decimal) -> ValidationError:
    return ValidationError(
        f"{_scope(walked)} on {on.isoformat()} is worth {value} with its carried prices, "
        "so no share of it can be called carried",
        code=E_ESTIMATE_LIMIT,
        remedy=(
            "The limit is a share of the day's value, and a value of zero or less has "
            "none (ADR 0044 §4). Measure a period that avoids the day, or price it."
        ),
        day=on.isoformat(),
        value=str(value),
        limit=str(limit),
    )


def _over_limit(
    walked: Walk,
    on: date,
    share: Decimal,
    limit: Decimal,
    holdings: Sequence[CarriedHolding],
) -> ValidationError:
    largest = max(holdings, key=lambda holding: (holding.share or _ZERO, holding.symbol))
    return ValidationError(
        f"{_scope(walked)} on {on.isoformat()} would carry {share:.4f} of its value, "
        f"over the --estimate limit of {limit}: {len(holdings)} holding(s), the "
        f"largest {largest.symbol} at a price of {largest.price_date.isoformat()}",
        code=E_ESTIMATE_LIMIT,
        remedy=(
            "An estimate carrying that much is a guess at the return, not an estimate "
            "of it. Price the holdings for the day (`pt price set`, `pt price load`, "
            "then `pt value`), or measure a period that avoids it."
        ),
        day=on.isoformat(),
        share=str(share),
        limit=str(limit),
        holdings=[holding.symbol for holding in holdings],
    )


def _scope(walked: Walk) -> str:
    return walked.account.name if walked.account is not None else "the portfolio"


def _nothing_held(walked: Walk, first: date, last: date) -> GipsRefusalError:
    return GipsRefusalError(
        f"{_scope(walked)} held nothing from {first.isoformat()} to {last.isoformat()}, "
        "and nothing moved: there is no return to measure",
        requirement="PORT-GIPS-H06",
        code=E_RETURN_NOTHING_HELD,
        remedy=(
            "A period with nothing in it has no return, which is not a return of zero. "
            "Measure a period in which something was held."
        ),
        first=first.isoformat(),
        last=last.isoformat(),
    )


def _break(walked: Walk, pieces: Sequence[_Piece], index: int) -> GipsRefusalError:
    """The break that begins at *pieces*[*index*], the first piece with nothing held."""
    termination = pieces[index].after
    resumed = next(
        piece for piece in pieces[index:] if not piece.held_nothing
    ).after  # the last day of the run with nothing held
    restart = resumed + _DAY
    return GipsRefusalError(
        f"{_scope(walked)}'s track record ends on {termination.isoformat()} and begins "
        f"again on {restart.isoformat()}: nothing was held from "
        f"{(termination + _DAY).isoformat()} to {resumed.isoformat()}",
        requirement="PORT-GIPS-H06",
        code=E_RETURN_BREAK,
        remedy=(
            "Returns either side of a break are never linked into one: that would "
            "count the days with nothing invested as a return. Measure each side on "
            f"its own: `--to {termination.isoformat()}`, or `--from {restart.isoformat()}`."
        ),
        termination=termination.isoformat(),
        restart=restart.isoformat(),
    )


def _timed_flows(
    repos: Repositories, begin: _Day, end: _Day, covered: Sequence[_Day]
) -> list[TimedFlow]:
    """The flows from *begin* to *end*, each called large or not by the policy in force."""
    flows: list[TimedFlow] = []
    for day in covered:
        for flow in day.flows:
            policy = repos.policies.in_force(flow.on)
            if policy is None:  # pragma: no cover -- refused before the series
                raise AssertionError("no policy in force inside a checked period")
            weight_base = begin.value if begin.value != 0 else end.value
            flows.append(
                TimedFlow(
                    txn_id=flow.txn_id,
                    on=flow.on,
                    amount=flow.amount,
                    is_large=policy.is_large_flow(flow.amount, weight_base),
                )
            )
    return flows


def _subperiod(
    repos: Repositories, begin: _Day, end: _Day, covered: Sequence[_Day]
) -> SubPeriod | None:
    span = (end.on - begin.on).days
    method = ReturnMethod.DAILY if span == 1 else ReturnMethod.MODIFIED_DIETZ
    flows = _timed_flows(repos, begin, end, covered)
    undone = {basis: sum((day.undone[basis] for day in covered), _ZERO) for basis in BASES}

    if method is ReturnMethod.MODIFIED_DIETZ:
        _check_gap(begin.on, end.on, flows)

    if begin.value == 0 and end.value == 0 and not flows and not any(undone.values()):
        return None

    returns: list[BasisReturn] = []
    for basis in BASES:
        timed = [(flow.on, flow.amount) for flow in flows]
        # A fee the basis does not deduct is undone as an outflow at the end of
        # its day (ADR 0040 §8): over one day, exactly adding it back.
        timed.extend((day.on, -day.undone[basis]) for day in covered if day.undone[basis])
        try:
            value = dietz(begin.value, end.value, timed, after=begin.on, through=end.on)
        except _Undefined:
            raise ValidationError(
                f"the return from {begin.on.isoformat()} to {end.on.isoformat()} is "
                "undefined: the capital it is measured on is not positive",
                code=E_RETURN_UNDEFINED,
                remedy=(
                    "A short or margined position with negative equity, or an outflow "
                    "larger than the value, has no time-weighted return. Nothing is "
                    "clamped. Measure a period that excludes it."
                ),
                beginning_value=str(begin.value),
                ending_value=str(end.value),
                flows=[str(amount) for _, amount in timed],
            ) from None
        returns.append(BasisReturn(basis, value))

    return SubPeriod(
        after=begin.on,
        through=end.on,
        method=method,
        beginning_value=begin.value,
        ending_value=end.value,
        flows=tuple(flows),
        returns=tuple(returns),
        is_estimated=begin.estimated or end.estimated,
    )


def _carried(day: _Day, which: str, configured: int) -> DataUnavailableError:
    """A day `pert` cannot measure only because a price is older than five days.

    ADR 0044 §1. `pt holdings` calls the same snapshot complete, under the
    configured tolerance, so the refusal says why `pert` does not.
    """
    return DataUnavailableError(
        f"{which}, {day.on.isoformat()}, is valued with a price {day.oldest_price_days} "
        f"days old. `pert` measures a day only with prices at most "
        f"{PERT_STALENESS_DAYS} days old, whatever staleness_tolerance_days "
        f"({configured} here) allowed `pt value`",
        code=E_PRICE_STALE,
        remedy=(
            "A price carried that long is not the best approximation of fair value "
            "(PORT-GIPS-A09). Load a newer one with `pt price load`, or record a "
            "judged estimate with `pt price set --estimate`, then `pt value` again."
        ),
        day=day.on.isoformat(),
        price_age_days=day.oldest_price_days,
        pert_staleness_days=PERT_STALENESS_DAYS,
        staleness_tolerance_days=configured,
    )


def _carried_in_gap(
    refusal: GipsRefusalError, carried: Sequence[_Day], configured: int
) -> GipsRefusalError:
    """A gap refusal, saying which of its days only `pert`'s five days made unusable."""
    days = ", ".join(day.on.isoformat() for day in carried)
    return GipsRefusalError(
        f"{refusal.message}. Of its days, {days} "
        f"{'is' if len(carried) == 1 else 'are'} unusable only because a price is more "
        f"than {PERT_STALENESS_DAYS} days old, which staleness_tolerance_days "
        f"({configured} here) allowed `pt value` but `pert` does not",
        requirement=str(refusal.context.get("requirement", "PORT-GIPS-A03")),
        code=refusal.code,
        remedy=refusal.remedy,
        **{k: v for k, v in refusal.context.items() if k != "requirement"},
    )


def _check_gap(after: date, through: date, flows: Sequence[TimedFlow]) -> None:
    """What Modified Dietz may bridge. ADR 0040 §6.

    Never a month end (PORT-GIPS-A03 a, b), and never a large flow the
    valuation it requires is missing for (PORT-GIPS-B03, A03 c).
    """
    reasons = _unbridgeable(after, through, flows)
    if reasons:
        raise _gap(after, through, reasons[0][1])


def _unbridgeable(
    after: date, through: date, flows: Sequence[TimedFlow]
) -> list[tuple[date, str]]:
    """Every reason the bridge from *after* to *through* is refused, in order.

    Each with the day inside the gap whose value would answer it, which is
    what an estimate values (ADR 0044 §1). For a month end, the month end.
    For a large flow, the valuation it requires: the end of the day before an
    inflow, which counts from the start of its day, and the end of the day of
    an outflow.

    ADR 0040 §6 calls any large flow before the gap's last day inside it, so
    a large inflow on a gap's first day is refused although the gap's start
    is the valuation it requires. For that one its own day is the one
    valued, and the stretch to it is a single day. So a large inflow inside
    a gap is estimated around twice: the day before it, and then, in the
    stretch that day begins, its own day.
    """
    reasons: list[tuple[date, str]] = []
    for day in _span(after + _DAY, through - _DAY):
        if day.day == calendar.monthrange(day.year, day.month)[1]:
            reasons.append((day, f"it spans the month end {day.isoformat()}"))
    for flow in flows:
        if flow.is_large and (flow.on < through or flow.amount > 0):
            needed = flow.on - _DAY if flow.amount > 0 else flow.on
            reasons.append(
                (
                    needed if needed > after else flow.on,
                    f"a large flow of {flow.amount} on {flow.on.isoformat()} falls inside it",
                )
            )
    return reasons


def _gap(after: date, through: date, why: str) -> GipsRefusalError:
    return GipsRefusalError(
        f"cannot bridge {after.isoformat()} to {through.isoformat()}: {why}",
        requirement="PORT-GIPS-A03",
        code=E_GIPS_VALUATION_GAP,
        remedy=(
            "A day is valued when its snapshot is complete. Price the days between "
            "(`pt price check`, `pt value`), or measure a period that avoids them."
        ),
        after=after.isoformat(),
        through=through.isoformat(),
    )


def _disclosures(
    repos: Repositories,
    stored: dict[tuple[int, date], ValuationSnapshot],
    rows: Sequence[Transaction],
    subperiods: Sequence[SubPeriod],
    first_day: date,
    end: date,
    *,
    ending: Decimal | None = None,
) -> Disclosures:
    """*ending*, when given, is the level's value on *end* as the return valued it."""
    used = [s for (_, on), s in sorted(stored.items()) if first_day <= on <= end]
    final = [s for (_, on), s in stored.items() if on == end]
    with decimal.localcontext(RETURN_CONTEXT):
        level5 = sum((s.level5_market_value for s in final), _ZERO)
        value = (
            ending if ending is not None else sum((s.ending_market_value for s in final), _ZERO)
        )
        share = level5 / value if value != 0 else None
    unpriced: set[str] = set()
    for snapshot in used:
        for entry in snapshot.prices:
            if entry.source == NO_MARKET_PRICE:
                instrument = repos.instruments.get(entry.instrument_id)
                unpriced.add(instrument.symbol if instrument else str(entry.instrument_id))
    return Disclosures(
        estimated_days=tuple(sorted({s.snapshot_date for s in used if s.uses_estimates})),
        stale_price_days=tuple(
            sorted({s.snapshot_date for s in used if any(p.staleness_days for p in s.prices)})
        ),
        level5_value=level5,
        level5_share=share,
        unpriced_holdings=tuple(sorted(unpriced)),
        margin_interest=any(t.txn_type is TransactionType.MARGIN_INTEREST for t in rows),
        reclaimable_withholding=any((t.withholding_reclaimable or 0) > 0 for t in rows),
        large_flow_days=tuple(
            sorted({f.on for sub in subperiods for f in sub.flows if f.is_large})
        ),
        external_fees=any(
            t.fee_class is FeeClass.EXTERNAL_MGMT_FEE
            and (t.fees + t.commissions) > 0
            and t.txn_type is not TransactionType.MARGIN_INTEREST
            for t in rows
        ),
    )


# ── dates ────────────────────────────────────────────────────────────────────


def _span(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def _ranges(days: Sequence[date]) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    for day in days:
        if out and day == out[-1][1] + _DAY:
            out[-1] = (out[-1][0], day)
        else:
            out.append((day, day))
    return out
