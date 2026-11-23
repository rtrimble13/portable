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
"""

from __future__ import annotations

import calendar
import decimal
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
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
    Disclosures,
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
    E_FEE_EMBEDDED_IN_CASH,
    E_GIPS_NO_FLOW_POLICY,
    E_GIPS_VALUATION_GAP,
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
from portable_core.services.price_acceptance import PriceLine
from portable_core.services.replay import effective_ledger
from portable_core.services.snapshots import SnapshotProblem
from portable_core.services.valuation import NO_MARKET_PRICE

__all__ = [
    "RETURN_CONTEXT",
    "Walk",
    "dietz",
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


def time_weighted_return(
    repos: Repositories,
    *,
    end: date,
    start: date | None = None,
    account: Account | None = None,
    staleness_days: int,
) -> TimeWeightedReturn:
    """The cumulative TWR of *account*, or of the portfolio, through *end*.

    *start* defaults to inception, and a start before inception moves to it;
    the result carries both. Every refusal is a place where a plausible
    number would otherwise have been printed (ADR 0040 §2, §4, §6, §7, §12).

    A walk over the period alone, and its one window (ADR 0045 §1). The
    checks that need no walk are made first, so a refusal that needs none
    costs none.
    """
    accounts = [account] if account is not None else repos.accounts.all()
    _, first_day, _ = _open(repos, _begun(repos, accounts, end), start, end)
    walked = walk(
        repos, first=first_day, through=end, account=account, staleness_days=staleness_days
    )
    return window(walked, start=start, end=end)


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
            price_acceptance.check_recorded(repos, staleness_days=staleness_days)
        ),
        rows=rows,
        fee_refusals=fee_refusals,
        days=days,
        pieces=_pieces(repos, days),
    )


def window(walked: Walk, *, start: date | None, end: date) -> TimeWeightedReturn:
    """The return of *walked*'s level from *start* through *end*. ADR 0045 §1.

    It is exactly what a walk over this window alone would give: the same
    checks, in the order `pert twr` has always made them, on the window's own
    accounts and days.

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

    linked = _linked(walked, anchor, end)
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
            repos, stored, measured, linked.subperiods, linked.start, linked.end
        ),
    )


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
        series.append(
            _Day(
                on=day,
                value=value,
                # A day missing a snapshot is unusable. A window holding it is
                # refused for the snapshot before it reads any day.
                usable=len(present) == len(held)
                and all(snapshot.is_complete for snapshot in present),
                flows=flows,
                undone=fees_on.get(day, dict.fromkeys(BASES, _ZERO)),
                reliable=len(present) == len(held) and day not in stale,
            )
        )
    return tuple(series), refused


def _pieces(repos: Repositories, days: Sequence[_Day]) -> tuple[_Piece, ...]:
    """Cut the series at every usable day and compute each piece. ADR 0040 §3 to §6.

    A piece that cannot be computed keeps its refusal rather than raising it:
    only a window that covers the piece is refused for it.
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
        uncertain = not all(d.reliable for d in (begin, *between))
        try:
            subperiod = _subperiod(repos, begin, day, between)
        except (PortableError, AssertionError) as refusal:
            piece = _Piece(begin.on, day.on, None, refusal, uncertain)
        else:
            piece = _Piece(begin.on, day.on, subperiod, None, uncertain)
        pieces.append(piece)
        begin, between = day, []
    return tuple(pieces)


def _linked(walked: Walk, anchor: date, end: date) -> _Linked:
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
    """
    first_day = walked.days[(anchor - walked.anchor).days]
    last_day = walked.days[(end - walked.anchor).days]
    if not first_day.usable:
        raise _gap(first_day.on, first_day.on, "the day before the period cannot be valued")
    if not last_day.usable:
        raise _gap(last_day.on, last_day.on, "the period's last day cannot be valued")

    pieces = [p for p in walked.pieces if anchor <= p.after and p.through <= end]
    held = [index for index, piece in enumerate(pieces) if not piece.held_nothing]
    if not held:
        raise _nothing_held(walked, anchor + _DAY, end)
    first, last = held[0], held[-1]
    for index in range(first + 1, last):
        if pieces[index].held_nothing:
            raise _break(walked, pieces, index)

    measured = pieces[first : last + 1]
    subperiods: list[SubPeriod] = []
    for piece in measured:
        if piece.refusal is not None:
            raise piece.refusal
        assert piece.subperiod is not None  # held_nothing is false and no refusal
        subperiods.append(piece.subperiod)
    start, through = measured[0].after + _DAY, measured[-1].through
    return _Linked(
        subperiods=subperiods,
        start=start,
        end=through,
        before=(anchor + _DAY, measured[0].after) if first > 0 else None,
        after=(through + _DAY, end) if last < len(pieces) - 1 else None,
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


def _subperiod(
    repos: Repositories, begin: _Day, end: _Day, covered: Sequence[_Day]
) -> SubPeriod | None:
    span = (end.on - begin.on).days
    method = ReturnMethod.DAILY if span == 1 else ReturnMethod.MODIFIED_DIETZ
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
    )


def _check_gap(after: date, through: date, flows: Sequence[TimedFlow]) -> None:
    """What Modified Dietz may bridge. ADR 0040 §6.

    Never a month end (PORT-GIPS-A03 a, b), and never a large flow the
    valuation it requires is missing for (PORT-GIPS-B03, A03 c).
    """
    for day in _span(after + _DAY, through - _DAY):
        if day.day == calendar.monthrange(day.year, day.month)[1]:
            raise _gap(after, through, f"it spans the month end {day.isoformat()}")
    for flow in flows:
        if flow.is_large and (flow.on < through or flow.amount > 0):
            raise _gap(
                after,
                through,
                f"a large flow of {flow.amount} on {flow.on.isoformat()} falls inside it",
            )


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
) -> Disclosures:
    used = [s for (_, on), s in sorted(stored.items()) if first_day <= on <= end]
    ending = [s for (_, on), s in stored.items() if on == end]
    with decimal.localcontext(RETURN_CONTEXT):
        level5 = sum((s.level5_market_value for s in ending), _ZERO)
        value = sum((s.ending_market_value for s in ending), _ZERO)
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
