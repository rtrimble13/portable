"""The time-weighted return's arithmetic. ADR 0040, PORT-GIPS-B04, B06, D01.

The engine's whole path through stored snapshots is held by
`tests/integration/test_pert_twr_cli.py`; these are the pieces it is built
from, checked against hand-worked figures.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from portable_core.domain.enums import FeeClass, TransactionType
from portable_core.domain.models import Transaction
from portable_core.domain.returns import Basis
from portable_core.errors import GipsRefusalError
from portable_core.services.returns import RETURN_CONTEXT, _Undefined, dietz, fee_addback, link

JUN_30 = date(2025, 6, 30)
JUL_30 = date(2025, 7, 30)


@pytest.mark.gips
def test_modified_dietz_matches_reference() -> None:
    """PORT-GIPS-B06, worked by hand. 30 calendar days, Jun 30 to Jul 30.

    An inflow of 10,000 on Jul 11 counts from the start of that day: 10 days
    elapsed, weight 20/30. (112,000 - 100,000 - 10,000) / (100,000 + 10,000 x
    20/30) = 2,000 / 106,666.67 = 1.875% exactly.
    """
    r = dietz(D("100000"), D("112000"), [(date(2025, 7, 11), D("10000"))], after=JUN_30,
              through=JUL_30)  # fmt: skip
    assert r == D("0.01875")


@pytest.mark.gips
def test_an_outflow_counts_from_the_end_of_its_day() -> None:
    """A 6,000 withdrawal on Jul 20: 20 days elapsed, weight 10/30.

    (95,000 - 100,000 + 6,000) / (100,000 - 6,000 x 10/30) = 1,000 / 98,000.
    """
    r = dietz(D("100000"), D("95000"), [(date(2025, 7, 20), D("-6000"))], after=JUN_30,
              through=JUL_30)  # fmt: skip
    assert r == RETURN_CONTEXT.divide(D(1000), D(98000))


@pytest.mark.gips
def test_modified_dietz_equals_twr_without_flows() -> None:
    """PORT-GIPS-B06: with no flows it is the simple period return, exactly."""
    assert dietz(D("100000"), D("103000"), [], after=JUN_30, through=JUL_30) == D("0.03")


@pytest.mark.gips
def test_the_funding_day_and_the_full_withdrawal_are_both_defined() -> None:
    """ADR 0040 §3: why inflows count at the start and outflows at the end."""
    day = date(2025, 7, 1)
    funded = dietz(D("0"), D("10050"), [(day, D("10000"))], after=JUN_30, through=day)
    emptied = dietz(D("10050"), D("0"), [(day, D("-10080"))], after=JUN_30, through=day)
    assert funded == D("0.005")
    assert emptied == RETURN_CONTEXT.divide(D(30), D(10050))


def test_a_denominator_that_is_not_positive_is_refused() -> None:
    with pytest.raises(_Undefined):
        dietz(D("-500"), D("-400"), [], after=JUN_30, through=date(2025, 7, 1))


@pytest.mark.gips
@given(
    st.lists(
        st.decimals(
            min_value=D("1"),
            max_value=D("1000000"),
            places=2,
            allow_nan=False,
            allow_infinity=False,
        ),
        min_size=2,
        max_size=40,
    )
)
def test_linked_daily_returns_equal_the_direct_return(values: list[D]) -> None:
    """PORT-GIPS-B04: with no flows, linking the days gives the whole period.

    Tolerance: 1e-28 relative, stated here (ADR 0040 §9).
    """
    days = [date(2025, 1, 1).fromordinal(date(2025, 1, 1).toordinal() + n)
            for n in range(len(values))]  # fmt: skip
    daily = [
        dietz(values[n - 1], values[n], [], after=days[n - 1], through=days[n])
        for n in range(1, len(values))
    ]
    direct = RETURN_CONTEXT.subtract(RETURN_CONTEXT.divide(values[-1], values[0]), D(1))
    assert abs(link(daily) - direct) <= D("1e-28") * max(D(1), abs(direct))


@pytest.mark.gips
def test_daily_returns_subsume_subperiod_rule() -> None:
    """PORT-GIPS-B03: daily valuation is valuation at every large flow.

    Chained daily returns across a large flow equal the two sub-period returns
    either side of it, each flowless, to the stated tolerance.
    """
    d = [date(2025, 7, n) for n in range(1, 6)]
    values = [D("100000"), D("101000"), D("150500"), D("151000"), D("149000")]
    flows = {d[2]: D("50000")}  # a large inflow on Jul 3, at the start of the day
    daily = [
        dietz(
            values[n - 1],
            values[n],
            [(d[n], flows[d[n]])] if d[n] in flows else [],
            after=d[n - 1],
            through=d[n],
        )
        for n in range(1, 5)
    ]
    # Sub-periods cut at the flow: Jul 1 to Jul 2 (no flow), then Jul 2 to Jul 5
    # with the flow at the start of its first day.
    before = dietz(values[0], values[1], [], after=d[0], through=d[1])
    after_flow_start = values[1] + flows[d[2]]
    after = values[4] / after_flow_start - 1
    assert abs(link(daily) - link([before, after])) <= D("1e-28")


def _fee(
    cls: FeeClass | None, *, txn_type: TransactionType = TransactionType.FEE
) -> Transaction:
    return Transaction(
        txn_id=7,
        account_id=1,
        trade_date=JUL_30,
        seq=1,
        txn_type=txn_type,
        net_cash_effect=D("-25.00"),
        gross_amount=D("25.00"),
        # On a margin row too, though `pt cash margin-interest` books none: the
        # rule must not lean on the amount being zero.
        fees=D("25.00"),
        fee_class=cls,
    )


@pytest.mark.gips
@pytest.mark.parametrize(
    ("cls", "undone"),
    [
        (FeeClass.TRANSACTION_COST, set()),
        (FeeClass.OTHER_ADMIN, {Basis.NET_OF_FEES, Basis.NET_OF_EXTERNAL_COSTS_ONLY,
                                Basis.GROSS_OF_FEES}),
        (FeeClass.INTERNAL_MGMT_COST, {Basis.NET_OF_EXTERNAL_COSTS_ONLY, Basis.GROSS_OF_FEES}),
        (FeeClass.EXTERNAL_MGMT_FEE, {Basis.GROSS_OF_FEES}),
    ],
)  # fmt: skip
def test_fee_classification_decides_the_bases(cls: FeeClass, undone: set[Basis]) -> None:
    """PORT-GIPS-D01: the ladder, read from the stored class."""
    for basis in Basis:
        expected = D("25.00") if basis in undone else D("0")
        assert fee_addback(_fee(cls), basis) == expected, (cls, basis)


@pytest.mark.gips
def test_margin_interest_is_deducted_at_every_basis_whatever_its_class() -> None:
    """Its rows carry internal_mgmt_cost; reading that would undo it at gross."""
    row = _fee(FeeClass.INTERNAL_MGMT_COST, txn_type=TransactionType.MARGIN_INTEREST)
    assert all(fee_addback(row, basis) == 0 for basis in Basis)


@pytest.mark.gips
def test_a_cash_fee_classed_as_a_fund_expense_is_refused() -> None:
    """PORT-GIPS-D05: it is inside NAV already; a cash row of it deducts it twice."""
    with pytest.raises(GipsRefusalError) as raised:
        fee_addback(_fee(FeeClass.EMBEDDED_FUND_FEE), Basis.GROSS_OF_FEES)
    assert raised.value.code == "PT-E-FEE-EMBEDDED-IN-CASH"
