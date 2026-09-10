"""Rolling a custodian's history back to the state before it begins. ADR 0017.

The claim being tested is narrow and worth stating exactly, because the value of
the reconstruction rests on it: **quantities come back exactly, most of the
basis comes back exactly as an aggregate, and everything that does not is
labelled rather than approximated silently.**

So the tests fall in three groups. The roll-back arithmetic, which must be
exact. The basis ladder, where the interesting cases are the ones with no
anchor — a block fully consumed, or a position liquidated entirely — because
those are where a plausible number could be manufactured and must not be. And
the completeness checks, where the roll-back proves something about the
*history* rather than about a position.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portable_core.domain.enums import BasisSource
from portable_core.domain.import_records import HoldingRecord, TransactionRecord
from portable_core.errors import ValidationError
from portable_core.services.reconstruction import reconstruct

pytestmark = pytest.mark.unit

CUTOVER = date(2025, 1, 31)


def _hold(
    symbol: str,
    quantity: str,
    basis: str | None = "1000.00",
    *,
    account: str = "Main",
    cash: bool = False,
    market_value: str | None = None,
) -> HoldingRecord:
    return HoldingRecord(
        as_of=date(2026, 6, 30),
        account=account,
        identifier=symbol,
        quantity=Decimal(quantity),
        is_cash_equivalent=cash,
        market_value=Decimal(market_value) if market_value is not None else None,
        cost_basis=Decimal(basis) if basis is not None else None,
    )


def _txn(
    day: date,
    symbol: str | None,
    quantity: str | None,
    amount: str,
    *,
    account: str = "Main",
    activity: str = "Trade",
) -> TransactionRecord:
    return TransactionRecord(
        trade_date=day,
        account=account,
        activity=activity,
        identifier=symbol,
        quantity=Decimal(quantity) if quantity is not None else None,
        amount=Decimal(amount),
        source_row={},
    )


SWEEP = _hold("SWEEP", "5000.00", "5000.00", cash=True, market_value="5000.00")


# ── the roll-back ────────────────────────────────────────────────────────────


def test_the_cutover_defaults_to_the_day_before_the_history_begins() -> None:
    """ADR 0017 §1, and the only definition the evidence supports."""
    result = reconstruct(
        [_hold("AAPL", "100"), SWEEP],
        [_txn(date(2025, 2, 3), "AAPL", "10", "-1500.00")],
    )
    assert result.cutover == date(2025, 2, 2)


def test_quantities_roll_back_exactly() -> None:
    """Arithmetic on numbers the custodian stated. Nothing is assumed."""
    result = reconstruct(
        [_hold("AAPL", "130"), SWEEP],
        [
            _txn(date(2025, 3, 1), "AAPL", "50", "-7500.00"),
            _txn(date(2025, 6, 1), "AAPL", "-20", "3400.00"),
        ],
        cutover=CUTOVER,
    )
    (position,) = result.positions
    # 130 held today, 50 added and 20 disposed since: 130 - 50 + 20.
    assert position.quantity == Decimal("100")
    assert position.added_after == Decimal("50")
    assert position.disposed_after == Decimal("20")


def test_a_position_opened_after_the_cutover_is_named_not_seeded() -> None:
    """ "Held nothing" and "was not looked at" are different facts."""
    result = reconstruct(
        [_hold("AAPL", "50"), SWEEP],
        [_txn(date(2025, 3, 1), "AAPL", "50", "-7500.00")],
        cutover=CUTOVER,
    )
    assert result.positions == ()
    assert result.opened_after == (("Main", "AAPL"),)


def test_cash_rolls_back_the_same_way() -> None:
    """The reconciliation anchor's other half.

    Quantities that reconcile and cash that does not is the signature of a sign
    error or a dropped row — exactly what a share count cannot catch.
    """
    result = reconstruct(
        [_hold("AAPL", "100"), SWEEP],
        [
            _txn(date(2025, 3, 1), None, None, "-7500.00", activity="Bought"),
            _txn(date(2025, 4, 1), None, None, "200.00", activity="Dividend"),
        ],
        cutover=CUTOVER,
    )
    (cash,) = result.cash
    assert cash.moved_after == Decimal("-7300.00")
    assert cash.amount == Decimal("12300.00")


def test_a_sweep_line_is_cash_and_not_a_position() -> None:
    """ADR 0013: fold it into cash or the account reconciles short by its sweep."""
    result = reconstruct([_hold("AAPL", "100"), SWEEP], [_txn(CUTOVER, None, None, "0")])
    assert [p.identifier for p in result.positions] == ["AAPL"]


def test_accounts_are_kept_apart() -> None:
    holdings = [
        _hold("AAPL", "100", account="Taxable"),
        _hold("AAPL", "40", account="IRA"),
        _hold("SWEEP", "1.00", "1.00", account="Taxable", cash=True, market_value="1.00"),
        _hold("SWEEP", "1.00", "1.00", account="IRA", cash=True, market_value="1.00"),
    ]
    result = reconstruct(
        holdings,
        [_txn(date(2025, 3, 1), "AAPL", "10", "-1500.00", account="IRA")],
        cutover=CUTOVER,
    )
    at_cutover = {(p.account, p.identifier): p.quantity for p in result.positions}
    assert at_cutover == {("Taxable", "AAPL"): Decimal("100"), ("IRA", "AAPL"): Decimal("30")}


# ── the basis ladder ─────────────────────────────────────────────────────────


def test_an_untouched_position_is_reconstructed_exactly() -> None:
    """Today's basis less every subsequent addition. Exact as an aggregate."""
    result = reconstruct(
        [_hold("AAPL", "120", "24150.00"), SWEEP],
        [_txn(date(2025, 3, 1), "AAPL", "20", "-4050.00")],
        cutover=CUTOVER,
    )
    (position,) = result.positions
    assert position.basis_source is BasisSource.RECONSTRUCTED
    assert position.cost_basis == Decimal("20100.00")
    assert "averaged within the block" in (position.assumption or "")


def test_a_partly_consumed_block_is_solved_backwards_under_fifo() -> None:
    """The surviving remainder is the anchor, and the only one there is.

    40 shares are stated at 11240 today, so 281 a share. The block was 50, of
    which 10 were sold: 50 x 281.
    """
    result = reconstruct(
        [_hold("MSFT", "40", "11240.00"), SWEEP],
        [_txn(date(2025, 4, 15), "MSFT", "-10", "4180.00")],
        cutover=CUTOVER,
    )
    (position,) = result.positions
    assert position.basis_source is BasisSource.ESTIMATED
    assert position.cost_basis == Decimal("14050.00")
    assert "FIFO assumed" in (position.assumption or "")
    assert "states the custodian's actual relief method" in (position.assumption or "")


def test_a_fully_consumed_block_has_no_anchor_and_gets_no_number() -> None:
    """ADR 0017 §2a's finding: no relief-method assumption reaches this case.

    Today's basis constrains the block only through what survives of it. Where
    nothing survives there is no equation to solve — under FIFO or anything
    else — so manufacturing a figure here would be inventing one.
    """
    result = reconstruct(
        [_hold("XOM", "30", "3000.00"), SWEEP],
        [
            _txn(date(2025, 3, 1), "XOM", "-50", "6000.00"),
            _txn(date(2025, 4, 1), "XOM", "30", "-3000.00"),
        ],
        cutover=CUTOVER,
    )
    (position,) = result.positions
    assert position.quantity == Decimal("50")
    assert position.basis_source is BasisSource.UNAVAILABLE
    assert position.cost_basis is None
    assert position.still_held is True
    assert "consuming it entirely under FIFO" in (position.assumption or "")


def test_a_liquidated_position_has_no_anchor_either() -> None:
    """It is not in the snapshot at all, so nothing constrains its basis."""
    result = reconstruct(
        [_hold("AAPL", "100"), SWEEP],
        [_txn(date(2025, 3, 1), "KO", "-80", "4000.00")],
        cutover=CUTOVER,
    )
    liquidated = next(p for p in result.positions if p.identifier == "KO")
    assert liquidated.quantity == Decimal("80")
    assert liquidated.basis_source is BasisSource.UNAVAILABLE
    assert liquidated.cost_basis is None
    assert liquidated.still_held is False
    assert "fully liquidated" in (liquidated.assumption or "")


def test_an_unavailable_block_needs_a_cutover_price_and_says_so() -> None:
    """The seed value makes invariant 4 close and is not a basis claim.

    Nothing here invents it: the flag says a price is required, and the seeding
    step fetches one from price history or refuses.
    """
    result = reconstruct(
        [_hold("AAPL", "100"), SWEEP],
        [_txn(date(2025, 3, 1), "KO", "-80", "4000.00")],
        cutover=CUTOVER,
    )
    by_symbol = {p.identifier: p for p in result.positions}
    assert by_symbol["KO"].needs_cutover_price
    assert not by_symbol["AAPL"].needs_cutover_price


def test_a_position_with_no_stated_basis_is_unavailable_not_refused() -> None:
    """COST_BASIS is optional (ADR 0018), so its absence cannot stop the build.

    The honest outcome is the same lot with no basis claim attached, not a
    refusal to construct the portfolio.
    """
    result = reconstruct([_hold("AAPL", "100", None), SWEEP], [_txn(CUTOVER, None, None, "0")])
    (position,) = result.positions
    assert position.basis_source is BasisSource.UNAVAILABLE
    assert position.cost_basis is None
    assert "COST_BASIS" in (position.assumption or "")


def test_a_negative_solve_is_reported_rather_than_clamped() -> None:
    """A clamp would turn a contradiction into a plausible small number."""
    result = reconstruct(
        [_hold("AAPL", "120", "1000.00"), SWEEP],
        [_txn(date(2025, 3, 1), "AAPL", "20", "-9000.00")],
        cutover=CUTOVER,
    )
    (position,) = result.positions
    assert position.basis_source is BasisSource.UNAVAILABLE
    assert position.cost_basis is None
    (finding,) = result.findings
    assert finding.kind == "negative_basis"
    assert "wrong cash sign" in finding.detail


def test_every_assumption_is_recorded_on_the_position_that_used_it() -> None:
    """So the arithmetic can be re-derived and re-argued, not merely trusted."""
    result = reconstruct(
        [_hold("AAPL", "120", "24150.00"), _hold("MSFT", "40", "11240.00"), SWEEP],
        [
            _txn(date(2025, 3, 1), "AAPL", "20", "-4050.00"),
            _txn(date(2025, 4, 15), "MSFT", "-10", "4180.00"),
        ],
        cutover=CUTOVER,
    )
    assert all(p.assumption for p in result.positions)


def test_the_exact_share_is_reported_and_is_none_rather_than_zero() -> None:
    """A report where a sixth of the basis is reconstructed must not look like
    one where none of it is."""
    mixed = reconstruct(
        [_hold("AAPL", "120", "24150.00"), _hold("MSFT", "40", "11240.00"), SWEEP],
        [
            _txn(date(2025, 3, 1), "AAPL", "20", "-4050.00"),
            _txn(date(2025, 4, 15), "MSFT", "-10", "4180.00"),
        ],
        cutover=CUTOVER,
    )
    share = mixed.exact_basis_share
    assert share is not None
    assert Decimal("0.58") < share < Decimal("0.59")

    nothing = reconstruct([_hold("AAPL", "100", None), SWEEP], [_txn(CUTOVER, None, None, "0")])
    assert nothing.exact_basis_share is None


def test_the_ladder_is_tallied_by_source() -> None:
    result = reconstruct(
        [_hold("AAPL", "120", "24150.00"), _hold("MSFT", "40", "11240.00"), SWEEP],
        [
            _txn(date(2025, 3, 1), "AAPL", "20", "-4050.00"),
            _txn(date(2025, 4, 15), "MSFT", "-10", "4180.00"),
        ],
        cutover=CUTOVER,
    )
    assert result.by_source() == {
        BasisSource.RECONSTRUCTED: 1,
        BasisSource.ESTIMATED: 1,
    }


# ── the roll-back as a completeness check ────────────────────────────────────


def test_a_negative_roll_back_proves_the_history_is_missing_an_event() -> None:
    """This is what makes CORPORATE_ACTIONS detectable rather than trusted.

    A holding that rolls back below zero cannot be a rounding artefact: some
    event changed the quantity and is not in the file. A split is the usual
    candidate.
    """
    result = reconstruct(
        [_hold("AAPL", "10"), SWEEP],
        [_txn(date(2025, 3, 1), "AAPL", "100", "-15000.00")],
        cutover=CUTOVER,
    )
    (finding,) = result.findings
    assert finding.kind == "negative_rollback"
    assert finding.identifier == "AAPL"
    assert "corporate action" in finding.detail
    assert result.positions == ()


def test_a_sub_share_residue_is_distinguished_from_a_missing_event() -> None:
    """Custodians state transaction and holding quantities to different
    precisions; tolerated is not the same as unremarked."""
    result = reconstruct(
        [_hold("VTI", "99.995"), SWEEP],
        [_txn(date(2025, 3, 1), "VTI", "100", "-15000.00")],
        cutover=CUTOVER,
    )
    (finding,) = result.findings
    assert finding.kind == "subshare_residue"
    assert "different precisions" in finding.detail


# ── holding-period certainty ─────────────────────────────────────────────────


def test_dispositions_more_than_a_year_after_the_cutover_are_certain() -> None:
    """ADR 0017 §4. Every seeded lot was acquired on or before the cutover, so
    even the latest possible true acquisition date is more than a year before
    such a sale. It is long-term whatever the seeded date says."""
    result = reconstruct(
        [_hold("AAPL", "80", "8000.00"), SWEEP],
        [_txn(date(2026, 3, 1), "AAPL", "-20", "4000.00")],
        cutover=CUTOVER,
    )
    assert result.uncertain_dispositions == ()


def test_dispositions_inside_the_first_year_are_enumerated_not_counted() -> None:
    """The seeded date is the block's *earliest* acquisition, which biases
    toward long-term — the wrong direction to be relaxed about."""
    result = reconstruct(
        [_hold("AAPL", "80", "8000.00"), SWEEP],
        [
            _txn(date(2025, 4, 15), "AAPL", "-10", "2000.00"),
            _txn(date(2025, 6, 1), "AAPL", "-10", "2000.00"),
            _txn(date(2026, 3, 1), "AAPL", "-20", "4000.00"),
        ],
        cutover=CUTOVER,
    )
    assert [(u.trade_date, u.quantity) for u in result.uncertain_dispositions] == [
        (date(2025, 4, 15), Decimal("10")),
        (date(2025, 6, 1), Decimal("10")),
    ]
    assert result.uncertain_dispositions[0].days_after_cutover == 74


def test_a_sale_of_something_bought_after_the_cutover_is_exact() -> None:
    """Its acquisition date is in the ledger; nothing is seeded."""
    result = reconstruct(
        [_hold("AAPL", "100"), SWEEP],
        [
            _txn(date(2025, 3, 1), "NEW", "50", "-5000.00"),
            _txn(date(2025, 4, 1), "NEW", "-50", "5500.00"),
        ],
        cutover=CUTOVER,
    )
    assert all(u.identifier != "NEW" for u in result.uncertain_dispositions)


# ── refusals ─────────────────────────────────────────────────────────────────


def test_no_history_is_refused_rather_than_reconstructed_from_nothing() -> None:
    with pytest.raises(ValidationError, match="no transaction history to roll back"):
        reconstruct([_hold("AAPL", "100")], [])


def test_no_snapshot_is_refused_because_the_anchor_is_the_point() -> None:
    with pytest.raises(ValidationError, match="no holdings snapshot"):
        reconstruct([], [_txn(CUTOVER, "AAPL", "10", "-1500.00")])


def test_a_cutover_past_the_end_of_the_history_is_refused() -> None:
    """Everything would roll back and nothing would be left to reconcile."""
    with pytest.raises(ValidationError, match="at or after the history's last"):
        reconstruct(
            [_hold("AAPL", "100"), SWEEP],
            [_txn(date(2025, 3, 1), "AAPL", "10", "-1500.00")],
            cutover=date(2026, 1, 1),
        )


def test_rows_after_the_snapshot_are_not_rolled_back_and_are_named() -> None:
    """A history pulled a day after the position statement carries rows the
    snapshot does not reflect. Subtracting them would leave a hole in every
    account they touch; they are set aside and reported."""
    sale = _txn(date(2025, 6, 1), "AAPL", "-10", "2000.00")
    later = _txn(date(2026, 7, 1), None, None, "50.00", activity="Dividend")
    result = reconstruct([_hold("AAPL", "30"), SWEEP], [sale, later], cutover=CUTOVER)
    cash = next(c for c in result.cash if c.account == "Main")
    # Only the sale moved cash inside the window the snapshot covers.
    assert cash.moved_after == Decimal("2000.00")
    finding = next(f for f in result.findings if f.kind == "after_snapshot")
    assert "1 row(s) are dated after the snapshot (2026-06-30)" in finding.detail
    assert "2026-07-01" in finding.detail
