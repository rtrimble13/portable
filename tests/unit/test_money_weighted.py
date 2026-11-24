"""The money-weighted solve and its proof of a single rate. ADR 0046 §2, §3.

The series are ADR 0046's, from the investor's account side: money in is
positive and money out, the ending value included, negative. Each rate was
worked outside `portable` and is checked here to six places, with the
present value at it checked to zero.
"""

from __future__ import annotations

import decimal
from datetime import date
from decimal import Decimal as D

import pytest

from portable_core.domain.periods import years
from portable_core.errors import PortableError
from portable_core.services.money_weighted import Moment, _require_only_rate, solve
from portable_core.services.returns import RETURN_CONTEXT

pytestmark = [pytest.mark.unit, pytest.mark.gips]


def _series(anchor: date, *amounts: tuple[date, str]) -> list[Moment]:
    out: list[Moment] = []
    for on, amount in amounts:
        length = years(anchor, on)
        with decimal.localcontext(RETURN_CONTEXT):
            span = D(length.whole) + D(length.days) / D(length.of)
        out.append(Moment(on, span, D(amount), (len(out) + 1,)))
    return out


def _rate(series: list[Moment], anchor: date) -> tuple[D, D, date]:
    last = series[-1].on
    span = series[-1].years
    g, found = solve(series, span=span, scope="B", anchor=anchor, last=last)
    lowest, on = _require_only_rate(series, g, scope="B", anchor=anchor, last=last)
    with decimal.localcontext(RETURN_CONTEXT):
        assert found.residual <= D("1e-26")
        return (g.exp() - 1).quantize(D("0.000001")), lowest.quantize(D("1")), on


#: ADR 0046's typical series: a deposit, a withdrawal, another deposit, the value.
TYPICAL = (date(2019, 12, 31), [
    (date(2020, 1, 1), "100000"), (date(2022, 6, 30), "-20000"),
    (date(2024, 3, 15), "30000"), (date(2025, 12, 31), "-180000"),
])  # fmt: skip
#: ADR 0046's drawdown: more taken out than was ever paid in, then paid in again.
DRAWDOWN = (date(2015, 12, 31), [
    (date(2016, 1, 1), "100000"), (date(2020, 1, 2), "-130000"),
    (date(2022, 1, 3), "60000"), (date(2025, 12, 31), "-110000"),
])  # fmt: skip


@pytest.mark.parametrize(
    ("case", "rate", "lowest", "on"),
    [
        (TYPICAL, "0.094829", "100000", date(2020, 1, 1)),
        (DRAWDOWN, "0.095019", "13812", date(2020, 1, 2)),
    ],
    ids=["typical", "drawdown"],
)
def test_port_gips_c04_interleaved_flows_with_one_rate_are_measured(
    case: tuple[date, list[tuple[date, str]]], rate: str, lowest: str, on: date
) -> None:
    """Three sign changes each, which C04 as first written refused. The
    drawdown's running total changes sign three times too, so Norstrøm's
    rule would refuse it. Compounded at the rate, neither balance is ever
    negative, so each rate is the only one."""
    anchor, amounts = case
    assert _rate(_series(anchor, *amounts), anchor) == (D(rate), D(lowest), on)


def test_port_gips_c04_xirr_refuses_pathological_flows() -> None:
    """In 1, out 6, in 11, a value of 6, a year apart. The rates are 0%, 100%
    and 200%: (1 + r)^3 - 6(1 + r)^2 + 11(1 + r) - 6 has roots 1, 2 and 3.
    Whichever is found, the balance at it goes negative after the 6 taken
    out, and the refusal names that withdrawal."""
    anchor = date(2020, 12, 31)
    series = _series(anchor, (date(2020, 12, 31), "1"), (date(2021, 12, 31), "-6"),
                     (date(2022, 12, 31), "11"), (date(2023, 12, 31), "-6"))  # fmt: skip
    with pytest.raises(PortableError) as refused:
        _rate(series, anchor)
    assert refused.value.code == "PT-E-MWR-NOT-UNIQUE"
    assert refused.value.context["day"] == "2021-12-31"
    assert refused.value.context["txn_ids"] == [2]


def test_port_gips_c04_two_rates_are_refused_at_the_bracket() -> None:
    """The textbook series: out 100, in 230, out 132, a year apart, which solves
    at 10% and at 20%. It ends with money in, as no portfolio's window can, so
    its present value has one sign at both ends of the bracket. That is no
    rate in the bracket, or more than one, and it is refused there."""
    anchor = date(2021, 1, 1)
    series = _series(anchor, (date(2021, 1, 1), "100"), (date(2022, 1, 1), "-230"),
                     (date(2023, 1, 1), "132"))  # fmt: skip
    with pytest.raises(PortableError) as refused:
        _rate(series, anchor)
    assert refused.value.code == "PT-E-MWR-NO-CONVERGENCE"
    assert "same sign at both ends" in refused.value.message


def test_a_series_that_starts_with_money_out_cannot_be_shown_to_have_one_rate() -> None:
    """Out 50, then in 60 a year later: a loan's shape, which solves at 20%.
    The proof needs money in first, which every portfolio window has, so it
    is refused rather than presented on a rate the proof does not cover."""
    anchor = date(2024, 1, 1)
    series = _series(anchor, (date(2024, 1, 1), "-50"), (date(2025, 1, 1), "60"))
    g, _ = solve(series, span=series[-1].years, scope="B", anchor=anchor, last=date(2025, 1, 1))
    with decimal.localcontext(RETURN_CONTEXT):
        assert (g.exp() - 1).quantize(D("0.000001")) == D("0.200000")
    with pytest.raises(PortableError) as refused:
        _require_only_rate(series, g, scope="B", anchor=anchor, last=date(2025, 1, 1))
    assert refused.value.code == "PT-E-MWR-NOT-UNIQUE"
    assert "is money out" in refused.value.message


def test_everything_lost_has_no_rate() -> None:
    anchor = date(2024, 1, 1)
    series = _series(anchor, (date(2024, 1, 1), "100"), (date(2024, 6, 1), "50"))
    with pytest.raises(PortableError) as refused:
        _rate(series, anchor)
    assert refused.value.code == "PT-E-RETURN-TOTAL-LOSS"


def test_with_no_flow_the_rate_is_the_growth_annualised_by_anniversary() -> None:
    """100 to 121 over exactly two years, a 29 February among them: 10%."""
    anchor = date(2023, 12, 31)
    series = _series(anchor, (date(2023, 12, 31), "100"), (date(2025, 12, 31), "-121"))
    assert _rate(series, anchor)[0] == D("0.100000")
