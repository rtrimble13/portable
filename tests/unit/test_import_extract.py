"""Building the reviewable batch. ADR 0012 stage one, on canonical records.

The batch this produces has two halves that answer different questions, and
most of what can go wrong is one half answering the other's. The seed says what
was held before the ledger begins; the history says what happened after. A row
in both is a position counted twice, and a seed carrying its cost basis where
its market value belongs is the conflation ADR 0015 exists to prevent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

import pytest

from portable_core.domain.enums import (
    BasisSource,
    FeeClass,
    ReliefMethod,
    TransactionType,
)
from portable_core.domain.import_records import (
    HoldingRecord,
    MappedTransaction,
    TransactionRecord,
)
from portable_core.errors import DataUnavailableError, ValidationError
from portable_core.services.import_extract import (
    ExtractResult,
    build_batch,
    build_incremental_batch,
    cutover_prices_needed,
)
from portable_core.services.reconstruction import reconstruct

pytestmark = pytest.mark.unit

CUTOVER = date(2025, 1, 31)
PRICES = {"AAPL": Decimal("200.00")}


def _hold(
    symbol: str,
    quantity: str,
    basis: str | None = "6000.00",
    *,
    cash: bool = False,
) -> HoldingRecord:
    return HoldingRecord(
        as_of=date(2026, 6, 30),
        account="Main",
        identifier=symbol,
        quantity=Decimal(quantity),
        is_cash_equivalent=cash,
        market_value=Decimal(quantity) if cash else None,
        cost_basis=Decimal(basis) if basis is not None else None,
    )


def _txn(
    day: date,
    symbol: str | None,
    quantity: str | None,
    amount: str,
    activity: str = "Bought",
) -> TransactionRecord:
    return TransactionRecord(
        trade_date=day,
        account="Main",
        activity=activity,
        identifier=symbol,
        quantity=Decimal(quantity) if quantity is not None else None,
        amount=Decimal(amount),
        source_row={"Date": day.isoformat(), "Activity": activity},
    )


def _mapped(
    record: TransactionRecord,
    txn_type: TransactionType | None = TransactionType.BUY,
    *,
    fee_class: FeeClass | None = None,
) -> MappedTransaction:
    return MappedTransaction(
        record=record,
        rule=f"activity:{record.activity}",
        txn_type=txn_type,
        fee_class=fee_class,
    )


SWEEP = _hold("SWEEP", "1000.00", "1000.00", cash=True)


def _extract(
    holdings: Sequence[HoldingRecord],
    transactions: Sequence[TransactionRecord],
    mapped: Sequence[MappedTransaction],
    prices: Mapping[str, Decimal] | None = None,
) -> ExtractResult:
    result = reconstruct(holdings, transactions, cutover=CUTOVER)
    return build_batch(
        broker="acme",
        reconstruction=result,
        mapped=mapped,
        cutover_prices=PRICES if prices is None else prices,
    )


# ── the seed ─────────────────────────────────────────────────────────────────


def test_a_seeded_row_carries_the_value_and_the_basis_separately() -> None:
    """The conflation ADR 0015 exists to prevent, at the point it would happen.

    30 shares at 200 is 6,000 of market value on the cutover day. The basis is
    what was paid, and here it is a different number entirely.
    """
    extract = _extract(
        [_hold("AAPL", "30", "4500.00"), SWEEP],
        [_txn(date(2025, 6, 1), None, None, "-100.00", "Fee")],
        [],
    )
    seed = next(r for r in extract.batch.rows if r.rule.startswith("cutover:"))
    assert seed.txn_type is TransactionType.TRANSFER_IN
    assert seed.quantity == Decimal("30")
    assert seed.price == Decimal("200.00")
    assert seed.amount == Decimal("6000.00")  # the flow
    assert seed.original_basis == Decimal("4500.00")  # the tax number
    assert seed.amount != seed.original_basis


def test_the_seed_is_dated_at_the_cutover() -> None:
    extract = _extract(
        [_hold("AAPL", "30"), SWEEP], [_txn(date(2025, 6, 1), None, None, "-1")], []
    )
    seed = next(r for r in extract.batch.rows if r.rule.startswith("cutover:"))
    assert seed.trade_date == CUTOVER


def test_the_seed_carries_its_rung_and_its_assumption() -> None:
    """A lot cannot exist without saying where its basis came from, so the row
    that creates it cannot either."""
    extract = _extract(
        [_hold("AAPL", "30"), SWEEP], [_txn(date(2025, 6, 1), None, None, "-1")], []
    )
    seed = next(r for r in extract.batch.rows if r.rule.startswith("cutover:"))
    assert seed.basis_source is BasisSource.RECONSTRUCTED
    assert seed.basis_assumption
    assert "reconstructed" in seed.rule


def test_a_missing_cutover_price_is_refused_and_names_the_instrument() -> None:
    """There is no honest substitute. The basis is the number a substitute
    would reach for, and it is the one number that must not be used."""
    with pytest.raises(DataUnavailableError) as excinfo:
        _extract(
            [_hold("AAPL", "30"), SWEEP],
            [_txn(date(2025, 6, 1), None, None, "-1")],
            [],
            prices={},
        )
    assert excinfo.value.code == "PT-E-PRICE-MISSING"
    assert excinfo.value.context["instruments"] == ["AAPL"]
    assert "cannot stand in for it" in (excinfo.value.remedy or "")


def test_the_prices_needed_can_be_asked_for_before_anything_is_built() -> None:
    result = reconstruct(
        [_hold("AAPL", "30"), SWEEP],
        [_txn(date(2025, 6, 1), None, None, "-1")],
        cutover=CUTOVER,
    )
    assert cutover_prices_needed(result) == ("AAPL",)


def test_the_seed_reference_is_stable_across_runs() -> None:
    """A second commit of the same extract must be refused as a duplicate
    rather than doubling the position — which is the whole reason references
    are synthesized."""
    holdings = [_hold("AAPL", "30"), SWEEP]
    history = [_txn(date(2025, 6, 1), None, None, "-1")]
    first = _extract(holdings, history, [])
    second = _extract(holdings, history, [])

    def refs(extract: ExtractResult) -> list[str | None]:
        return [r.external_ref for r in extract.batch.rows]

    assert refs(first) == refs(second)
    assert all(r and r.startswith("cutover:") for r in refs(first))


# ── the seeded cash ──────────────────────────────────────────────────────────


def test_the_cash_held_at_the_cutover_is_seeded_too() -> None:
    """Without it nothing reconciles: the account starts from zero cash and
    every purchase drives it negative by exactly the opening balance."""
    extract = _extract(
        [_hold("AAPL", "30"), SWEEP],
        [_txn(date(2025, 6, 1), None, None, "-400.00")],
        [],
    )
    cash = next(r for r in extract.batch.rows if "cutover:cash" in r.rule)
    assert cash.txn_type is TransactionType.DEPOSIT
    assert cash.trade_date == CUTOVER
    # 1,000 today, less the 400 that went out after the cutover.
    assert cash.amount == Decimal("1400.00")
    assert "ADR 0015" in cash.rule


def test_a_negative_cutover_balance_is_a_margin_loan_not_a_contribution() -> None:
    """The sign has to stay honest: `record_cash` reads direction from the type
    and refuses a negative amount outright."""
    extract = _extract(
        [_hold("AAPL", "30"), SWEEP],
        [_txn(date(2025, 6, 1), None, None, "5000.00", "Sold")],
        [],
    )
    cash = next(r for r in extract.batch.rows if "cutover:cash" in r.rule)
    assert cash.txn_type is TransactionType.WITHDRAWAL
    assert cash.amount == Decimal("4000.00")
    assert "margin" in cash.rule


def test_a_zero_cutover_balance_writes_no_row() -> None:
    """A row asserting nothing was there is noise in a file read line by line."""
    extract = _extract(
        [_hold("AAPL", "30"), _hold("SWEEP", "0.00", "0.00", cash=True)],
        [_txn(date(2025, 6, 1), None, None, "0")],
        [],
    )
    assert not [r for r in extract.batch.rows if "cutover:cash" in r.rule]


# ── the history ──────────────────────────────────────────────────────────────


def test_rows_at_or_before_the_cutover_are_not_appended() -> None:
    """The seed already accounts for them; appending them too doubles the
    position."""
    before = _txn(date(2025, 1, 5), "AAPL", "10", "-1000.00")
    after = _txn(date(2025, 6, 1), "AAPL", "10", "-2000.00")
    extract = _extract(
        [_hold("AAPL", "40"), SWEEP], [before, after], [_mapped(before), _mapped(after)]
    )
    appended = [r for r in extract.batch.rows if r.action == "append" and r.symbol == "AAPL"]
    assert [r.trade_date for r in appended] == [CUTOVER, date(2025, 6, 1)]


def test_a_closing_trade_states_fifo_relief() -> None:
    """ADR 0017 §2a. The seeded basis was solved under FIFO, so the ledger has
    to relieve under FIFO — a block solved for one method and relieved by
    another yields a basis the solve never computed."""
    sale = _txn(date(2025, 6, 1), "AAPL", "-10", "2000.00", "Sold")
    extract = _extract(
        [_hold("AAPL", "30"), SWEEP], [sale], [_mapped(sale, TransactionType.SELL)]
    )
    row = next(r for r in extract.batch.rows if r.txn_type is TransactionType.SELL)
    assert row.relief_method is ReliefMethod.FIFO


def test_an_opening_trade_states_no_relief_method() -> None:
    buy = _txn(date(2025, 6, 1), "AAPL", "10", "-2000.00")
    extract = _extract([_hold("AAPL", "40"), SWEEP], [buy], [_mapped(buy)])
    row = next(r for r in extract.batch.rows if r.txn_type is TransactionType.BUY)
    assert row.relief_method is None


def test_quantities_and_amounts_are_magnitudes_in_the_batch() -> None:
    """The canonical record signs them; the batch states direction by type,
    which is what every typed command does."""
    sale = _txn(date(2025, 6, 1), "AAPL", "-10", "2000.00", "Sold")
    extract = _extract(
        [_hold("AAPL", "30"), SWEEP], [sale], [_mapped(sale, TransactionType.SELL)]
    )
    row = next(r for r in extract.batch.rows if r.txn_type is TransactionType.SELL)
    assert row.quantity == Decimal("10")
    assert row.amount == Decimal("2000.00")
    assert row.price == Decimal("200.00")


def test_a_skipped_row_is_carried_with_its_reason() -> None:
    """A silently dropped row and a deliberately dropped one are the same
    absence in the ledger and completely different claims about the import."""
    memo = _txn(date(2025, 6, 1), "AAPL", None, "0", "Position Memo")
    extract = _extract(
        [_hold("AAPL", "30"), SWEEP],
        [_txn(date(2025, 6, 2), None, None, "-1")],
        [MappedTransaction(record=memo, rule="activity:Position Memo", reason="a restatement")],
    )
    skipped = next(r for r in extract.batch.rows if r.action == "skip")
    assert skipped.note == "a restatement"
    assert skipped.source_row["Activity"] == "Position Memo"


def test_a_type_the_batch_cannot_carry_is_skipped_and_named() -> None:
    """Recorded rather than refused, and never silently dropped: format
    version 1 carries the types with a service behind them, and the operator
    records the rest by hand."""
    split = _txn(date(2025, 6, 1), "AAPL", "30", "0", "Split")
    extract = _extract(
        [_hold("AAPL", "60"), SWEEP], [split], [_mapped(split, TransactionType.SPLIT)]
    )
    row = next(r for r in extract.batch.rows if "unsupported" in r.rule)
    assert row.action == "skip"
    assert extract.unsupported == ("split",)
    assert "record it with the typed command" in (row.note or "")


def test_a_custodian_identifier_is_preferred_to_a_hash() -> None:
    """It survives the custodian re-exporting the same period in a different
    row order, which a content hash including the ordinal does not."""
    row = TransactionRecord(
        trade_date=date(2025, 6, 1),
        account="Main",
        activity="Bought",
        identifier="AAPL",
        quantity=Decimal("10"),
        amount=Decimal("-2000.00"),
        source_row={"Confirm": "C-1"},
        external_id="C-1",
    )
    extract = _extract([_hold("AAPL", "40"), SWEEP], [row], [_mapped(row)])
    appended = next(r for r in extract.batch.rows if r.txn_type is TransactionType.BUY)
    assert appended.external_ref == "C-1"


def test_the_fee_class_survives_into_the_batch() -> None:
    """PORT-GIPS-D01: a fee with no classification is refused at commit, so the
    extract has to carry the one the activity map decided."""
    fee = _txn(date(2025, 6, 1), None, None, "-100.00", "Advisory Fee")
    extract = _extract(
        [_hold("AAPL", "30"), SWEEP],
        [fee],
        [_mapped(fee, TransactionType.FEE, fee_class=FeeClass.EXTERNAL_MGMT_FEE)],
    )
    row = next(r for r in extract.batch.rows if r.txn_type is TransactionType.FEE)
    assert row.fee_class is FeeClass.EXTERNAL_MGMT_FEE


def test_the_batch_period_spans_the_cutover_to_the_last_row() -> None:
    """Not the snapshot date, which is later than anything the batch carries."""
    last = _txn(date(2025, 6, 1), "AAPL", "10", "-2000.00")
    extract = _extract([_hold("AAPL", "40"), SWEEP], [last], [_mapped(last)])
    assert extract.batch.source.period == (CUTOVER, date(2025, 6, 1))


# ── identity, and the overlap ────────────────────────────────────────────────


def _hashed(day: date, amount: str, activity: str = "Dividend") -> TransactionRecord:
    """A row with no custodian identifier, so the reference is synthesized."""
    return TransactionRecord(
        trade_date=day,
        account="Main",
        activity=activity,
        identifier="AAPL",
        quantity=None,
        amount=Decimal(amount),
        source_row={"Date": day.isoformat(), "Activity": activity, "Amount": amount},
    )


def _refs(*records: TransactionRecord) -> list[str]:
    extract = _extract(
        [_hold("AAPL", "30"), SWEEP],
        list(records),
        [_mapped(r, TransactionType.DIVIDEND) for r in records],
    )
    return [r.external_ref or "" for r in extract.batch.rows if r.action == "append"]


def test_two_identical_rows_on_one_day_get_distinct_references() -> None:
    """ADR 0012's ordinal. Two identical dividends on one day are not
    hypothetical, and without it the second would be refused as a duplicate
    of the first."""
    twin = _hashed(date(2025, 6, 1), "10.00")
    rows = [ref for ref in _refs(twin, twin) if ref.startswith("row:")]
    assert len(rows) == 2 and len(set(rows)) == 2


def test_a_reference_does_not_depend_on_where_the_row_sits_in_the_batch() -> None:
    """The same source row gets the same reference in the initial extract and
    in every incremental one after it. That is what lets an overlapping export
    be recognised as an overlap rather than refused as a duplicate."""
    later = _hashed(date(2025, 6, 1), "10.00")
    earlier = _hashed(date(2025, 3, 1), "7.00")
    alone = _refs(later)
    with_company = _refs(earlier, later)
    assert alone[-1] == with_company[-1]


def test_a_row_the_ledger_already_carries_is_a_skip_naming_the_reference() -> None:
    """Resolved at extract, where the reviewer sees it, not at commit and not
    by a flag that would hide it."""
    row = _hashed(date(2025, 6, 1), "10.00")
    result = reconstruct([_hold("AAPL", "30"), SWEEP], [row], cutover=CUTOVER)
    extract = build_batch(
        broker="acme",
        reconstruction=result,
        mapped=[_mapped(row, TransactionType.DIVIDEND)],
        cutover_prices=PRICES,
        in_ledger=lambda account, ref: ref.startswith("row:"),
    )
    skipped = next(r for r in extract.batch.rows if r.rule == "ledger:already-recorded")
    assert skipped.action == "skip"
    assert skipped.external_ref and skipped.external_ref.startswith("row:")
    assert skipped.external_ref in (skipped.note or "")
    assert extract.appended == 0


def test_a_transfer_carries_its_counter_account_into_the_batch() -> None:
    """ADR 0014: one ledger row, with the receiving account on it."""
    leg = _txn(date(2025, 6, 1), None, None, "-150.00", "Journal")
    extract = _extract(
        [_hold("AAPL", "30"), SWEEP],
        [leg],
        [
            MappedTransaction(
                record=leg,
                rule="activity:Journal (paired with row 2)",
                txn_type=TransactionType.TRANSFER,
                counter_account="IRA",
            )
        ],
    )
    row = next(r for r in extract.batch.rows if r.txn_type is TransactionType.TRANSFER)
    assert row.counter_account == "IRA"
    assert row.amount == Decimal("150.00")


# ── the incremental extract ──────────────────────────────────────────────────


def _incremental(
    mapped: Sequence[MappedTransaction],
    *,
    inception: Mapping[str, date] | None = None,
    known: frozenset[str] = frozenset(),
) -> ExtractResult:
    return build_incremental_batch(
        broker="acme",
        mapped=mapped,
        inception={"Main": CUTOVER} if inception is None else inception,
        in_ledger=lambda account, ref: ref in known,
    )


def test_an_incremental_extract_seeds_nothing() -> None:
    row = _hashed(date(2025, 6, 1), "10.00")
    extract = _incremental([_mapped(row, TransactionType.DIVIDEND)])
    assert extract.seeded == 0
    assert extract.appended == 1
    assert all("cutover" not in r.rule for r in extract.batch.rows)


def test_an_incremental_extract_skips_what_the_ledger_already_carries() -> None:
    row = TransactionRecord(
        trade_date=date(2025, 6, 1),
        account="Main",
        activity="Dividend",
        identifier="AAPL",
        quantity=None,
        amount=Decimal("10.00"),
        source_row={},
        external_id="C-1",
    )
    extract = _incremental([_mapped(row, TransactionType.DIVIDEND)], known=frozenset({"C-1"}))
    assert extract.appended == 0 and extract.skipped == 1
    assert extract.batch.rows[0].rule == "ledger:already-recorded"


def test_a_row_on_or_before_the_accounts_first_ledger_date_is_inside_the_seed() -> None:
    """A later export whose window reaches back past the cutover would
    otherwise append rows the seeded position already contains."""
    at = _hashed(CUTOVER, "10.00")
    before = _hashed(date(2025, 1, 2), "10.00")
    after = _hashed(date(2025, 2, 1), "10.00")
    extract = _incremental([_mapped(r, TransactionType.DIVIDEND) for r in (at, before, after)])
    rules = [r.rule for r in extract.batch.rows]
    assert rules == [
        "ledger:before-inception",
        "ledger:before-inception",
        "activity:Dividend",
    ]
    assert "2025-01-31" in (extract.batch.rows[0].note or "")


def test_an_account_with_no_ledger_rows_is_refused_by_an_incremental_extract() -> None:
    row = _hashed(date(2025, 6, 1), "10.00")
    with pytest.raises(ValidationError) as excinfo:
        _incremental([_mapped(row, TransactionType.DIVIDEND)], inception={"Other": CUTOVER})
    assert excinfo.value.context["accounts"] == ["Main"]
    assert "run the initial extract" in str(excinfo.value)


def test_an_incremental_batch_states_the_period_it_covers() -> None:
    rows = [_hashed(date(2025, 6, 1), "1"), _hashed(date(2025, 3, 1), "2")]
    extract = _incremental([_mapped(r, TransactionType.DIVIDEND) for r in rows])
    assert extract.batch.source.period == (date(2025, 3, 1), date(2025, 6, 1))


def test_the_seed_carries_the_blocks_earliest_acquisition_date() -> None:
    """Stated where the custodian states one; the lot then ages from it."""
    held = HoldingRecord(
        as_of=date(2026, 6, 30),
        account="Main",
        identifier="AAPL",
        quantity=Decimal("30"),
        is_cash_equivalent=False,
        cost_basis=Decimal("6000.00"),
        acquired=date(2018, 6, 14),
    )
    extract = _extract([held, SWEEP], [_txn(date(2025, 6, 1), None, None, "1.00")], [])
    seed = next(r for r in extract.batch.rows if r.symbol == "AAPL")
    assert seed.original_acquired_date == date(2018, 6, 14)


def test_the_two_parts_of_a_block_get_distinct_references_and_the_accounted_part_first() -> (
    None
):
    receipt = TransactionRecord(
        trade_date=CUTOVER,
        account="Main",
        activity="Receipt",
        identifier="AAPL",
        quantity=Decimal("100"),
        amount=Decimal("0"),
        source_row={},
        value=Decimal("20000.00"),
    )
    sale = _txn(date(2025, 3, 1), "AAPL", "-30", "6600.00", "Sold")
    extract = _extract([SWEEP], [receipt, sale], [_mapped(sale, TransactionType.SELL)])
    seeds = [r for r in extract.batch.rows if r.symbol == "AAPL" and "cutover" in r.rule]
    assert [s.quantity for s in seeds] == [Decimal("30"), Decimal("70")]
    assert seeds[0].external_ref != seeds[1].external_ref
    assert seeds[1].source_row["part"] == "vanished"
