"""A year counted by anniversary, the fiscal year end, and annualising. ADR 0045 §2, §5.

Every case is a calendar boundary somebody has got wrong. The figures are
worked by hand, and the leap-year ones are the point.
"""

from __future__ import annotations

import decimal
from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from portable_core.domain.dates import is_long_term
from portable_core.domain.periods import (
    FiscalYearEnd,
    Years,
    at_least_a_year,
    last_month_end_before,
    shift_years,
    years,
)
from portable_core.errors import GipsRefusalError, ValidationError
from portable_core.services.periods import annualize

pytestmark = pytest.mark.unit


# ── the anniversary ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("day", "count", "expected"),
    [
        (date(2024, 2, 29), 1, date(2025, 2, 28)),  # a month end stays one
        (date(2023, 2, 28), 1, date(2024, 2, 29)),  # ...in both directions
        (date(2025, 2, 28), -1, date(2024, 2, 29)),
        (date(2024, 2, 28), 1, date(2025, 2, 28)),  # not a month end in 2024
        (date(2024, 3, 15), 1, date(2025, 3, 15)),
        (date(2024, 1, 30), 1, date(2025, 1, 30)),
        (date(2024, 4, 30), -3, date(2021, 4, 30)),
        (date(2024, 12, 31), -10, date(2014, 12, 31)),
    ],
)
def test_a_year_runs_month_end_to_month_end(day: date, count: int, expected: date) -> None:
    assert shift_years(day, count) == expected


def test_the_return_anniversary_is_not_the_tax_anniversary() -> None:
    """Deliberately different, and pinned so nobody makes them agree.

    A return period runs month end to month end, so the end of February 2023
    goes to the end of February 2024. A holding period runs calendar date to
    calendar date. Long-term needs more than one year, so a lot bought on
    2023-02-28 and sold on 2024-02-29 is long-term. Using the return rule
    there would make it short, and move a tax boundary by a day.
    """
    assert shift_years(date(2023, 2, 28), 1) == date(2024, 2, 29)
    assert is_long_term(date(2023, 2, 28), date(2024, 2, 29))


@pytest.mark.gips
@pytest.mark.parametrize(
    ("after", "through", "expected"),
    [
        (date(2023, 12, 31), date(2024, 12, 30), False),  # 365 days, across 29 Feb
        (date(2023, 12, 31), date(2024, 12, 31), True),  # 366 days: one year
        (date(2024, 12, 31), date(2025, 12, 31), True),  # 365 days: one year
        (date(2025, 1, 1), date(2025, 12, 31), False),  # 364 days
        (date(2023, 2, 28), date(2024, 2, 28), False),  # month end to a day before one
        (date(2023, 2, 28), date(2024, 2, 29), True),
        (date(2024, 2, 29), date(2025, 2, 28), True),
        (date(2023, 3, 15), date(2024, 3, 14), False),  # 365 days, across 29 Feb
    ],
)
def test_port_gips_b07_a_year_is_an_anniversary_not_365_days(
    after: date, through: date, expected: bool
) -> None:
    assert at_least_a_year(after, through) is expected


# ── the length of a period ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("after", "through", "expected"),
    [
        # Three years exactly, holding 2024-02-29: 1,096 days, and 3.
        (date(2021, 12, 31), date(2024, 12, 31), Years(3, 0, 365)),
        # Two anniversaries to 2024-03-14, then 108 days of a 365-day year.
        (date(2022, 3, 14), date(2024, 6, 30), Years(2, 108, 365)),
        # A day short of a year, measured against the 366 days it falls in.
        (date(2023, 12, 31), date(2024, 12, 30), Years(0, 365, 366)),
        (date(2024, 6, 30), date(2024, 6, 30), Years(0, 0, 365)),
    ],
)
def test_a_period_counts_whole_anniversaries_then_the_year_it_ends_in(
    after: date, through: date, expected: Years
) -> None:
    assert years(after, through) == expected


@given(
    after=st.dates(min_value=date(1990, 1, 1), max_value=date(2060, 12, 31)),
    span=st.integers(min_value=0, max_value=20_000),
)
def test_the_whole_years_are_exactly_the_anniversaries_passed(after: date, span: int) -> None:
    through = after + timedelta(days=span)
    length = years(after, through)
    assert shift_years(after, length.whole) <= through < shift_years(after, length.whole + 1)
    assert 0 <= length.days < length.of
    assert at_least_a_year(after, through) is (length.whole >= 1)


def test_a_period_cannot_end_before_it_starts() -> None:
    with pytest.raises(ValueError, match="before it starts"):
        years(date(2024, 6, 30), date(2024, 6, 29))


# ── the fiscal year end (PORT-GIPS-A04) ──────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "month", "leap_year_end"),
    [
        ("12-31", 12, date(2024, 12, 31)),
        ("06-30", 6, date(2024, 6, 30)),
        ("09-30", 9, date(2024, 9, 30)),
        ("02-28", 2, date(2024, 2, 29)),  # the end of February, in every year
    ],
)
def test_a_fiscal_year_ends_at_a_month_end(text: str, month: int, leap_year_end: date) -> None:
    fiscal = FiscalYearEnd.parse(text)
    assert fiscal.month == month
    assert str(fiscal) == text
    assert fiscal.in_year(2024) == leap_year_end
    assert fiscal.in_year(2023).month == month


@pytest.mark.parametrize(
    ("text", "says"),
    [
        ("06-15", "not the last day of its month"),
        ("04-31", "not the last day of its month"),
        ("02-29", "02-28 means the last day of February"),
        ("13-31", "has no month 13"),
        ("6-30", "is not MM-DD"),
        ("banana", "is not MM-DD"),
        ("", "is not MM-DD"),
    ],
)
def test_anything_else_is_refused_by_name(text: str, says: str) -> None:
    with pytest.raises(ValueError, match=says):
        FiscalYearEnd.parse(text)


# ── annualising ──────────────────────────────────────────────────────────────


@pytest.mark.gips
def test_annualised_years_count_anniversaries() -> None:
    """ADR 0045 §5. Over the three years to 2024-12-31, which hold a 29
    February, 33.1% cumulative is 1.1 cubed: 10% a year, with N = 3 exactly.
    Counting 1,096 days over 365 would give N = 3.0027 and 9.99%."""
    rate = annualize(D("0.331"), after=date(2021, 12, 31), through=date(2024, 12, 31))
    assert abs(rate - D("0.1")) < D("1e-30")


@pytest.mark.gips
def test_one_year_annualised_is_the_cumulative_return_exactly() -> None:
    """Not merely close: through exp and ln it could differ in the last digit,
    and a one-year row would then show two numbers for one."""
    cumulative = D("0.084213977412650193880145230175")
    assert annualize(cumulative, after=date(2023, 12, 31), through=date(2024, 12, 31)) == (
        cumulative
    )


@pytest.mark.gips
def test_a_part_year_is_measured_against_the_year_it_falls_in() -> None:
    """Since an inception on 2022-03-15, to 2024-06-30: N = 2 + 108/365. The
    rate is the one that, compounded over N years, gives the cumulative back."""
    cumulative = D("0.21")
    rate = annualize(cumulative, after=date(2022, 3, 14), through=date(2024, 6, 30))
    with decimal.localcontext(decimal.Context(prec=50)):
        count = D(2) + D(108) / D(365)
        assert abs(((1 + rate).ln() * count).exp() - (1 + cumulative)) < D("1e-30")
    assert D("0.08") < rate < D("0.09")


@pytest.mark.gips
def test_port_gips_b07_a_period_under_a_year_is_never_annualised() -> None:
    with pytest.raises(GipsRefusalError) as refused:
        annualize(D("0.05"), after=date(2023, 12, 31), through=date(2024, 12, 30))
    assert refused.value.code == "PT-E-GIPS-ANNUALIZE-SUB-YEAR"
    assert refused.value.context["requirement"] == "PORT-GIPS-B07"


@pytest.mark.parametrize("cumulative", [D("-1"), D("-1.2")])
def test_a_total_loss_has_no_annualised_rate(cumulative: D) -> None:
    with pytest.raises(ValidationError) as refused:
        annualize(cumulative, after=date(2020, 12, 31), through=date(2024, 12, 31))
    assert refused.value.code == "PT-E-RETURN-TOTAL-LOSS"


# ── period boundaries (ADR 0045 §2) ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2025, 12, 31), date(2025, 11, 30)),  # a month end: the month before
        (date(2025, 12, 20), date(2025, 11, 30)),
        (date(2024, 3, 1), date(2024, 2, 29)),
    ],
)
def test_the_month_to_date_starts_after_the_last_month_end_before(
    day: date, expected: date
) -> None:
    assert last_month_end_before(day) == expected


@pytest.mark.parametrize(
    ("text", "day", "quarter", "year"),
    [
        ("12-31", date(2025, 12, 31), date(2025, 9, 30), date(2024, 12, 31)),
        ("12-31", date(2025, 11, 15), date(2025, 9, 30), date(2024, 12, 31)),
        ("06-30", date(2025, 12, 31), date(2025, 9, 30), date(2025, 6, 30)),
        ("06-30", date(2025, 6, 30), date(2025, 3, 31), date(2024, 6, 30)),
        ("01-31", date(2025, 3, 15), date(2025, 1, 31), date(2025, 1, 31)),
        ("02-28", date(2024, 3, 1), date(2024, 2, 29), date(2024, 2, 29)),
    ],
)
def test_quarters_and_years_step_back_from_the_fiscal_year_end(
    text: str, day: date, quarter: date, year: date
) -> None:
    """PORT-GIPS-A04: strictly before the day, so a table ending on a year end
    has the whole year as its year to date."""
    fiscal = FiscalYearEnd.parse(text)
    assert fiscal.last_quarter_end_before(day) == quarter
    assert fiscal.last_end_before(day) == year


def test_a_fiscal_year_ending_in_january_has_quarters_out_of_step_with_the_calendar() -> None:
    assert FiscalYearEnd.parse("01-31").quarter_months() == frozenset({1, 4, 7, 10})
    assert FiscalYearEnd.parse("06-30").quarter_months() == frozenset({3, 6, 9, 12})


def test_annualising_reads_its_own_context_not_the_threads() -> None:
    """At six digits the thread could not carry the rate. RETURN_CONTEXT does."""
    with decimal.localcontext(decimal.Context(prec=6)):
        narrow = annualize(D("0.331"), after=date(2021, 12, 31), through=date(2024, 12, 31))
    assert narrow == annualize(D("0.331"), after=date(2021, 12, 31), through=date(2024, 12, 31))
    assert len(narrow.as_tuple().digits) > 20
