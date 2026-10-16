"""Valuation snapshots: building them, and checking the stored ones still hold.

A snapshot is derived from two things: the ledger, as it stood at the end of
its date, and the prices it consumed, which it records in
``valuation_snapshot_price`` (``PORT-GIPS-J03``). Replay can reproduce the
first and not the second -- prices are not in the ledger -- so a snapshot
cannot be checked the way a lot is, by rebuilding and comparing. It is checked
by re-deriving it from the ledger *holding its recorded prices fixed*, which
is ADR 0010's "replay determinism is asserted holding the price set fixed",
applied to the stored rows themselves (ADR 0024).

`pt value` builds with :func:`value_range`; `pt validate` checks with
:func:`verify`. Both go through :func:`build`, so the check cannot drift from
what it checks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from portable_core.decimals import from_text
from portable_core.domain.enums import TransactionType, ValuationBasis
from portable_core.domain.models import (
    Account,
    Price,
    SnapshotPrice,
    Transaction,
    ValuationSnapshot,
)
from portable_core.errors import DataUnavailableError
from portable_core.persistence.repositories import RemovedSnapshots, Repositories
from portable_core.services.replay import effective_ledger, over_dates
from portable_core.services.valuation import Holding, ValuationEngine

__all__ = [
    "PriceLookup",
    "build",
    "rederived_differently",
    "removal_notices",
    "store",
    "value_range",
    "verify",
]

ZERO = Decimal("0.00")

#: The price a snapshot uses for an instrument on a date, or None.
PriceLookup = Callable[[int, date], Price | None]

_REVALUE = "value it again with `pt value --date {on}`, or a range that covers it"

#: A recorded price was within tolerance when it was recorded. Re-checking it
#: against today's setting would report a changed configuration as a changed
#: snapshot, so verification accepts whatever was recorded.
_ANY_STALENESS = 36_500


def build(
    repos: Repositories,
    account: Account,
    on: date,
    *,
    beginning: Decimal,
    transactions: list[Transaction],
    price_for: PriceLookup,
    staleness_tolerance_days: int,
) -> ValuationSnapshot:
    """Price an account's holdings and assemble its snapshot for one date.

    Reads positions and cash from derived state, so it must run where derived
    state is the state at the end of *on* -- inside :func:`over_dates`
    (ADR 0021). *transactions* are the account's effective ledger rows dated
    *on*: the rows, reversals netted out, that the positions and cash were
    derived from. A flow taken from any other set is a flow the market value
    does not reflect.

    Instruments that cannot be priced are collected rather than raised on, so
    that one missing price does not hide the rest of the report -- but the
    snapshot is marked incomplete, and an incomplete snapshot must not be used
    for a return.
    """
    engine = ValuationEngine(staleness_tolerance_days=staleness_tolerance_days)
    priced: list[SnapshotPrice] = []
    missing: list[str] = []
    for holding in _holdings(repos, account.account_id):
        try:
            priced.append(
                engine.price_holding(
                    holding, price_for(holding.instrument.instrument_id, on), on
                )
            )
        except DataUnavailableError:
            missing.append(holding.instrument.symbol)

    cash, margin = repos.valuations.cash(account.account_id, currency=account.currency)
    return engine.build_snapshot(
        account,
        on,
        priced=priced,
        cash_balance=cash,
        margin_loan=margin,
        beginning_market_value=beginning,
        transactions=transactions,
        policy=repos.policies.in_force(on),
        incomplete_symbols=tuple(missing),
    )


def value_range(
    repos: Repositories,
    accounts: Sequence[Account],
    first: date,
    last: date,
    *,
    staleness_tolerance_days: int,
) -> tuple[list[ValuationSnapshot], list[str]]:
    """Every account's snapshot for every day from *first* to *last*, from the cache.

    Account-major, oldest first. Positions and cash on each day are the state
    at the end of that day, not today's state at that day's prices
    (ADR 0021). Beginning market value chains from one day's snapshot to the
    next in memory; only the day before *first* comes from a stored snapshot,
    read before the walk rewinds derived state.
    """
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    rows_on = _rows_on(repos, set(days))

    beginning: dict[int, Decimal] = {}
    for target in accounts:
        previous = repos.valuations.snapshot(target.account_id, first - timedelta(days=1))
        beginning[target.account_id] = (
            from_text(str(previous["ending_market_value"])) if previous is not None else ZERO
        )

    def visit(on: date) -> list[ValuationSnapshot]:
        built: list[ValuationSnapshot] = []
        for target in accounts:
            snapshot = build(
                repos,
                target,
                on,
                beginning=beginning[target.account_id],
                transactions=rows_on.get((target.account_id, on), []),
                price_for=repos.prices.newest_on_or_before,
                staleness_tolerance_days=staleness_tolerance_days,
            )
            beginning[target.account_id] = snapshot.ending_market_value
            built.append(snapshot)
        return built

    walked = over_dates(repos, days, visit)
    by_account = [[day[index] for day in walked.results] for index in range(len(accounts))]
    return [snapshot for series in by_account for snapshot in series], list(walked.warnings)


def verify(repos: Repositories) -> list[str]:
    """Every stored snapshot that no longer holds, one sentence each.

    Two checks (ADR 0024):

    * **Re-derived from the ledger as it now stands**, with the prices the
      snapshot recorded, it must come out the same, field for field, price for
      price and flow for flow. Its beginning value is the stored ending value
      of the day before when there is one, and what it recorded when there is
      not. A snapshot that does not come out the same describes a book the
      ledger, or the day before, no longer contains.
    * **Each recorded price must still be what its price row says.** One that
      is not -- a reloaded price, an estimate since made final -- is the case
      ``PORT-GIPS-A09`` exists for: the snapshot used a figure that has since
      changed, and has to be valued again so the change can be reported.
    """
    names = {account.account_id: account.name for account in repos.accounts.all()}
    problems = [
        f"{names.get(account_id)} {on.isoformat()}: recorded {symbol} at {recorded}, "
        f"and its price row now says {current} -- {_REVALUE.format(on=on.isoformat())}"
        for account_id, on, symbol, recorded, current in repos.valuations.superseded_prices()
    ]

    for item in _rederive(repos):
        again = repos.valuations.record_of(item.again)
        differing = [field for field in item.kept if item.kept[field] != again[field]]
        if differing:
            problems.append(
                f"{names.get(item.account_id)} {item.on.isoformat()}: re-derived from the "
                f"ledger with the prices it recorded, its {', '.join(differing)} "
                f"{'differs' if len(differing) == 1 else 'differ'} from what is stored "
                f"-- {_REVALUE.format(on=item.on.isoformat())}"
            )
    return problems


def store(repos: Repositories, built: Sequence[ValuationSnapshot]) -> list[str]:
    """Save what :func:`value_range` built, and re-chain the stored day after it.

    A snapshot's beginning value is the ending value of the stored day before
    it. Valuing a range can supply that day for a snapshot already stored on
    the day after the range -- valued alone earlier, when it had no day before
    and began from zero. That snapshot is derived again with the prices it
    recorded and saved, so its beginning value follows the new chain; nothing
    else in it can change, because nothing else read the day before (ADR 0024).

    Returns one sentence per snapshot re-chained, for the command to report.
    """
    for snapshot in built:
        repos.valuations.save_snapshot(snapshot)
    after: set[tuple[int, date]] = set()
    for snapshot in built:
        after.add((snapshot.account_id, snapshot.snapshot_date + timedelta(days=1)))
    after -= {(snapshot.account_id, snapshot.snapshot_date) for snapshot in built}
    if not after:
        return []

    names = {account.account_id: account.name for account in repos.accounts.all()}
    notices: list[str] = []
    for again in _differently(repos, lambda account_id, on: (account_id, on) in after):
        repos.valuations.save_snapshot(again)
        notices.append(
            f"updated the stored {names.get(again.account_id)} snapshot of "
            f"{again.snapshot_date.isoformat()}: its beginning value follows the day "
            "valued before it"
        )
    return notices


def rederived_differently(
    repos: Repositories, since: date, until: date | None = None
) -> list[ValuationSnapshot]:
    """Stored snapshots dated from *since* up to *until* that re-derive differently.

    Each comes back as re-derived: the ledger as it now stands, the prices it
    recorded, and the policy now in force on its date. Saving them restates
    them without discarding a single recorded price (ADR 0025), which is what
    a change of return policy needs -- it moves flags, not values.
    """
    return _differently(
        repos, lambda _account_id, on: since <= on and (until is None or on < until)
    )


def _differently(
    repos: Repositories, wanted: Callable[[int, date], bool]
) -> list[ValuationSnapshot]:
    return [
        item.again
        for item in _rederive(repos, wanted)
        if repos.valuations.record_of(item.again) != item.kept
    ]


@dataclass(frozen=True, slots=True)
class _Rederived:
    account_id: int
    on: date
    #: The stored snapshot, in :meth:`ValuationRepository.record` form.
    kept: dict[str, Any]
    #: The same snapshot, derived again with its recorded prices held fixed.
    again: ValuationSnapshot


def _rederive(
    repos: Repositories, wanted: Callable[[int, date], bool] | None = None
) -> list[_Rederived]:
    """Derive stored snapshots again, holding their recorded prices fixed. ADR 0024.

    Each is derived from the ledger as it stands at the end of its date (the
    ADR 0021 walk), with exactly the prices it recorded. Its beginning value is
    the stored ending value of the day before when there is one, and what it
    recorded when there is not. *wanted* picks by account and date; every
    stored snapshot still supplies the ending values the chosen ones begin from.
    """
    stored = repos.valuations.stored_snapshots()
    chosen = [entry for entry in stored if wanted is None or wanted(entry[1], entry[2])]
    if not chosen:
        return []

    # Read before the walk: inside it, derived state is rewound, and these
    # are what is being derived again, not inputs to the derivation.
    kept = {snapshot_id: repos.valuations.record(snapshot_id) for snapshot_id, _, _ in stored}
    recorded = {
        snapshot_id: repos.valuations.recorded_prices(snapshot_id)
        for snapshot_id, _, _ in chosen
    }
    ending = {
        (account_id, on): kept[snapshot_id]["ending_market_value"]
        for snapshot_id, account_id, on in stored
    }
    by_date: dict[date, list[tuple[int, int]]] = {}
    for snapshot_id, account_id, on in chosen:
        by_date.setdefault(on, []).append((snapshot_id, account_id))
    days = sorted(by_date)
    accounts = {account.account_id: account for account in repos.accounts.all()}
    rows_on = _rows_on(repos, set(days))

    def visit(on: date) -> list[_Rederived]:
        again: list[_Rederived] = []
        for snapshot_id, account_id in by_date[on]:
            beginning = ending.get(
                (account_id, on - timedelta(days=1)),
                kept[snapshot_id]["beginning_market_value"],
            )
            snapshot = build(
                repos,
                accounts[account_id],
                on,
                beginning=from_text(beginning),
                transactions=rows_on.get((account_id, on), []),
                price_for=_recorded(recorded[snapshot_id]),
                staleness_tolerance_days=_ANY_STALENESS,
            )
            again.append(_Rederived(account_id, on, kept[snapshot_id], snapshot))
        return again

    return [item for day in over_dates(repos, days, visit).results for item in day]


def removal_notices(removed: Iterable[RemovedSnapshots], *, dry_run: bool) -> list[str]:
    """One sentence per account whose valuation snapshots a ledger write removed.

    A removed snapshot is valuation history the user had and no longer has, so
    it is never removed in silence (ADR 0024). A dry run that rehearses its
    writes in a rolled-back transaction (`pt import batch`) removed nothing, and
    says what it would have removed.
    """
    merged: dict[str, tuple[date, date, int]] = {}
    for entry in removed:
        if entry.account in merged:
            first, last, count = merged[entry.account]
            merged[entry.account] = (
                min(first, entry.first),
                max(last, entry.last),
                count + entry.count,
            )
        else:
            merged[entry.account] = (entry.first, entry.last, entry.count)

    verb = "would remove" if dry_run else "removed"
    notices: list[str] = []
    for account, (first, last, count) in sorted(merged.items()):
        span = first.isoformat() if first == last else f"{first.isoformat()} to {last}"
        noun = "snapshot" if count == 1 else "snapshots"
        notices.append(
            f"{verb} {count} valuation {noun} of {account} ({span}): this entry changes "
            f"them. Value them again with `pt value --from {first.isoformat()} "
            f'--to {last.isoformat()} -a "{account}"`.'
        )
    return notices


def _recorded(entries: Iterable[SnapshotPrice]) -> PriceLookup:
    """Offer a snapshot exactly the prices it recorded, and nothing else."""
    by_instrument = {entry.instrument_id: entry for entry in entries}

    def lookup(instrument_id: int, on: date) -> Price | None:
        entry = by_instrument.get(instrument_id)
        if entry is None:
            return None
        return Price(
            instrument_id=instrument_id,
            price_date=on - timedelta(days=entry.staleness_days),
            price=entry.price,
            source=entry.source,
            as_of=entry.as_of,
            valuation_level=entry.valuation_level,
            valuation_basis=ValuationBasis.EXCHANGE_CLOSE,
            is_estimate=entry.is_estimate,
            price_id=entry.price_id,
        )

    return lookup


def _holdings(repos: Repositories, account_id: int) -> list[Holding]:
    """Open legs in an account, as holdings to be priced.

    Takes no date on purpose. It reads derived state as it currently stands,
    and the caller is what makes that the right date: it runs inside
    :func:`over_dates`, which rewinds derived state to the date being valued
    when anything later is in the ledger (ADR 0021).
    """
    holdings: list[Holding] = []
    for position in repos.positions.all(account_id=account_id, open_only=True):
        for leg in position.legs:
            if leg.quantity == 0:
                continue
            instrument = repos.instruments.get(leg.instrument_id)
            if instrument is None:
                continue
            holdings.append(Holding(instrument, leg.quantity))
    return holdings


def _rows_on(repos: Repositories, days: set[date]) -> dict[tuple[int, date], list[Transaction]]:
    """Effective ledger rows on *days*, filed under every account they touch.

    A transfer is one row with two sides, and the receiving account's snapshot
    needs its inflow (ADR 0007).
    """
    rows: dict[tuple[int, date], list[Transaction]] = {}
    for txn in effective_ledger(repos.transactions.in_ledger_order()):
        if txn.trade_date not in days:
            continue
        sides = [txn.account_id]
        if txn.txn_type is TransactionType.TRANSFER and txn.counter_account_id is not None:
            sides.append(txn.counter_account_id)
        for side in sides:
            rows.setdefault((side, txn.trade_date), []).append(txn)
    return rows
