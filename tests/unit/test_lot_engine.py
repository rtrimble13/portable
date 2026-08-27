"""The lot engine: every relief method, and the boundaries that decide a tax rate.

Which lots a closing trade consumes determines the basis relieved, the holding
period, and therefore the rate. These tests pin all three.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portable_core.domain.enums import HoldingPeriod, LotStatus, ReliefMethod
from portable_core.domain.models import Lot
from portable_core.errors import ValidationError
from portable_core.services.lots import LotEngine, parse_lot_selection

pytestmark = pytest.mark.unit

D = Decimal
ENGINE = LotEngine()


def lot(
    lot_id: int,
    *,
    open_date: date,
    quantity: str,
    basis: str,
    holding_start: date | None = None,
    is_short: bool = False,
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
        holding_period_start=holding_start or open_date,
        is_short=is_short,
        status=LotStatus.OPEN,
    )


# Three lots, deliberately with different dates AND different prices, so that
# every ordering method picks a different one first.
LOTS = [
    lot(1, open_date=date(2023, 1, 10), quantity="100", basis="10000.00"),  # $100/sh
    lot(2, open_date=date(2023, 6, 15), quantity="100", basis="15000.00"),  # $150/sh
    lot(3, open_date=date(2024, 3, 20), quantity="100", basis="12000.00"),  # $120/sh
]
SALE_DATE = date(2025, 1, 15)


# ── ordering ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "expected_first"),
    [
        (ReliefMethod.FIFO, 1),  # oldest
        (ReliefMethod.LIFO, 3),  # newest
        (ReliefMethod.HIFO, 2),  # $150/sh -- smallest gain
        (ReliefMethod.LOFO, 1),  # $100/sh -- largest gain
    ],
)
def test_each_method_picks_the_lot_it_should(method: ReliefMethod, expected_first: int) -> None:
    plan = ENGINE.select(list(LOTS), D("50"), method, SALE_DATE)
    assert plan.consumptions[0].lot.lot_id == expected_first
    assert plan.total_quantity == D("50")


def test_ordered_methods_consume_across_lots_until_satisfied() -> None:
    plan = ENGINE.select(list(LOTS), D("250"), ReliefMethod.FIFO, SALE_DATE)
    assert [(c.lot.lot_id, c.quantity) for c in plan.consumptions] == [
        (1, D("100")),
        (2, D("100")),
        (3, D("50")),
    ]
    assert plan.total_quantity == D("250")


def test_ordering_is_deterministic_when_lots_tie() -> None:
    """CLAUDE.md invariant 6: identical inputs, identical output.

    Two lots acquired the same day at the same price would otherwise order
    arbitrarily, and the choice changes which lot_id appears in the tax
    record. Every sort key ends in lot_id for this reason.
    """
    tied = [
        lot(7, open_date=date(2024, 1, 1), quantity="10", basis="1000.00"),
        lot(3, open_date=date(2024, 1, 1), quantity="10", basis="1000.00"),
        lot(5, open_date=date(2024, 1, 1), quantity="10", basis="1000.00"),
    ]
    for method in (ReliefMethod.FIFO, ReliefMethod.HIFO, ReliefMethod.LOFO):
        plans = [ENGINE.select(list(tied), D("15"), method, SALE_DATE) for _ in range(10)]
        ids = {tuple(c.lot.lot_id for c in p.consumptions) for p in plans}
        assert len(ids) == 1, f"{method} is not deterministic under ties"


def test_full_consumption_relieves_the_whole_basis_with_no_residue() -> None:
    """A closed lot must not be left holding a rounding remainder.

    Recomputing basis from a per-unit figure leaves fractions of a cent on a
    fully closed lot, which then looks like a closed lot that still has basis.
    """
    awkward = [lot(1, open_date=date(2024, 1, 1), quantity="3", basis="100.00")]
    plan = ENGINE.select(awkward, D("3"), ReliefMethod.FIFO, SALE_DATE)
    assert plan.total_basis == D("100.00")


# ── specific identification ──────────────────────────────────────────────────


def test_spec_id_consumes_exactly_what_was_designated() -> None:
    plan = ENGINE.select(
        list(LOTS),
        D("120"),
        ReliefMethod.SPEC,
        SALE_DATE,
        selection={3: D("100"), 1: D("20")},
    )
    assert [(c.lot.lot_id, c.quantity) for c in plan.consumptions] == [
        (1, D("20")),
        (3, D("100")),
    ], "sorted by lot_id, so the plan does not depend on typing order"


def test_spec_id_without_a_designation_is_refused_not_defaulted() -> None:
    """Falling back to FIFO would change the tax treatment without saying so."""
    with pytest.raises(ValidationError) as exc:
        ENGINE.select(list(LOTS), D("50"), ReliefMethod.SPEC, SALE_DATE)
    assert exc.value.code == "PT-E-LOT-SELECTION-INVALID"
    assert "--method" in (exc.value.remedy or "")


def test_a_designation_that_does_not_add_up_is_refused() -> None:
    """No partial credit: making up the difference silently changes the basis."""
    with pytest.raises(ValidationError) as exc:
        ENGINE.select(
            list(LOTS), D("100"), ReliefMethod.SPEC, SALE_DATE, selection={1: D("60")}
        )
    assert exc.value.code == "PT-E-LOT-SELECTION-INVALID"
    assert exc.value.context["designated"] == "60"


def test_designating_more_than_a_lot_holds_is_refused() -> None:
    with pytest.raises(ValidationError) as exc:
        ENGINE.select(
            list(LOTS), D("150"), ReliefMethod.SPEC, SALE_DATE, selection={1: D("150")}
        )
    assert exc.value.code == "PT-E-LOT-INSUFFICIENT"


def test_designating_an_unknown_lot_is_refused() -> None:
    with pytest.raises(ValidationError) as exc:
        ENGINE.select(
            list(LOTS), D("10"), ReliefMethod.SPEC, SALE_DATE, selection={99: D("10")}
        )
    assert exc.value.code == "PT-E-LOT-SELECTION-INVALID"
    assert exc.value.context["available_lots"] == [1, 2, 3]


@pytest.mark.parametrize(
    "spec", ["", "12", "12:", "abc:10", "12:-5", "12:0", "12:not_a_number"]
)
def test_malformed_lot_selections_are_refused(spec: str) -> None:
    with pytest.raises(ValidationError) as exc:
        parse_lot_selection(spec)
    assert exc.value.code == "PT-E-LOT-SELECTION-INVALID"


def test_lot_selection_parsing_accumulates_repeats() -> None:
    assert parse_lot_selection("12:100;15:50;12:25") == {12: D("125"), 15: D("50")}


# ── average cost ─────────────────────────────────────────────────────────────


def test_average_cost_averages_basis_across_every_open_lot() -> None:
    plan = ENGINE.select(list(LOTS), D("100"), ReliefMethod.AVERAGE, SALE_DATE)
    # (10000 + 15000 + 12000) / 300 = $123.3333.../sh
    assert plan.total_basis == D("12333.33")


def test_average_cost_still_determines_holding_period_lot_by_lot_fifo() -> None:
    """Average cost averages the BASIS, not the DATES.

    IRS Publication 550: under the average basis method the shares disposed of
    are the ones acquired first. So a sale spanning old and new lots is split
    between long-term and short-term, and reporting it wholly as long-term
    because the average lot "looks old" is a wrong tax rate rather than a
    presentational choice.
    """
    lots = [
        lot(1, open_date=date(2023, 1, 10), quantity="100", basis="10000.00"),  # long
        lot(2, open_date=date(2024, 12, 1), quantity="100", basis="20000.00"),  # short
    ]
    plan = ENGINE.select(lots, D("150"), ReliefMethod.AVERAGE, SALE_DATE)

    periods = [(c.lot.lot_id, c.quantity, c.holding_period) for c in plan.consumptions]
    assert periods == [
        (1, D("100"), HoldingPeriod.LONG),
        (2, D("50"), HoldingPeriod.SHORT),
    ]
    # Every share carries the same averaged basis: $150/sh.
    assert plan.total_basis == D("22500.00")


def test_average_cost_cannot_be_mixed_with_specific_identification() -> None:
    """Refused rather than resolved: either choice silently restates old basis."""
    with pytest.raises(ValidationError) as exc:
        LotEngine.check_method_consistency(1, ReliefMethod.AVERAGE, {ReliefMethod.SPEC})
    assert exc.value.code == "PT-E-TAX-METHOD-CONFLICT"

    with pytest.raises(ValidationError):
        LotEngine.check_method_consistency(1, ReliefMethod.FIFO, {ReliefMethod.AVERAGE})

    # Consistent use is fine in both directions.
    LotEngine.check_method_consistency(1, ReliefMethod.AVERAGE, {ReliefMethod.AVERAGE})
    LotEngine.check_method_consistency(1, ReliefMethod.HIFO, {ReliefMethod.FIFO})
    LotEngine.check_method_consistency(1, ReliefMethod.SPEC, set())


# ── holding period at the boundary ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("acquired", "disposed", "expected"),
    [
        (date(2024, 3, 14), date(2025, 3, 14), HoldingPeriod.SHORT),  # exactly 1y
        (date(2024, 3, 14), date(2025, 3, 15), HoldingPeriod.LONG),  # 1y + 1d
        (date(2023, 3, 1), date(2024, 3, 1), HoldingPeriod.SHORT),  # 366 days, leap
        (date(2024, 2, 29), date(2025, 2, 28), HoldingPeriod.SHORT),  # no anniversary
        (date(2024, 2, 29), date(2025, 3, 1), HoldingPeriod.LONG),
    ],
)
def test_holding_period_boundary(
    acquired: date, disposed: date, expected: HoldingPeriod
) -> None:
    """Exactly one year is SHORT. Do not "simplify" these."""
    plan = ENGINE.select(
        [lot(1, open_date=acquired, quantity="10", basis="1000.00")],
        D("10"),
        ReliefMethod.FIFO,
        disposed,
    )
    assert plan.consumptions[0].holding_period is expected


def test_a_short_sale_is_short_term_however_long_it_is_held() -> None:
    held = [lot(1, open_date=date(2015, 1, 1), quantity="100", basis="10000.00", is_short=True)]
    plan = ENGINE.select(held, D("100"), ReliefMethod.FIFO, SALE_DATE)
    assert plan.consumptions[0].holding_period is HoldingPeriod.SHORT
    assert plan.consumptions[0].days_held > 3650


# ── refusals ─────────────────────────────────────────────────────────────────


def test_a_closing_trade_with_no_lots_stops_the_command() -> None:
    """CLAUDE.md invariant 9. Never a zero basis by default."""
    with pytest.raises(ValidationError) as exc:
        ENGINE.select([], D("100"), ReliefMethod.FIFO, SALE_DATE)
    assert exc.value.code == "PT-E-LOT-UNMATCHED"
    assert exc.value.exit_code == 4
    assert "force-zero-basis" in (exc.value.remedy or "")


def test_overselling_is_refused_rather_than_creating_a_short() -> None:
    with pytest.raises(ValidationError) as exc:
        ENGINE.select(list(LOTS), D("500"), ReliefMethod.FIFO, SALE_DATE)
    assert exc.value.code == "PT-E-LOT-INSUFFICIENT"
    assert exc.value.context["available"] == "300"
    assert "pt short" in (exc.value.remedy or "")


def test_a_non_positive_quantity_is_refused() -> None:
    for quantity in (D("0"), D("-10")):
        with pytest.raises(ValidationError):
            ENGINE.select(list(LOTS), quantity, ReliefMethod.FIFO, SALE_DATE)


def test_closed_lots_are_not_candidates() -> None:
    closed = Lot(
        **{
            **{f.name: getattr(LOTS[0], f.name) for f in LOTS[0].__dataclass_fields__.values()},
            "remaining_quantity": D("0"),
            "status": LotStatus.CLOSED,
            "closed_date": date(2024, 6, 1),
        }
    )
    with pytest.raises(ValidationError) as exc:
        ENGINE.select([closed], D("10"), ReliefMethod.FIFO, SALE_DATE)
    assert exc.value.code == "PT-E-LOT-UNMATCHED"


# ── realization ──────────────────────────────────────────────────────────────


def test_proceeds_and_fees_allocate_without_losing_a_cent() -> None:
    """Independent rounding per lot loses money; largest remainder does not."""
    plan = ENGINE.select(list(LOTS), D("300"), ReliefMethod.FIFO, SALE_DATE)
    dispositions = ENGINE.realize(
        plan,
        txn_id=99,
        account_id=1,
        instrument_id=1,
        disposition_date=SALE_DATE,
        gross_proceeds=D("40000.01"),
        fees=D("9.99"),
    )
    assert sum(d.proceeds for d in dispositions) == D("40000.01") - D("9.99")
    assert sum(d.allocated_fees for d in dispositions) == D("9.99")


def test_realized_gain_is_net_proceeds_less_basis() -> None:
    """Fees reduce proceeds rather than forming a third term."""
    plan = ENGINE.select(
        [lot(1, open_date=date(2023, 1, 1), quantity="100", basis="10000.00")],
        D("100"),
        ReliefMethod.FIFO,
        SALE_DATE,
    )
    (disposition,) = ENGINE.realize(
        plan,
        txn_id=1,
        account_id=1,
        instrument_id=1,
        disposition_date=SALE_DATE,
        gross_proceeds=D("15000.00"),
        fees=D("10.00"),
    )
    assert disposition.proceeds == D("14990.00")
    assert disposition.cost_basis_relieved == D("10000.00")
    assert disposition.realized_gain == D("4990.00")
    assert disposition.holding_period is HoldingPeriod.LONG
