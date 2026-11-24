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
  ``ln`` correctly rounded, so the bytes are fixed (invariant 6). Its
  bracket is computed from the flows, so that it provably holds a rate.
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
    "STEPS",
    "YEAR_COUNT",
    "Moment",
    "money_weighted_return",
    "solve",
    "window",
]

#: How a year is counted, recorded on every result (ADR 0046, answer 3).
YEAR_COUNT: Final = "anniversary"
#: ADR 0046 §2: the most steps the solve may take.
STEPS: Final = 200
#: Converged when g moves by no more than this share of itself (or of one),
#: or the bracket is that narrow.
_G_TOLERANCE: Final = Decimal("1e-30")
#: Converged when the present value is no more than this share of its terms'
#: gross, each discounted at the rate: a measure that holds at any scale.
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
        # A track record begins where the day before held nothing (ADR 0045
        # §4). Read off the walk's records instead, a walk that starts at the
        # window would see one begin there.
        since_inception=beginning == 0,
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
    g, found = solve(series, scope=scope, anchor=anchor, last=last)
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


def _present_value(series: Sequence[Moment], g: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """The series' present value at g, its derivative in g, and its terms' gross."""
    value = slope = gross = _ZERO
    for moment in series:
        discounted = moment.amount * (-g * moment.years).exp()
        value += discounted
        slope -= discounted * moment.years
        gross += abs(discounted)
    return value, slope, gross


def bracket(series: Sequence[Moment]) -> tuple[Decimal, Decimal]:
    """Rates of g below and above which the present value is negative and positive.

    ADR 0046 §2, as built. For a series that begins with money in and ends
    with money out:

    * **Above the top**, the first amount outweighs all the others however
      they are discounted. If *S* is the gross of the rest, then at
      ``g >= (ln(S / first) + 1) / (t1 - t0)`` (or ``1 / (t1 - t0)`` when *S*
      is the smaller), their discounted gross is at most ``first / e``.
    * **Below the bottom**, the last amount outweighs all the earlier ones.
      If *S* is their gross, then at
      ``g <= -(ln(S / |last|) + 1) / (tn - tn-1)`` they come to at most
      ``|last| / e``, compounded to the end.

    So a rate lies between the two, whatever the flows, and the bracket
    is no wider than they need. A window's rate can lie far outside any
    fixed range: money paid in two days before a fall is annualised from
    those two days.
    """
    first, final = series[0], series[-1]
    with decimal.localcontext(RETURN_CONTEXT):
        after = sum((abs(m.amount) for m in series[1:]), _ZERO)
        before = sum((abs(m.amount) for m in series[:-1]), _ZERO)
        upper = (_log_ratio(after, first.amount) + _ONE) / (series[1].years - first.years)
        lower = -(_log_ratio(before, -final.amount) + _ONE) / (final.years - series[-2].years)
    return lower, upper


def _log_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    """ln(numerator / denominator), or 0 when that is below 0."""
    return max((numerator / denominator).ln(), _ZERO)


def solve(
    series: Sequence[Moment], *, scope: str, anchor: date, last: date
) -> tuple[Decimal, Solve]:
    """g = ln(1 + R) at which *series* is worth nothing. ADR 0046 §2.

    Newton, a step taken only when it lands inside the bracket
    (:func:`bracket`), which halves otherwise.

    Raises:
        ValidationError: nothing was ever taken out and nothing is left, a
            total loss with no rate; the series does not begin with money
            in, which ADR 0046 §3's proof needs; it does not end with money
            out, so no bracket holds a rate; the arithmetic overflows; or no
            convergence in :data:`STEPS` steps.
    """
    with decimal.localcontext(RETURN_CONTEXT):
        if all(moment.amount >= 0 for moment in series):
            raise ValidationError(
                f"{scope} lost everything put in from {anchor.isoformat()} to "
                f"{last.isoformat()}: no money-weighted rate solves",
                code=E_RETURN_TOTAL_LOSS,
                remedy="The loss is -100%. Nothing is clamped or estimated.",
            )
        first, final = series[0], series[-1]
        if first.amount <= 0:
            raise _not_unique(scope, anchor, last, None, first, first.amount, "is money out")
        if final.amount >= 0:
            raise _no_rate(
                scope,
                anchor,
                last,
                "ends with money in, so no bracket can be shown to hold "
                "a rate: there may be none, or more than one",
            )
        lower, upper = bracket(series)
        try:
            g, newton, bisection, residual = _search(series, lower, upper)
        except decimal.Overflow:
            raise _no_rate(
                scope, anchor, last, f"overflows between g = {lower} and g = {upper}"
            ) from None
        if g is None:
            raise _no_rate(scope, anchor, last, f"did not settle in {STEPS} steps")
    return g, Solve(newton, bisection, lower, upper, residual)


def _search(
    series: Sequence[Moment], lower: Decimal, upper: Decimal
) -> tuple[Decimal | None, int, int, Decimal]:
    """Guarded Newton inside [lower, upper], where the value goes from negative to positive.

    A Newton step is taken only if it lands inside the bracket and is no more
    than half the step before it (Numerical Recipes' rtsafe). Otherwise the
    bracket is halved. Far from the rate the present value is close to an
    exponential, on which Newton's step stays the same size, so without the
    second condition it would crawl.
    """
    low, high = lower, upper
    g = _ZERO
    previous = high - low
    newton = bisection = 0
    for _ in range(STEPS):
        value, slope, gross = _present_value(series, g)
        if abs(value) <= _PV_TOLERANCE * gross:
            return g, newton, bisection, abs(value) / gross
        if value < 0:
            low = g
        else:
            high = g
        scale = max(abs(g), _ONE)
        if high - low <= _G_TOLERANCE * scale:
            return g, newton, bisection, abs(value) / gross
        step = value / slope if slope != 0 else None
        if step is not None and low < g - step < high and 2 * abs(step) <= abs(previous):
            previous = step
            g -= step
            newton += 1
            if abs(step) <= _G_TOLERANCE * scale:
                value, _, gross = _present_value(series, g)
                return g, newton, bisection, abs(value) / gross
        else:
            previous = (high - low) / 2
            g = low + previous
            bisection += 1
    return None, newton, bisection, _ZERO


def _no_rate(scope: str, anchor: date, last: date, why: str) -> ValidationError:
    return ValidationError(
        f"no money-weighted rate for {scope} from {anchor.isoformat()} to "
        f"{last.isoformat()}: its flows' present value {why}",
        code=E_MWR_NO_CONVERGENCE,
        remedy=(
            "A period that begins with money in and ends with its value taken out "
            "always has a rate, and the solver's bracket is computed to hold it "
            "(ADR 0046 §2). Rely on the time-weighted return, or measure a shorter "
            "period."
        ),
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
    :data:`_PV_TOLERANCE` of the gross that made the balance, which counts
    as zero.

    Raises:
        ValidationError: the first amount is not money in, or a balance
            before the end is negative (PT-E-MWR-NOT-UNIQUE).
    """
    with decimal.localcontext(RETURN_CONTEXT):
        first = series[0]
        if first.amount <= 0:
            raise _not_unique(scope, anchor, last, g, first, first.amount, "is money out")
        # The balance, and the gross of what made it, both compounded: the
        # floor for rounding scales with what the balance was made of.
        balance = gross = _ZERO
        lowest: tuple[Decimal, date] | None = None
        previous = first.years
        for moment in series[:-1]:
            growth = (g * (moment.years - previous)).exp()
            balance = balance * growth + moment.amount
            gross = gross * growth + abs(moment.amount)
            previous = moment.years
            if balance < -_PV_TOLERANCE * gross:
                raise _not_unique(scope, anchor, last, g, moment, balance, "falls below zero")
            if lowest is None or balance < lowest[0]:
                lowest = (balance, moment.on)
    assert lowest is not None  # the first amount is in and precedes the end
    return lowest


def _not_unique(
    scope: str,
    anchor: date,
    last: date,
    g: Decimal | None,
    moment: Moment,
    balance: Decimal,
    why: str,
) -> ValidationError:
    if g is None:
        rate = None
        found = "no money-weighted rate"
        how = "its first amount"
    else:
        with decimal.localcontext(RETURN_CONTEXT):
            rate = g.exp() - _ONE
        found = f"the money-weighted rate {rate:.6f}"
        how = "compounded at it, the balance"
    return ValidationError(
        f"{found} for {scope} from {anchor.isoformat()} to {last.isoformat()} cannot "
        f"be shown to be the only one: {how} {why} at the end of "
        f"{moment.on.isoformat()}, at {balance:.2f}",
        code=E_MWR_NOT_UNIQUE,
        remedy=(
            "A withdrawal took out more than the money had grown to at the rate, so "
            "another rate may solve too, and one rate misdescribes the period (ADR 0046 "
            "§3, PORT-GIPS-C04). Rely on the time-weighted return, or measure a period "
            "that ends before the flows named."
        ),
        day=moment.on.isoformat(),
        balance=str(balance),
        rate=str(rate) if rate is not None else None,
        txn_ids=list(moment.txn_ids),
    )
