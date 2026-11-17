"""The role a new leg takes, from what it holds and the side it opens.

Issue 63: a bond was labelled ``long_stock``, because the role was read from
the trade alone. Every leg-creating site now asks ``leg_role_for``, and these
tests pin each branch of it, so that fixing the bond moved nothing else.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from portable_core.domain.enums import (
    DayCount,
    InstrumentType,
    LegRole,
    OptionRight,
)
from portable_core.domain.models import BondDetail, Instrument, OptionDetail
from portable_core.services.positions import leg_role_for

pytestmark = pytest.mark.unit


def _instrument(kind: InstrumentType, **detail: object) -> Instrument:
    return Instrument(instrument_id=1, symbol="X", instrument_type=kind, **detail)  # type: ignore[arg-type]


def _option(right: OptionRight) -> Instrument:
    return _instrument(
        InstrumentType.OPTION,
        option=OptionDetail(
            underlier_instrument_id=2,
            option_right=right,
            strike=D("100"),
            expiry=date(2025, 1, 17),
            multiplier=D("100"),
        ),
    )


BOND = _instrument(
    InstrumentType.BOND,
    bond=BondDetail(
        issuer="US Treasury",
        coupon_rate=D("0.0425"),
        coupon_frequency=2,
        maturity_date=date(2030, 5, 15),
        day_count=DayCount.ACT_ACT,
        face_value=D("1000"),
    ),
)


def test_a_bond_leg_takes_the_bond_role() -> None:
    assert leg_role_for(BOND, short=False) is LegRole.BOND


@pytest.mark.parametrize(
    ("instrument", "short", "role"),
    [
        (_instrument(InstrumentType.EQUITY), False, LegRole.LONG_STOCK),
        (_instrument(InstrumentType.EQUITY), True, LegRole.SHORT_STOCK),
        (_instrument(InstrumentType.ETF), False, LegRole.LONG_STOCK),
        (_instrument(InstrumentType.MUTUAL_FUND), False, LegRole.LONG_STOCK),
        (_option(OptionRight.CALL), False, LegRole.LONG_CALL),
        (_option(OptionRight.CALL), True, LegRole.SHORT_CALL),
        (_option(OptionRight.PUT), False, LegRole.LONG_PUT),
        (_option(OptionRight.PUT), True, LegRole.SHORT_PUT),
        (_instrument(InstrumentType.OPTION), False, LegRole.OTHER),
    ],
)
def test_every_other_role_is_where_it_was(
    instrument: Instrument, short: bool, role: LegRole
) -> None:
    assert leg_role_for(instrument, short=short) is role
