"""A bond's schedule, accrual and quote basis. ADR 0041, PORT-GIPS-A06.

Each figure is worked by hand in its test. The schedule steps back from
maturity; quantity is face amount.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from portable_core.decimals import quantize_money
from portable_core.domain import bonds
from portable_core.domain.enums import DayCount, InstrumentType
from portable_core.domain.models import BondDetail, Instrument

pytestmark = pytest.mark.unit


def _bond(**overrides: object) -> BondDetail:
    fields: dict[str, object] = {
        "issuer": "US Treasury",
        "coupon_rate": D("0.0425"),
        "coupon_frequency": 2,
        "maturity_date": date(2030, 5, 15),
        "day_count": DayCount.ACT_ACT,
        "face_value": D("1000"),
    }
    fields |= overrides
    return BondDetail(**fields)  # type: ignore[arg-type]


def test_the_schedule_steps_back_from_maturity() -> None:
    period = bonds.coupon_period(_bond(), date(2025, 6, 30))
    assert period == bonds.CouponPeriod(last=date(2025, 5, 15), next=date(2025, 11, 15))


def test_a_coupon_date_starts_a_period_and_accrues_nothing() -> None:
    period = bonds.coupon_period(_bond(), date(2025, 11, 15))
    assert period == bonds.CouponPeriod(last=date(2025, 11, 15), next=date(2026, 5, 15))
    assert bonds.accrued_interest(_bond(), D("10000"), date(2025, 11, 15)) == 0


def test_a_month_end_maturity_keeps_every_coupon_on_a_month_end() -> None:
    """2030-08-31 semi-annual: coupons on 02-28 / 02-29 and 08-31, never 02-31."""
    bond = _bond(maturity_date=date(2030, 8, 31))
    assert bonds.coupon_period(bond, date(2028, 3, 1)) == bonds.CouponPeriod(
        last=date(2028, 2, 29), next=date(2028, 8, 31)
    )
    assert bonds.coupon_period(bond, date(2027, 2, 27)) == bonds.CouponPeriod(
        last=date(2026, 8, 31), next=date(2027, 2, 28)
    )


@pytest.mark.gips
def test_act_act_accrues_actual_days_over_the_actual_period() -> None:
    """PORT-GIPS-A06. 46 of 184 days: 212.50 x 46 / 184 = 53.125.

    Across 1 January the ISDA year fraction would split the period by year;
    a bond does not (ICMA).
    """
    assert bonds.accrued_interest(_bond(), D("10000"), date(2025, 6, 30)) == D("53.125")
    across = bonds.accrued_interest(_bond(), D("10000"), date(2026, 1, 15))
    # 2025-11-15 to 2026-05-15 is 181 days: 212.50 x 61 / 181 = 71.616...
    assert quantize_money(across) == D("71.62")


def test_thirty_360_accrues_the_annual_rate_over_its_year_fraction() -> None:
    """5% on 100,000 from 03-01 to 04-16: 45 days of 360 = 625.00."""
    bond = _bond(coupon_rate=D("0.05"), maturity_date=date(2030, 9, 1),
                 day_count=DayCount.THIRTY_360)  # fmt: skip
    assert bonds.accrued_interest(bond, D("100000"), date(2025, 4, 16)) == D("625")


def test_a_zero_coupon_bond_and_a_matured_one_accrue_nothing() -> None:
    zero = _bond(coupon_rate=D("0"), coupon_frequency=0)
    assert bonds.accrued_interest(zero, D("10000"), date(2025, 6, 30)) == 0
    assert bonds.coupon_period(_bond(), date(2030, 5, 15)) is None


def test_the_first_period_is_refused_rather_than_guessed() -> None:
    """The dated date that period accrues from is not stored (ADR 0041 §3)."""
    bond = _bond(first_coupon_date=date(2025, 11, 15))
    with pytest.raises(bonds.OddFirstPeriod):
        bonds.accrued_interest(bond, D("10000"), date(2025, 9, 1))
    assert bonds.accrued_interest(bond, D("10000"), date(2025, 11, 16)) > 0


def test_a_first_coupon_off_the_schedule_is_refused() -> None:
    with pytest.raises(bonds.OddFirstPeriod):
        bonds.coupon_period(_bond(first_coupon_date=date(2025, 12, 1)), date(2026, 1, 15))


def test_the_coupon_a_payment_is_for() -> None:
    """A coupon due on Saturday 2025-11-15 and paid on Monday is for the 15th."""
    assert bonds.last_coupon_on_or_before(_bond(), date(2025, 11, 17)) == date(2025, 11, 15)
    assert bonds.last_coupon_on_or_before(_bond(), date(2030, 5, 15)) == date(2030, 5, 15)


@pytest.mark.parametrize(
    ("basis", "price", "value"),
    [("percent_of_par", "99.50", "24875.00"), ("price_per_bond", "995.00", "24875.00")],
)
def test_the_quote_basis_decides_what_a_price_is_worth(
    basis: str, price: str, value: str
) -> None:
    """ADR 0041 §1. $25,000 of face, quoted either way, is the same 24,875."""
    instrument = Instrument(
        instrument_id=1, symbol="T", instrument_type=InstrumentType.BOND,
        bond=_bond(quote_basis=basis),
    )  # fmt: skip
    assert quantize_money(instrument.value_of(D("25000"), D(price))) == D(value)
    assert instrument.price_of(D("25000"), D(value)) == D(price)


def test_a_stock_is_still_price_times_quantity() -> None:
    stock = Instrument(instrument_id=1, symbol="X", instrument_type=InstrumentType.EQUITY)
    assert stock.value_of(D("10"), D("12.34")) == D("123.40")


def test_a_whole_denomination() -> None:
    assert _bond().is_whole_denomination(D("25000"))
    assert not _bond().is_whole_denomination(D("25500"))
