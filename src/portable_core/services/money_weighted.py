"""The money-weighted return, solved from a walk's flows. ADR 0046.

`pert mwr` is a thin layer over :func:`money_weighted_return`. Everything
that decides a number is here:

* **One solve from the level's flows** (§1). They are the flows a window of
  the walk reads, so at portfolio level they are the accounts' combined,
  with a transfer between the owner's accounts no flow at all, and solved
  once (``PORT-GIPS-C05``). An inflow counts from the start of its day and an
  outflow from its end (ADR 0040 §3), and time is counted in years by
  anniversary (ADR 0045 §5). Each basis undoes the fees it does not deduct as
  outflows at the end of their day (ADR 0040 §8).
* **A guarded Newton solve for g = ln(1 + R)** (§2), in
  :data:`~portable_core.services.returns.RETURN_CONTEXT`, with ``exp`` and
  ``ln`` correctly rounded, so the bytes are fixed (invariant 6).
* **A rate that is provably the only one, or none** (§3, ``PORT-GIPS-C04``).
  Compounded at the rate found, the balance must never fall below zero
  before the end. Then no other rate solves.
* **Never without the time-weighted return** (§4, ``PORT-GIPS-C01``). The TWR
  over the same window is computed first, and a window it refuses is
  refused. Both cover the same days.
"""

from __future__ import annotations

import decimal
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from portable_core.domain.models import Account
from portable_core.domain.periods import at_least_a_year, years
from portable_core.domain.returns import (
    BASES,
    Basis,
    MoneyWeightedBasis,
    MoneyWeightedReturn,
    Solve,
    TimedFlow,
)
from portable_core.errors import ValidationError
from portable_core.errors.kinds import (
    E_MWR_NO_CONVERGENCE,
    E_MWR_NOT_UNIQUE,
    E_RETURN_TOTAL_LOSS,
)
from portable_core.persistence.repositories import Repositories
from portable_core.services import returns
from portable_core.services.returns import RETURN_CONTEXT, Walk

__all__ = [
    "BRACKET_GROWTH",
    "STEPS",
    "YEAR_COUNT",
    "Moment",
    "money_weighted_return",
    "solve",
    "window",
]

#: How a year is counted, recorded on every result (ADR 0046, answer 3).
YEAR_COUNT: Final = "anniversary"
#: ADR 0046 §2: the bracket, as the log of the growth over the window.
BRACKET_GROWTH: Final = Decimal(40)
#: ADR 0046 §2: the most steps the solve may take.
STEPS: Final = 200
#: Converged when g moves by no more than this, or the bracket is this narrow.
_G_TOLERANCE: Final = Decimal("1e-30")
#: Converged when the present value is no more than this share of the gross.
_PV_TOLERANCE: Final = Decimal("1e-28")

_DAY: Final = timedelta(days=1)
_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)


@dataclass(frozen=True, slots=True)
class Moment:
    """Money in (positive) or out (negative) at the end of one day.

    *years* is the time from the window's anchor, by anniversary. *txn_ids*
    are the external flows the amount holds, named in a refusal.
    """

    on: date
    years: Decimal
    amount: Decimal
    txn_ids: tuple[int, ...]


def money_weighted_return(
    repos: Repositories,
    *,
    end: date,
    start: date | None = None,
    account: Account | None = None,
    staleness_days: int,
    estimate: Decimal | None = None,
) -> MoneyWeightedReturn:
    """The MWR of *account*, or of the portfolio, through *end*, beside its TWR.

    *start* defaults to inception. The arguments are those of
    :func:`~portable_core.services.returns.time_weighted_return`, and so is
    every refusal the TWR beside it makes.
    """
    walked = returns.walk_for(
        repos, start=start, end=end, account=account, staleness_days=staleness_days
    )
    return window(walked, start=start, end=end, estimate=estimate)


def window(
    walked: Walk, *, start: date | None, end: date, estimate: Decimal | None = None
) -> MoneyWeightedReturn:
    """The MWR of *walked*'s level over one window, beside the TWR of the same window.

    The TWR is computed first, and its refusal is the MWR's (ADR 0046 §4). The
    MWR then covers exactly the TWR's days: a start or end the TWR moved past
    days with nothing held moves it too.
    """
    twr = returns.window(walked, start=start, end=end, estimate=estimate)
    anchor, last = twr.start - _DAY, twr.end
    flows, undone = returns.level_flows(walked, anchor, last)
    beginning = twr.subperiods[0].beginning_value
    ending = twr.subperiods[-1].ending_value
    estimated = {day.on for day in twr.estimate.days} if twr.estimate is not None else set()
    scope = walked.account.name if walked.account is not None else "the portfolio"

    bases = tuple(
        _basis(
            basis,
            moments(anchor, last, beginning, flows, undone[basis], ending),
            scope=scope,
            anchor=anchor,
            last=last,
        )
        for basis in BASES
    )
    length = years(anchor, last)
    with decimal.localcontext(RETURN_CONTEXT):
        flow_total = sum((flow.amount for flow in flows), _ZERO)
    return MoneyWeightedReturn(
        twr=twr,
        since_inception=any(
            record.inception == twr.start for record in returns.track_records(walked)
        ),
        is_annualized=at_least_a_year(anchor, last),
        whole_years=length.whole,
        part_days=length.days,
        part_of=length.of,
        beginning_value=beginning,
        ending_value=ending,
        flow_count=len(flows),
        flow_total=flow_total,
        bases=bases,
        is_estimated=anchor in estimated or last in estimated,
    )


def moments(
    anchor: date,
    last: date,
    beginning: Decimal,
    flows: Sequence[TimedFlow],
    undone: Sequence[tuple[date, Decimal]],
    ending: Decimal,
) -> list[Moment]:
    """The window's money in and out, by the moment it counts from. ADR 0046 §1.

    The beginning value at the anchor, money in. Each inflow at the end of
    the day before its own, since it counts from the start of its day; each
    outflow and each undone fee at the end of its day (ADR 0040 §3, §8). The
    ending value at the end, money out. Amounts at one moment are summed,
    and a moment that sums to nothing is dropped.
    """
    by_day: dict[date, tuple[Decimal, list[int]]] = {anchor: (beginning, [])}

    def add(on: date, amount: Decimal, txn_id: int | None) -> None:
        total, ids = by_day.get(on, (_ZERO, []))
        by_day[on] = (total + amount, [*ids, txn_id] if txn_id is not None else ids)

    with decimal.localcontext(RETURN_CONTEXT):
        for flow in flows:
            add(flow.on - _DAY if flow.amount > 0 else flow.on, flow.amount, flow.txn_id)
        for on, fee in undone:
            add(on, -fee, None)
        add(last, -ending, None)
        return [
            Moment(on, _years(anchor, on), total, tuple(ids))
            for on, (total, ids) in sorted(by_day.items())
            if total != 0
        ]


def _years(anchor: date, on: date) -> Decimal:
    length = years(anchor, on)
    return Decimal(length.whole) + Decimal(length.days) / Decimal(length.of)


def _basis(
    basis: Basis, series: Sequence[Moment], *, scope: str, anchor: date, last: date
) -> MoneyWeightedBasis:
    g, found = solve(series, span=_years(anchor, last), scope=scope, anchor=anchor, last=last)
    lowest, lowest_on = _require_only_rate(series, g, scope=scope, anchor=anchor, last=last)
    span = _years(anchor, last)
    with decimal.localcontext(RETURN_CONTEXT):
        return MoneyWeightedBasis(
            basis=basis,
            annual=g.exp() - _ONE,
            cumulative=(g * span).exp() - _ONE,
            solve=found,
            lowest_balance=lowest,
            lowest_balance_on=lowest_on,
        )


# ── the solve ────────────────────────────────────────────────────────────────


def _present_value(series: Sequence[Moment], g: Decimal) -> tuple[Decimal, Decimal]:
    """The series' present value at g, and its derivative in g."""
    value = slope = _ZERO
    for moment in series:
        discounted = moment.amount * (-g * moment.years).exp()
        value += discounted
        slope -= discounted * moment.years
    return value, slope


def solve(
    series: Sequence[Moment], *, span: Decimal, scope: str, anchor: date, last: date
) -> tuple[Decimal, Solve]:
    """g = ln(1 + R) at which *series* is worth nothing. ADR 0046 §2.

    Newton, a step taken only when it lands inside the bracket, which halves
    otherwise. The bracket is g·span between -40 and 40.

    Raises:
        ValidationError: nothing was ever taken out and nothing is left, a
            total loss with no rate; no rate in the bracket, or more than
            one; or no convergence in :data:`STEPS` steps.
    """
    with decimal.localcontext(RETURN_CONTEXT):
        if all(moment.amount >= 0 for moment in series):
            raise ValidationError(
                f"{scope} lost everything put in from {anchor.isoformat()} to "
                f"{last.isoformat()}: no money-weighted rate solves",
                code=E_RETURN_TOTAL_LOSS,
                remedy="The loss is -100%. Nothing is clamped or estimated.",
            )
        gross = sum((abs(moment.amount) for moment in series), _ZERO)
        tolerance = _PV_TOLERANCE * gross
        lower, upper = -BRACKET_GROWTH / span, BRACKET_GROWTH / span
        low_value, _ = _present_value(series, lower)
        high_value, _ = _present_value(series, upper)
        if (low_value > 0) == (high_value > 0):
            raise _no_rate(scope, anchor, last, lower, upper, "has the same sign at both ends")
        # Oriented so that the present value is negative at `low`.
        sign = _ONE if low_value < 0 else -_ONE
        low, high = lower, upper
        g = _ZERO
        newton = bisection = 0
        for _ in range(STEPS):
            value, slope = _present_value(series, g)
            if abs(value) <= tolerance:
                break
            if sign * value < 0:
                low = g
            else:
                high = g
            if high - low <= _G_TOLERANCE:
                break
            step = value / slope if slope != 0 else None
            if step is not None and low < g - step < high:
                g -= step
                newton += 1
                if abs(step) <= _G_TOLERANCE:
                    break
            else:
                g = (low + high) / 2
                bisection += 1
        else:
            raise _no_rate(
                scope, anchor, last, lower, upper, f"did not settle in {STEPS} steps"
            )
        residual = abs(_present_value(series, g)[0]) / gross
    return g, Solve(newton, bisection, lower, upper, residual)


def _no_rate(
    scope: str, anchor: date, last: date, lower: Decimal, upper: Decimal, why: str
) -> ValidationError:
    return ValidationError(
        f"no money-weighted rate for {scope} from {anchor.isoformat()} to "
        f"{last.isoformat()}: its flows' present value {why} of the bracket",
        code=E_MWR_NO_CONVERGENCE,
        remedy=(
            "The bracket allows a growth over the period between e^-40 and e^40, which "
            "holds any real portfolio. Same signs at both ends means no rate in it, or "
            "more than one (ADR 0046 §2). Rely on the time-weighted return, or measure "
            "a shorter period."
        ),
        lower=str(lower),
        upper=str(upper),
    )


# ── the proof that it is the only rate ───────────────────────────────────────


def _require_only_rate(
    series: Sequence[Moment], g: Decimal, *, scope: str, anchor: date, last: date
) -> tuple[Decimal, date]:
    """The lowest balance before the end, at g, which must not be negative. ADR 0046 §3.

    Compound the money in and out forward at the rate found. If the first
    amount is money in and no balance before the end is negative, every
    balance is larger at any higher rate and smaller at any lower one, so the
    final balance is zero at this rate alone (Teichroew, Robichek and
    Montalbano's pure investment). Rounding leaves an exact zero within
    :data:`_PV_TOLERANCE` of the gross, which counts as zero.

    Raises:
        ValidationError: the first amount is not money in, or a balance
            before the end is negative (PT-E-MWR-NOT-UNIQUE).
    """
    with decimal.localcontext(RETURN_CONTEXT):
        gross = sum((abs(moment.amount) for moment in series), _ZERO)
        floor = -_PV_TOLERANCE * gross
        first = series[0]
        if first.amount <= 0:
            raise _not_unique(scope, anchor, last, g, first, first.amount, "is money out")
        balance = _ZERO
        lowest: tuple[Decimal, date] | None = None
        previous = _ZERO
        for moment in series[:-1]:
            balance = balance * (g * (moment.years - previous)).exp() + moment.amount
            previous = moment.years
            if balance < floor:
                raise _not_unique(scope, anchor, last, g, moment, balance, "falls below zero")
            if lowest is None or balance < lowest[0]:
                lowest = (balance, moment.on)
    assert lowest is not None  # the first amount is in and precedes the end
    return lowest


def _not_unique(
    scope: str,
    anchor: date,
    last: date,
    g: Decimal,
    moment: Moment,
    balance: Decimal,
    why: str,
) -> ValidationError:
    with decimal.localcontext(RETURN_CONTEXT):
        rate = g.exp() - _ONE
    return ValidationError(
        f"the money-weighted rate {rate:.6f} for {scope} from {anchor.isoformat()} to "
        f"{last.isoformat()} cannot be shown to be the only one: compounded at it, the "
        f"balance {why} on {moment.on.isoformat()}, at {balance:.2f}",
        code=E_MWR_NOT_UNIQUE,
        remedy=(
            "A withdrawal took out more than the money had grown to at the rate, so "
            "another rate may solve too, and one rate misdescribes the period (ADR 0046 "
            "§3, PORT-GIPS-C04). Rely on the time-weighted return, or measure a period "
            "that ends before the flows named."
        ),
        day=moment.on.isoformat(),
        balance=str(balance),
        rate=str(rate),
        txn_ids=list(moment.txn_ids),
    )
