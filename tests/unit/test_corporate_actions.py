"""Corporate actions and their effect on basis and holding period.

Every trap here changes a tax RATE if got wrong, not merely a presentation.
That is why each has its own test rather than being covered incidentally.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portable_core.decimals import money_context
from portable_core.domain.enums import (
    BasisAdjustmentReason,
    HoldingPeriod,
    LotStatus,
    ReliefMethod,
)
from portable_core.domain.models import Lot
from portable_core.errors import ValidationError
from portable_core.services.corporate_actions import CorporateActionEngine
from portable_core.services.lots import LotEngine

pytestmark = pytest.mark.unit

D = Decimal
CA = CorporateActionEngine()
LOTS_ENGINE = LotEngine()


def lot(
    lot_id: int = 1,
    *,
    open_date: date = date(2023, 1, 10),
    quantity: str = "100",
    basis: str = "10000.00",
) -> Lot:
    qty = D(quantity)
    total = D(basis)
    return Lot(
        lot_id=lot_id,
        leg_id=1,
        position_id=1,
        instrument_id=1,
        account_id=1,
        open_date=open_date,
        open_txn_id=lot_id,
        original_quantity=qty,
        remaining_quantity=qty,
        per_unit_price=total / qty,
        original_cost_basis=total,
        adjusted_cost_basis=total,
        holding_period_start=open_date,
        status=LotStatus.OPEN,
    )


# ── splits ───────────────────────────────────────────────────────────────────


def test_a_split_multiplies_quantity_and_leaves_total_basis_alone() -> None:
    """The whole economic content of a split: same claim, divided differently."""
    result = CA.split([lot()], numerator=D(3), denominator=D(1), ex_date=date(2024, 6, 1))
    (split_lot,) = result.lots

    assert split_lot.remaining_quantity == D("300")
    assert split_lot.adjusted_cost_basis == D("10000.00"), "total basis is unchanged"
    # Compared under the portable context: the engine computes at 34 digits
    # and the ambient default is 28, so an unqualified division here would
    # disagree in the last place for a reason that has nothing to do with
    # splits.
    with money_context():
        assert split_lot.per_unit_price == D("10000.00") / D("300")


def test_a_split_does_not_reset_the_holding_period() -> None:
    """The trap. Resetting it turns a long-term gain into a short-term one.

    That is a rate error, not a rounding, and it will not look wrong on the
    report -- which is why it is asserted on the lot AND on the adjustment row,
    so the assertion is auditable after the fact rather than implicit.
    """
    original = lot(open_date=date(2023, 1, 10))
    result = CA.split([original], numerator=D(3), denominator=D(1), ex_date=date(2024, 6, 1))
    (split_lot,) = result.lots
    (adjustment,) = result.adjustments

    assert split_lot.holding_period_start == date(2023, 1, 10)
    assert adjustment.holding_period_start_after == date(2023, 1, 10)
    assert adjustment.basis_delta == D("0")
    assert adjustment.reason is BasisAdjustmentReason.SPLIT

    # And the consequence that actually matters: a sale shortly after the split
    # is still long-term.
    plan = LOTS_ENGINE.select(list(result.lots), D("300"), ReliefMethod.FIFO, date(2024, 7, 1))
    assert plan.consumptions[0].holding_period is HoldingPeriod.LONG


def test_a_reverse_split_reduces_quantity_and_leaves_total_basis_alone() -> None:
    result = CA.split([lot()], numerator=D(1), denominator=D(10), ex_date=date(2024, 6, 1))
    (split_lot,) = result.lots
    assert split_lot.remaining_quantity == D("10")
    assert split_lot.adjusted_cost_basis == D("10000.00")
    assert result.adjustments[0].reason is BasisAdjustmentReason.REVERSE_SPLIT


def test_a_split_and_its_inverse_leave_basis_and_quantity_unchanged() -> None:
    """A property the bootstrap names explicitly."""
    original = lot()
    forward = CA.split([original], numerator=D(3), denominator=D(1), ex_date=date(2024, 6, 1))
    back = CA.split(
        list(forward.lots), numerator=D(1), denominator=D(3), ex_date=date(2024, 6, 2)
    )
    (restored,) = back.lots

    assert restored.remaining_quantity == original.remaining_quantity
    assert restored.adjusted_cost_basis == original.adjusted_cost_basis
    assert restored.holding_period_start == original.holding_period_start


def test_a_split_reports_the_fraction_rather_than_rounding_it_away() -> None:
    """Cash in lieu is a taxable disposition, not a rounding.

    A 3-for-2 split of 101 shares yields 151.5. Swallowing the half hides a
    taxable event; reporting it lets the caller record the cash in lieu.
    """
    result = CA.split(
        [lot(quantity="101", basis="10100.00")],
        numerator=D(3),
        denominator=D(2),
        ex_date=date(2024, 6, 1),
        allows_fractional=False,
    )
    assert result.lots[0].remaining_quantity == D("151")
    assert result.fractional_shares == D("0.5")


def test_a_split_ratio_must_be_positive() -> None:
    for numerator, denominator in [(D(0), D(1)), (D(1), D(0)), (D(-3), D(1))]:
        with pytest.raises(ValidationError):
            CA.split(
                [lot()], numerator=numerator, denominator=denominator, ex_date=date(2024, 6, 1)
            )


# ── spinoffs ─────────────────────────────────────────────────────────────────


def test_spinoff_allocates_basis_by_relative_fair_market_value() -> None:
    """Not by share count, and not by the spinoff ratio.

    Parent worth $90, spun worth $10 immediately after: 90% of the original
    basis stays with the parent.
    """
    result = CA.spinoff(
        [lot(quantity="100", basis="10000.00")],
        ratio=D("0.5"),
        parent_fmv=D("90.00"),
        spun_fmv=D("20.00"),
        ex_date=date(2024, 6, 1),
        spun_instrument_id=2,
        spun_leg_id=2,
        spun_position_id=2,
    )
    # parent value 100 x 90 = 9000; spun value 50 x 20 = 1000; total 10000
    # so 90% / 10% of the $10,000 basis.
    assert result.parent_lots[0].adjusted_cost_basis == D("9000.00")
    assert result.spun_lots[0].adjusted_cost_basis == D("1000.00")
    assert result.parent_lots[0].adjusted_cost_basis + result.spun_lots[
        0
    ].adjusted_cost_basis == D("10000.00"), "no basis is created or destroyed"


def test_spun_shares_inherit_the_parents_holding_period() -> None:
    """They are not newly acquired.

    A spinoff from a five-year-old lot produces long-term spun shares on day
    one. Dating them from the ex-date would make an immediate sale short-term.
    """
    result = CA.spinoff(
        [lot(open_date=date(2019, 1, 10), quantity="100", basis="10000.00")],
        ratio=D("0.5"),
        parent_fmv=D("90.00"),
        spun_fmv=D("20.00"),
        ex_date=date(2024, 6, 1),
        spun_instrument_id=2,
        spun_leg_id=2,
        spun_position_id=2,
    )
    spun = result.spun_lots[0]
    assert spun.holding_period_start == date(2019, 1, 10)
    assert spun.open_date == date(2024, 6, 1), "acquired on the ex-date"

    plan = LOTS_ENGINE.select(
        [spun], spun.remaining_quantity, ReliefMethod.FIFO, date(2024, 6, 2)
    )
    assert plan.consumptions[0].holding_period is HoldingPeriod.LONG


def test_spinoff_with_no_observable_values_is_refused_not_guessed() -> None:
    """An arbitrary allocation is wrong on both sides and will not look wrong."""
    with pytest.raises(ValidationError) as exc:
        CA.spinoff(
            [lot()],
            ratio=D("0.5"),
            parent_fmv=D("0"),
            spun_fmv=D("0"),
            ex_date=date(2024, 6, 1),
            spun_instrument_id=2,
            spun_leg_id=2,
            spun_position_id=2,
        )
    assert "Form 8937" in (exc.value.remedy or "")


# ── return of capital ────────────────────────────────────────────────────────


def test_return_of_capital_reduces_basis_rather_than_being_income() -> None:
    lots, adjustments, excess = CA.return_of_capital(
        [lot(quantity="100", basis="10000.00")],
        amount_per_share=D("2.00"),
        pay_date=date(2024, 6, 1),
    )
    assert lots[0].adjusted_cost_basis == D("9800.00")
    assert excess == D("0.00")
    assert adjustments[0].basis_delta == D("-200.00")
    assert adjustments[0].reason is BasisAdjustmentReason.RETURN_OF_CAPITAL


def test_return_of_capital_beyond_basis_becomes_capital_gain() -> None:
    """There is no negative basis. The excess is recognised immediately."""
    lots, adjustments, excess = CA.return_of_capital(
        [lot(quantity="100", basis="150.00")],
        amount_per_share=D("2.00"),
        pay_date=date(2024, 6, 1),
    )
    assert lots[0].adjusted_cost_basis == D("0.00")
    assert excess == D("50.00"), "200 distributed against 150 of basis"
    assert "exceeded basis" in (adjustments[0].note or "")


def test_return_of_capital_does_not_reset_the_holding_period() -> None:
    _lots, adjustments, _excess = CA.return_of_capital(
        [lot(open_date=date(2020, 1, 1))],
        amount_per_share=D("1.00"),
        pay_date=date(2024, 6, 1),
    )
    assert adjustments[0].holding_period_start_after == date(2020, 1, 1)


# ── option premium ───────────────────────────────────────────────────────────


def test_exercised_long_call_premium_goes_into_the_stocks_basis() -> None:
    """The option produces no independent P&L; its cost becomes the stock's."""
    basis = CA.premium_on_exercise(
        stock_basis=D("10000.00"), premium_paid=D("350.00"), fees=D("1.00")
    )
    assert basis == D("10351.00")


def test_assigned_written_call_premium_goes_into_the_stocks_proceeds() -> None:
    """Not a separate short-term gain.

    Treating it as one both double-counts the premium and applies the wrong
    holding period, because the stock's own holding period governs the sale.
    """
    proceeds = CA.premium_on_assignment(
        stock_proceeds=D("11000.00"), premium_received=D("420.00"), fees=D("1.00")
    )
    assert proceeds == D("11419.00")


def test_a_written_option_expiring_worthless_is_always_short_term() -> None:
    """However long it was open. There is no long-term treatment for a lapse."""
    gain, period = CA.premium_on_expiration(D("420.00"))
    assert gain == D("420.00")
    assert period is HoldingPeriod.SHORT


# ── fractional shares ────────────────────────────────────────────────────────


def test_a_fractional_share_an_account_cannot_hold_is_refused() -> None:
    with pytest.raises(ValidationError) as exc:
        CA.require_whole_shares(
            D("151.5"),
            allows_fractional=False,
            instrument_symbol="ACME",
            action="3-for-2 split",
        )
    assert exc.value.code == "PT-E-FRACTIONAL-SHARE"
    assert "cash in lieu" in (exc.value.remedy or "")

    CA.require_whole_shares(
        D("151.5"), allows_fractional=True, instrument_symbol="ACME", action="split"
    )
    CA.require_whole_shares(
        D("151"), allows_fractional=False, instrument_symbol="ACME", action="split"
    )
