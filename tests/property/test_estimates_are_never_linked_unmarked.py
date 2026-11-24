"""An estimated sub-period is never linked into an unmarked result. ADR 0044 §3.

A result is estimated if any sub-period linked into it is. The rule lives in
one place, where a linked result is made (`TimeWeightedReturn`), so no call
site can link an estimate into a measurement. `PORT-GIPS-J04` sets the same
guard between theoretical and actual results.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from portable_core.domain.enums import FlowLevel
from portable_core.domain.returns import (
    BASES,
    BasisReturn,
    CarriedHolding,
    CarrySource,
    Disclosures,
    Estimate,
    EstimatedDay,
    ReturnMethod,
    SubPeriod,
    TimeWeightedReturn,
)
from portable_core.services.returns import link

pytestmark = [pytest.mark.property, pytest.mark.unit, pytest.mark.gips]

START = date(2024, 1, 1)

_NONE = Disclosures(
    estimated_days=(),
    stale_price_days=(),
    level5_value=D(0),
    level5_share=None,
    unpriced_holdings=(),
    margin_interest=False,
    reclaimable_withholding=False,
    large_flow_days=(),
    external_fees=False,
)


def _subperiods(days: list[tuple[int, bool]]) -> tuple[SubPeriod, ...]:
    """One daily sub-period per entry: (return in basis points, estimated)."""
    return tuple(
        SubPeriod(
            after=START + timedelta(days=n),
            through=START + timedelta(days=n + 1),
            method=ReturnMethod.DAILY,
            beginning_value=D(100),
            ending_value=D(100),
            flows=(),
            returns=tuple(BasisReturn(basis, D(bps) / 10000) for basis in BASES),
            is_estimated=estimated,
        )
        for n, (bps, estimated) in enumerate(days)
    )


def _estimate(on: date) -> Estimate:
    holding = CarriedHolding(
        account="B", symbol="XYZ", quantity=D(1), price=D(1), last_known=D(1),
        price_date=on, age_days=0, source=CarrySource.PRICE, source_id=1,
        lowered_by=D(0), value=D(1), share=D("0.01"),
    )  # fmt: skip
    return Estimate(D("0.05"), (EstimatedDay(on, D(100), D("0.01"), (holding,)),))


def _linked(subperiods: tuple[SubPeriod, ...], estimate: Estimate | None) -> TimeWeightedReturn:
    return TimeWeightedReturn(
        level=FlowLevel.PORTFOLIO,
        account=None,
        requested_start=START + timedelta(days=1),
        start=START + timedelta(days=1),
        end=subperiods[-1].through,
        requested_end=subperiods[-1].through,
        inception=START + timedelta(days=1),
        cumulative=tuple(
            BasisReturn(basis, link(sub.return_for(basis) for sub in subperiods))
            for basis in BASES
        ),
        subperiods=subperiods,
        nothing_held_before=None,
        nothing_held_after=None,
        disclosures=_NONE,
        estimate=estimate,
    )


@given(st.lists(st.tuples(st.integers(-500, 500), st.booleans()), min_size=1, max_size=40))
def test_linking_any_estimated_subperiod_gives_an_estimated_result(
    days: list[tuple[int, bool]],
) -> None:
    subperiods = _subperiods(days)
    any_estimated = any(estimated for _, estimated in days)

    if any_estimated:
        with pytest.raises(ValueError, match="carries an estimate exactly when"):
            _linked(subperiods, None)
        result = _linked(subperiods, _estimate(subperiods[0].through))
        assert result.is_estimated
    else:
        assert not _linked(subperiods, None).is_estimated
        with pytest.raises(ValueError, match="carries an estimate exactly when"):
            _linked(subperiods, _estimate(subperiods[0].through))
