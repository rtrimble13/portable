"""The Decimal boundary. ADR 0005, CLAUDE.md invariant 1.

Every money figure in `portable` passes through this module, so these are the
tests that catch a whole class of wrong numbers before it starts.
"""

from __future__ import annotations

import decimal
from decimal import Decimal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from portable_core.decimals import (
    CENT,
    PORTABLE_CONTEXT,
    allocate,
    from_text,
    is_whole,
    money_context,
    quantize_money,
    to_text,
)

pytestmark = pytest.mark.unit

MONEY = st.decimals(
    min_value=Decimal("-1e12"),
    max_value=Decimal("1e12"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
)


# ── canonical text ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", "0"),
        ("0.00", "0.00"),
        ("-0.00", "0.00"),  # negative zero is not a distinct amount
        ("1E+2", "100"),  # str() would give '1E+2', which sorts wrongly
        ("1E-4", "0.0001"),
        ("10.500", "10.500"),  # trailing zeros carry the quote's significance
        ("-1234.56", "-1234.56"),
        ("1e30", "1000000000000000000000000000000"),
    ],
)
def test_canonical_text_form(value: str, expected: str) -> None:
    assert to_text(Decimal(value)) == expected


def test_scientific_notation_never_reaches_storage() -> None:
    """`str(Decimal)` produces exponents; text-sorted columns would break."""
    assert "E" not in to_text(Decimal("1E+20"))
    assert "e" not in to_text(Decimal("1e-20"))


@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_non_finite_values_are_refused_on_the_way_in_and_out(bad: str) -> None:
    """Neither is an amount of money; storing one defers the failure to a reader."""
    with pytest.raises(ValueError, match="finite"):
        to_text(Decimal(bad))
    with pytest.raises(ValueError, match="finite"):
        from_text(bad)


def test_float_is_refused_by_type_not_coerced() -> None:
    """The guard that catches the mistake this whole repository is built around."""
    with pytest.raises(TypeError, match="expected Decimal"):
        to_text(0.1)  # type: ignore[arg-type]


def test_garbage_text_is_a_value_error_not_a_decimal() -> None:
    with pytest.raises(ValueError, match="not a decimal"):
        from_text("about a hundred")


@given(MONEY)
def test_text_round_trip_is_exact(value: Decimal) -> None:
    """Storage must not lose a digit, including trailing zeros."""
    assert from_text(to_text(value)) == value
    assert to_text(from_text(to_text(value))) == to_text(value)


# ── context ──────────────────────────────────────────────────────────────────


def test_context_traps_the_errors_that_mean_something_went_wrong() -> None:
    assert decimal.InvalidOperation in PORTABLE_CONTEXT.traps
    assert decimal.DivisionByZero in PORTABLE_CONTEXT.traps
    assert decimal.Overflow in PORTABLE_CONTEXT.traps


def test_context_does_not_trap_inexact() -> None:
    """Dividing a dollar amount by a share count is legitimately inexact.

    Trapping Inexact would make correct code raise, which is why ADR 0005
    draws the line where it does.
    """
    with money_context():
        assert Decimal("10.00") / Decimal("3") is not None


def test_context_rounds_half_even() -> None:
    """The valuation default. Half-up would bias every rounded total upward."""
    assert quantize_money(Decimal("0.125")) == Decimal("0.12")
    assert quantize_money(Decimal("0.135")) == Decimal("0.14")


def test_precision_survives_a_realistic_position() -> None:
    """34 digits is not a guess: it must hold quantity times price exactly.

    A million shares at six decimal places, times a price at six decimal
    places, is 24 significant digits -- well inside the context, and the exact
    product is what comes back rather than a rounded one.
    """
    quantity = Decimal("1000000.123456")
    price = Decimal("98765.432109")
    with money_context():
        result = quantity * price

    exact = quantity * price  # under the default context, for comparison
    assert result == exact, "the portable context must not round a realistic product"
    assert len(result.as_tuple().digits) <= PORTABLE_CONTEXT.prec


# ── allocation ───────────────────────────────────────────────────────────────


def test_allocation_of_a_hundred_across_three_loses_no_cent() -> None:
    """The bug this function exists to prevent.

    Rounding each share independently gives three amounts of 33.33 and a
    missing cent. That cent is a basis error, and a basis error becomes a
    realized gain error and then a tax figure error.
    """
    parts = allocate(Decimal("100.00"), [Decimal(1)] * 3)
    assert sum(parts) == Decimal("100.00")
    assert sorted(parts) == [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")]


def test_allocation_is_proportional_where_it_divides_evenly() -> None:
    parts = allocate(Decimal("10.00"), [Decimal(1), Decimal(2), Decimal(7)])
    assert parts == [Decimal("1.00"), Decimal("2.00"), Decimal("7.00")]


def test_allocation_of_a_negative_total_still_sums_exactly() -> None:
    parts = allocate(Decimal("-100.00"), [Decimal(1)] * 3)
    assert sum(parts) == Decimal("-100.00")


def test_allocation_is_deterministic_under_ties() -> None:
    """CLAUDE.md invariant 6 reaches down to here: ties break on index."""
    first = allocate(Decimal("100.00"), [Decimal(1)] * 3)
    for _ in range(20):
        assert allocate(Decimal("100.00"), [Decimal(1)] * 3) == first


@given(
    st.decimals(
        min_value=Decimal("-1e6"),
        max_value=Decimal("1e6"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    ),
    st.lists(
        st.decimals(
            min_value=Decimal("0"),
            max_value=Decimal("1000"),
            places=4,
            allow_nan=False,
            allow_infinity=False,
        ),
        min_size=1,
        max_size=12,
    ),
)
def test_allocation_always_preserves_the_total(total: Decimal, weights: list[Decimal]) -> None:
    assume(sum(weights) > 0)  # the zero case is a documented refusal, tested below
    parts = allocate(total, weights)
    assert len(parts) == len(weights)
    assert sum(parts) == quantize_money(total)


@pytest.mark.parametrize(
    ("weights", "match"),
    [
        ([], "empty"),
        ([Decimal(1), Decimal(-1)], "non-negative"),
        ([Decimal(0), Decimal(0)], "sum to zero"),
    ],
)
def test_allocation_refuses_rather_than_guesses(weights: list[Decimal], match: str) -> None:
    """CLAUDE.md invariant 9: an unanswerable split is an error, not a default."""
    with pytest.raises(ValueError, match=match):
        allocate(Decimal("100.00"), weights)


# ── quantities ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "whole"),
    [("100", True), ("100.000", True), ("100.5", False), ("-3", True), ("0.000001", False)],
)
def test_is_whole(value: str, whole: bool) -> None:
    """Used where a fraction is an error rather than a rounding: option contracts."""
    assert is_whole(Decimal(value)) is whole


def test_cent_is_the_storage_quantum() -> None:
    assert Decimal("0.01") == CENT
