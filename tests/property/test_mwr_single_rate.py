"""A money-weighted rate is returned only when it is the only one. ADR 0046 §3.

Flow series are generated from an account whose value stays positive: an
opening deposit, then steps of growth or loss, each followed by a deposit or
a withdrawal of part of what is there, then the value taken out at the end.

- Every series gives a rate whose present value is zero, compounded at which
  no balance before the end is negative, or it is refused. None gives a rate
  that fails the test (PORT-GIPS-C04).
- A series of deposits alone is always measured: its balances are sums of
  money in, compounded.
- Where a rate is given, the present value changes sign once across the
  whole bracket, as the proof says it must.
"""

from __future__ import annotations

import decimal
from datetime import date, timedelta
from decimal import Decimal as D
from itertools import pairwise

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from portable_core.domain.periods import years
from portable_core.errors import PortableError
from portable_core.services.money_weighted import (
    BRACKET_GROWTH,
    Moment,
    _present_value,
    _require_only_rate,
    solve,
)
from portable_core.services.returns import RETURN_CONTEXT

pytestmark = [pytest.mark.property, pytest.mark.unit, pytest.mark.gips]

ANCHOR = date(2020, 12, 31)

#: One step: days later, the growth in percent, then the flow as a percent of
#: the value: positive paid in, negative taken out, never all of it.
_STEP = st.tuples(st.integers(1, 400), st.integers(-40, 60), st.integers(-90, 150))


def _series(opening: int, steps: list[tuple[int, int, int]]) -> list[Moment]:
    with decimal.localcontext(RETURN_CONTEXT):
        value = D(opening)
        on = ANCHOR
        amounts = [(on, D(opening))]
        for days, growth, flow in steps:
            on += timedelta(days=days)
            value *= 1 + D(growth) / 100
            moved = (value * D(flow) / 100).quantize(D("0.01"))
            value += moved
            if moved:
                amounts.append((on, moved))
        amounts.append((on + timedelta(days=1), -value.quantize(D("0.01"))))
        out = []
        for when, amount in amounts:
            length = years(ANCHOR, when)
            span = D(length.whole) + D(length.days) / D(length.of)
            out.append(Moment(when, span, amount, ()))
        return out


def _outcome(series: list[Moment]) -> tuple[D, D] | str:
    last = series[-1].on
    try:
        g, _ = solve(series, span=series[-1].years, scope="B", anchor=ANCHOR, last=last)
        lowest, _ = _require_only_rate(series, g, scope="B", anchor=ANCHOR, last=last)
    except PortableError as refused:
        return refused.code
    return g, lowest


@settings(max_examples=150, deadline=None)
@given(st.integers(1_000, 1_000_000), st.lists(_STEP, min_size=1, max_size=8))
def test_port_gips_c04_a_rate_is_returned_only_if_it_is_the_only_one(
    opening: int, steps: list[tuple[int, int, int]]
) -> None:
    series = _series(opening, steps)
    outcome = _outcome(series)
    if isinstance(outcome, str):
        assert outcome in {"PT-E-MWR-NOT-UNIQUE", "PT-E-MWR-NO-CONVERGENCE"}
        return
    g, lowest = outcome
    assert lowest >= 0
    with decimal.localcontext(RETURN_CONTEXT):
        gross = sum((abs(m.amount) for m in series), D(0))
        assert abs(_present_value(series, g)[0]) <= D("1e-26") * gross
        # One change of sign across the bracket, on a grid of 400 points.
        span = series[-1].years
        signs = []
        for k in range(401):
            point = -BRACKET_GROWTH / span + 2 * BRACKET_GROWTH / span * D(k) / 400
            value = _present_value(series, point)[0]
            if value != 0:
                signs.append(value > 0)
        assert sum(1 for a, b in pairwise(signs) if a != b) == 1


@settings(max_examples=100, deadline=None)
@given(
    st.integers(1_000, 1_000_000),
    st.lists(
        st.tuples(st.integers(1, 400), st.integers(-40, 60), st.integers(0, 150)),
        min_size=1,
        max_size=8,
    ),
)
def test_deposits_alone_are_always_measured(
    opening: int, steps: list[tuple[int, int, int]]
) -> None:
    outcome = _outcome(_series(opening, steps))
    assert not isinstance(outcome, str), outcome
