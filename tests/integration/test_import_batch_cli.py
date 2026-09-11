"""`pt import batch` through the real CLI.

The commit stage of ADR 0012's pipeline. `--dry-run` gets the most attention
here because a dry run that refuses a batch which would commit — or accepts one
that would not — is worse than having none.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import CliRunner

pytestmark = [pytest.mark.integration]


def _document(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "format": "portable-import-batch",
        "format_version": 1,
        "source": {
            "broker": "example",
            "capabilities": ["COST_BASIS"],
            "files": [],
            "period": {"from": "2024-01-02", "to": "2024-06-30"},
        },
        "rows": rows,
    }


def _append(**fields: Any) -> dict[str, Any]:
    return {"action": "append", "rule": "activity:test", "source_row": {"a": "b"}, **fields}


BUY = _append(
    external_ref="ex:2",
    account="B",
    txn_type="buy",
    trade_date="2024-01-10",
    symbol="AAPL",
    quantity="100",
    price="185.00",
)
SELL = _append(
    external_ref="ex:3",
    account="B",
    txn_type="sell",
    trade_date="2024-06-03",
    symbol="AAPL",
    quantity="40",
    price="210.00",
)
DROP = {"action": "drop", "rule": "sweep:cash-equivalent (ADR 0013)", "source_row": {"a": "b"}}


@pytest.fixture
def ready(run_pt: CliRunner, portfolio: Path) -> Path:
    run_pt("--port", str(portfolio), "instrument", "add", "AAPL", "--type", "equity").ok()
    return portfolio


def _ledger_size(run_pt: CliRunner, port: Path) -> int:
    """Rows in the ledger. The fixture funds the account, so this is never zero."""
    return len(run_pt("--port", str(port), "trade", "list").ok().data["rows"])


def _write(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps(_document(rows)), encoding="utf-8")
    return path


def test_a_batch_commits_and_reports_by_rule(
    run_pt: CliRunner, ready: Path, tmp_path: Path
) -> None:
    batch = _write(tmp_path / "b.json", [BUY, SELL, DROP, DROP])
    result = run_pt("--port", str(ready), "import", "batch", str(batch)).ok()

    assert result.data["append"] == 2
    assert result.data["drop"] == 2
    assert result.data["skip"] == 0
    assert result.data["broker"] == "example"
    # Grouped by rule: a thousand-row batch read one row at a time is not reviewed.
    by_rule = {(r["action"], r["rule"]): r["rows"] for r in result.data["rows"]}
    assert by_rule[("drop", "sweep:cash-equivalent (ADR 0013)")] == 2


def test_a_dry_run_accepts_a_batch_that_commits_and_writes_nothing(
    run_pt: CliRunner, ready: Path, tmp_path: Path
) -> None:
    """The sale relies on the purchase earlier in the same batch.

    Validating each row against the state *before* the batch would refuse this
    for want of a lot — so the dry run is the real commit, rolled back.
    """
    batch = _write(tmp_path / "b.json", [SELL, BUY])
    before = _ledger_size(run_pt, ready)

    run_pt("--port", str(ready), "--dry-run", "import", "batch", str(batch)).ok()
    assert _ledger_size(run_pt, ready) == before, "a dry run wrote to the ledger"

    run_pt("--port", str(ready), "import", "batch", str(batch)).ok()
    assert _ledger_size(run_pt, ready) == before + 2


def test_a_dry_run_refuses_what_a_commit_would_refuse(
    run_pt: CliRunner, ready: Path, tmp_path: Path
) -> None:
    """A sale with no purchase anywhere is refused by both."""
    batch = _write(tmp_path / "b.json", [SELL])
    before = _ledger_size(run_pt, ready)
    run_pt("--port", str(ready), "--dry-run", "import", "batch", str(batch), expect=4)
    run_pt("--port", str(ready), "import", "batch", str(batch), expect=4)
    assert _ledger_size(run_pt, ready) == before


def test_a_failed_commit_leaves_the_ledger_untouched(
    run_pt: CliRunner, ready: Path, tmp_path: Path
) -> None:
    """All or nothing: a batch half in the ledger would need reversing entries."""
    bad = _append(
        external_ref="ex:x",
        account="B",
        txn_type="buy",
        trade_date="2024-02-01",
        symbol="NOPE",
        quantity="1",
        price="1.00",
    )
    batch = _write(tmp_path / "b.json", [BUY, bad])
    before = _ledger_size(run_pt, ready)
    run_pt("--port", str(ready), "import", "batch", str(batch), expect=4)
    assert _ledger_size(run_pt, ready) == before, "half the batch landed"


def test_an_unsupported_transaction_type_is_refused_by_name(
    run_pt: CliRunner, ready: Path, tmp_path: Path
) -> None:
    """Corporate actions are refused rather than half-supported."""
    batch = _write(
        tmp_path / "b.json",
        [_append(account="B", txn_type="split", trade_date="2024-02-01")],
    )
    result = run_pt("--port", str(ready), "import", "batch", str(batch), expect=4)
    assert "split" in result.json()["error"]["message"]


def test_re_committing_the_same_batch_is_refused_as_duplicates(
    run_pt: CliRunner, ready: Path, tmp_path: Path
) -> None:
    """The references are already in the ledger, so the second run refuses."""
    batch = _write(tmp_path / "b.json", [BUY])
    run_pt("--port", str(ready), "import", "batch", str(batch)).ok()
    result = run_pt("--port", str(ready), "import", "batch", str(batch), expect=4)
    assert result.json()["error"]["code"] == "PT-E-DUPLICATE-REF"


def test_the_export_round_trip_still_works_under_its_new_verb(
    run_pt: CliRunner, ready: Path, tmp_path: Path
) -> None:
    """`import` became a noun with verbs; the round-trip moved, not changed."""
    batch = _write(tmp_path / "b.json", [BUY])
    run_pt("--port", str(ready), "import", "batch", str(batch)).ok()

    first = tmp_path / "a.json"
    run_pt("--port", str(ready), "export", "-o", str(first)).ok()
    run_pt("import", "portfolio", str(first), "--into", str(tmp_path / "copy.port")).ok()
    second = tmp_path / "b2.json"
    run_pt("--port", str(tmp_path / "copy.port"), "export", "-o", str(second)).ok()
    assert first.read_bytes() == second.read_bytes()


def test_a_batch_carries_a_reinvested_distribution(
    run_pt: CliRunner, ready: Path, tmp_path: Path
) -> None:
    """Format version 1 carries the income types with a service behind them;
    a reinvestment now has one, and it is income plus a lot with no cash."""
    reinvest = _append(
        external_ref="ex:4",
        account="B",
        txn_type="dividend_reinvest",
        trade_date="2024-03-15",
        symbol="AAPL",
        quantity="0.5",
        amount="100.00",
    )
    batch = _write(tmp_path / "b.json", [BUY, reinvest])
    before = run_pt("--port", str(ready), "account", "show", "B").ok().data["cash"]
    run_pt("--port", str(ready), "import", "batch", str(batch)).ok()
    rows = run_pt("--port", str(ready), "holdings").ok().data["rows"]
    assert next(r for r in rows if r["symbol"] == "AAPL")["quantity"] == "100.5"
    # The buy moved cash; the reinvestment did not.
    after = run_pt("--port", str(ready), "account", "show", "B").ok().data["cash"]
    assert Decimal(before) - Decimal(after) == Decimal("18500.00")

    missing = _write(
        tmp_path / "c.json", [{**reinvest, "external_ref": "ex:5", "quantity": None}]
    )
    result = run_pt("--port", str(ready), "import", "batch", str(missing), expect=4)
    assert "states the units it bought" in result.json()["error"]["message"]
