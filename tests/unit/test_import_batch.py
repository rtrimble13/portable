"""The import batch: its format, its refusals, and how it reaches the ledger.

ADR 0012. The batch is the reviewable artifact between extracting a custodian's
export and committing it, so two things matter most here: that a malformed
batch is refused in terms a reviewer can act on, and that a committed row went
through the same service a typed command uses rather than a second, laxer path
into the ledger.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from portable_core.domain.enums import TransactionSource
from portable_core.domain.models import Account, Instrument
from portable_core.errors import ValidationError
from portable_core.lint._common import repo_root
from portable_core.persistence.repositories import Repositories
from portable_core.services.import_batch import BatchImporter, load_batch

pytestmark = pytest.mark.unit

D = Decimal
SCHEMA = json.loads(
    (repo_root() / "schemas" / "import-batch-1.0.json").read_text(encoding="utf-8")
)


def _document(rows: list[dict[str, Any]], **source: Any) -> dict[str, Any]:
    return {
        "format": "portable-import-batch",
        "format_version": 1,
        "source": {"broker": "example", "files": [], **source},
        "rows": rows,
    }


def _write(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _append(**fields: Any) -> dict[str, Any]:
    return {
        "action": "append",
        "rule": "activity:test",
        "source_row": {"Type": "test"},
        **fields,
    }


DEPOSIT = _append(
    external_ref="ex:1",
    account="Brokerage",
    txn_type="deposit",
    trade_date="2024-01-02",
    amount="100000.00",
)
BUY = _append(
    external_ref="ex:2",
    account="Brokerage",
    txn_type="buy",
    trade_date="2024-01-10",
    symbol="AAPL",
    quantity="100",
    price="185.00",
)
SELL = _append(
    external_ref="ex:3",
    account="Brokerage",
    txn_type="sell",
    trade_date="2024-06-03",
    symbol="AAPL",
    quantity="40",
    price="210.00",
)


# ── the format ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        ({"format": "something-else"}, "not a portable batch"),
        ({"format_version": 2}, "a version this build does not read"),
        ({"rows": {}}, "rows must be a list"),
        ({"source": {"files": []}}, "source names no broker"),
    ],
)
def test_a_malformed_document_is_refused(
    tmp_path: Path, mutate: dict[str, Any], why: str
) -> None:
    document = {**_document([]), **mutate}
    with pytest.raises(ValidationError):
        load_batch(_write(tmp_path / "b.json", document))


@pytest.mark.parametrize(
    ("row", "why"),
    [
        ({"action": "nope", "rule": "r", "source_row": {}}, "unknown action"),
        ({"action": "drop", "source_row": {}}, "no rule"),
        ({"action": "drop", "rule": "r"}, "no source_row"),
        (_append(account="B", txn_type="buy"), "no trade_date"),
        (_append(account="B", trade_date="2024-01-02"), "no txn_type"),
        (
            _append(account="B", txn_type="split", trade_date="2024-01-02"),
            "a type no batch can carry",
        ),
        (
            _append(account="B", txn_type="deposit", trade_date="2024-01-02", amount=100000.0),
            "a JSON number where a decimal string belongs",
        ),
        (
            _append(account="B", txn_type="deposit", trade_date="not-a-date"),
            "a date that is not YYYY-MM-DD",
        ),
    ],
)
def test_a_malformed_row_is_refused_by_index(
    tmp_path: Path, row: dict[str, Any], why: str
) -> None:
    """Every refusal names the row, because a person reviews this file."""
    with pytest.raises(ValidationError) as caught:
        load_batch(_write(tmp_path / "b.json", _document([row])))
    assert caught.value.context.get("row") == 0, why


def test_an_unsupported_type_says_which_types_a_batch_can_carry(tmp_path: Path) -> None:
    """Refused rather than half-supported (`CLAUDE.md` invariant 10).

    A corporate action needs position context a typed command gathers; an
    importer deriving basis by a second, unreviewed route is the failure this
    avoids.
    """
    row = _append(account="B", txn_type="option_assignment", trade_date="2024-01-02")
    with pytest.raises(ValidationError) as caught:
        load_batch(_write(tmp_path / "b.json", _document([row])))
    supported = caught.value.context["supported"]
    assert "buy" in supported
    assert "option_assignment" not in supported


def test_counts_carry_every_action_even_at_zero(tmp_path: Path) -> None:
    """Absent and zero are different claims about an import."""
    batch = load_batch(_write(tmp_path / "b.json", _document([DEPOSIT])))
    assert batch.counted() == {"append": 1, "drop": 0, "skip": 0}


def test_a_json_number_is_refused_rather_than_coerced(tmp_path: Path) -> None:
    """A JSON number cannot round-trip a decimal (ADR 0005).

    Accepting one would silently convert money through a float, which is the
    guarantee the whole repository is organised around.
    """
    row = _append(account="B", txn_type="deposit", trade_date="2024-01-02", amount=100000.00)
    with pytest.raises(ValidationError) as caught:
        load_batch(_write(tmp_path / "b.json", _document([row])))
    assert "JSON number" in caught.value.message


# ── the published schema and the runtime loader agree ────────────────────────


@pytest.mark.parametrize(
    "document",
    [
        _document([]),
        _document([DEPOSIT, BUY, SELL]),
        _document(
            [
                {"action": "drop", "rule": "sweep:x", "source_row": {"a": "b"}},
                {
                    "action": "skip",
                    "rule": "duplicate:x",
                    "source_row": {"a": "b"},
                    "external_ref": "ex:9",
                },
            ]
        ),
        _document(
            [DEPOSIT],
            capabilities=["COST_BASIS"],
            period={"from": "2024-01-01", "to": "2024-12-31"},
            files=[{"name": "a.csv", "sha256": "0" * 64}],
        ),
    ],
)
def test_what_the_loader_accepts_validates_against_the_published_schema(
    tmp_path: Path, document: dict[str, Any]
) -> None:
    """`schemas/import-batch-1.0.json` is the contract adapter authors target.

    The loader validates by hand because `jsonschema` is a development
    dependency and a hand-written check gives a better error. This is what stops
    the two drifting: anything portable accepts, the published schema describes.
    """
    load_batch(_write(tmp_path / "b.json", document))
    errors = list(Draft202012Validator(SCHEMA).iter_errors(document))
    assert not errors, [e.message for e in errors]


# ── committing ───────────────────────────────────────────────────────────────


@pytest.fixture
def ready(repos: Repositories, taxable_account: Account, aapl: Instrument) -> Repositories:
    return repos


def test_rows_commit_in_trade_date_order_whatever_the_file_says(
    tmp_path: Path, ready: Repositories
) -> None:
    """A sale's relief must see the purchase earlier in the same batch.

    The file lists them backwards on purpose: an importer that trusted file
    order would refuse this batch for want of a lot.
    """
    batch = load_batch(_write(tmp_path / "b.json", _document([SELL, BUY, DEPOSIT])))
    outcome = BatchImporter(ready).commit(batch)

    assert outcome.appended == 3
    gains = ready.lots.realized_gains()
    assert len(gains) == 1
    # 40 shares bought at 185 and sold at 210.
    assert gains[0].gain == D("1000.00")


def test_committed_rows_are_marked_as_imported(tmp_path: Path, ready: Repositories) -> None:
    """An imported row that claims to be hand-entered is untraceable."""
    batch = load_batch(_write(tmp_path / "b.json", _document([DEPOSIT, BUY])))
    BatchImporter(ready).commit(batch)
    for txn in ready.transactions.in_ledger_order():
        assert txn.source is TransactionSource.IMPORT
    assert {t.external_ref for t in ready.transactions.in_ledger_order()} == {"ex:1", "ex:2"}


def test_withholding_survives_the_batch(tmp_path: Path, ready: Repositories) -> None:
    """The batch states the gross; portable derives what landed."""
    dividend = _append(
        external_ref="ex:d",
        account="Brokerage",
        txn_type="dividend",
        trade_date="2024-03-15",
        symbol="AAPL",
        amount="100.00",
        ex_date="2024-03-01",
        taxes_withheld="15.00",
        withholding_reclaimable="5.00",
    )
    batch = load_batch(_write(tmp_path / "b.json", _document([DEPOSIT, BUY, dividend])))
    BatchImporter(ready).commit(batch)

    income = next(t for t in ready.transactions.in_ledger_order() if t.external_ref == "ex:d")
    assert (income.gross_amount, income.net_cash_effect) == (D("100.00"), D("85.00"))
    assert (income.taxes_withheld, income.withholding_reclaimable) == (
        D("15.00"),
        D("5.00"),
    )


def test_a_duplicate_reference_inside_one_batch_is_refused(
    tmp_path: Path, ready: Repositories
) -> None:
    """Schema 0002 binds an importer exactly as it binds a typed command."""
    twice = [DEPOSIT, {**BUY, "external_ref": "ex:1"}]
    batch = load_batch(_write(tmp_path / "b.json", _document(twice)))
    with pytest.raises(ValidationError) as caught:
        BatchImporter(ready).commit(batch)
    assert caught.value.code == "PT-E-DUPLICATE-REF"


def test_an_unclassified_fee_is_refused_through_the_batch_too(
    tmp_path: Path, ready: Repositories
) -> None:
    """PORT-GIPS-D01 does not relax for an importer."""
    row = _append(
        external_ref="ex:f",
        account="Brokerage",
        txn_type="buy",
        trade_date="2024-01-10",
        symbol="AAPL",
        quantity="10",
        price="185.00",
        fees="4.95",
    )
    batch = load_batch(_write(tmp_path / "b.json", _document([DEPOSIT, row])))
    with pytest.raises(ValidationError) as caught:
        BatchImporter(ready).commit(batch)
    assert caught.value.code == "PT-E-FEE-CLASS-MISSING"


def test_a_source_document_that_changed_since_extraction_is_refused(
    tmp_path: Path, ready: Repositories
) -> None:
    """A review approves particular rows against a particular export.

    Re-download the export and the review no longer covers what is about to be
    committed.
    """
    export = tmp_path / "a.csv"
    export.write_text("changed", encoding="utf-8")
    stale = hashlib.sha256(b"original").hexdigest()
    path = _write(
        tmp_path / "b.json",
        _document([DEPOSIT], files=[{"name": "a.csv", "sha256": stale}]),
    )
    batch = load_batch(path)
    with pytest.raises(ValidationError) as caught:
        BatchImporter(ready).commit(batch, batch_path=path)
    assert "changed since this batch was extracted" in caught.value.message


def test_a_source_document_that_is_absent_is_reported_not_refused(
    tmp_path: Path, ready: Repositories
) -> None:
    """A batch is often reviewed somewhere other than where it was extracted.

    So absence is reported rather than refused -- but reported, so that
    "verified" and "not checked" stay distinct.
    """
    path = _write(
        tmp_path / "b.json",
        _document([DEPOSIT], files=[{"name": "gone.csv", "sha256": "0" * 64}]),
    )
    outcome = BatchImporter(ready).commit(load_batch(path), batch_path=path)
    assert outcome.unverified_files == ("gone.csv",)


def test_a_matching_source_document_verifies_silently(
    tmp_path: Path, ready: Repositories
) -> None:
    export = tmp_path / "a.csv"
    export.write_text("original", encoding="utf-8")
    path = _write(
        tmp_path / "b.json",
        _document(
            [DEPOSIT],
            files=[{"name": "a.csv", "sha256": hashlib.sha256(b"original").hexdigest()}],
        ),
    )
    outcome = BatchImporter(ready).commit(load_batch(path), batch_path=path)
    assert outcome.unverified_files == ()


def test_dropped_and_skipped_rows_reach_the_counts_and_not_the_ledger(
    tmp_path: Path, ready: Repositories
) -> None:
    """A discarded row is a visible decision, not an absence."""
    document = _document(
        [
            DEPOSIT,
            {"action": "drop", "rule": "sweep:x", "source_row": {"Type": "MoneyTransfer"}},
            {"action": "skip", "rule": "duplicate:x", "source_row": {"Type": "Buy"}},
        ]
    )
    outcome = BatchImporter(ready).commit(load_batch(_write(tmp_path / "b.json", document)))
    assert (outcome.appended, outcome.dropped, outcome.skipped) == (1, 1, 1)
    assert ready.transactions.count() == 1


def test_an_instrument_the_portfolio_does_not_know_is_refused(
    tmp_path: Path, ready: Repositories
) -> None:
    """The crosswalk's job is upstream; the ledger will not invent a security."""
    row = _append(
        external_ref="ex:x",
        account="Brokerage",
        txn_type="buy",
        trade_date="2024-01-10",
        symbol="NOPE",
        quantity="10",
        price="1.00",
    )
    batch = load_batch(_write(tmp_path / "b.json", _document([DEPOSIT, row])))
    with pytest.raises(ValidationError) as caught:
        BatchImporter(ready).commit(batch)
    assert caught.value.code == "PT-E-INSTRUMENT-NOT-FOUND"


def test_a_withdrawal_below_zero_is_recorded_rather_than_refused(
    tmp_path: Path, ready: Repositories
) -> None:
    """A statement records what happened; it is not proposing a transaction.

    Refusing an overdraft here would refuse a true statement. The cash
    reconciliation is what catches a wrong one.
    """
    row = _append(
        external_ref="ex:w",
        account="Brokerage",
        txn_type="withdrawal",
        trade_date="2024-01-03",
        amount="500.00",
    )
    outcome = BatchImporter(ready).commit(
        load_batch(_write(tmp_path / "b.json", _document([row])))
    )
    assert outcome.appended == 1
