"""Naming a position, through the real CLI. ADR 0027.

`position.label` existed from the first schema and nothing wrote it. Written
directly, it would have gone the way groupings went before ADR 0023: lost on
the next rebuild. A label is a ledger row, so every command here also survives
a rebuild.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import CliRunner

pytestmark = pytest.mark.integration


@pytest.fixture
def held(run_pt: CliRunner, portfolio: Path) -> str:
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", "AAPL", "--type", "equity")
    run_pt("--port", port, "buy", "AAPL", "--qty", "10", "--price", "100.00",
           "--date", "2024-01-03", "-a", "B")  # fmt: skip
    return port


def _position(run_pt: CliRunner, port: str) -> dict[str, Any]:
    rows = run_pt("--port", port, "position", "list").data["rows"]
    assert len(rows) == 1, rows
    return dict(rows[0])


def _label(run_pt: CliRunner, port: str, *args: str, expect: int = 0) -> dict[str, Any]:
    position_id = str(_position(run_pt, port)["position_id"])
    return dict(
        run_pt("--port", port, "position", "label", position_id, *args, expect=expect).json()
    )


def test_a_label_survives_a_rebuild_and_validates(run_pt: CliRunner, held: str) -> None:
    labelled = _label(run_pt, held, "Core holding", "--date", "2024-02-01")

    assert labelled["data"]["label"] == "Core holding"
    run_pt("--port", held, "rebuild")
    assert _position(run_pt, held)["label"] == "Core holding"
    run_pt("--port", held, "validate")


def test_relabel_clear_and_reverse_are_ledger_entries(run_pt: CliRunner, held: str) -> None:
    _label(run_pt, held, "Core holding", "--date", "2024-02-01")
    renamed = _label(run_pt, held, "Income sleeve", "--date", "2024-03-01")
    assert _position(run_pt, held)["label"] == "Income sleeve"

    run_pt("--port", held, "trade", "reverse", str(renamed["data"]["txn_id"]),
           "--date", "2024-03-02")  # fmt: skip
    assert _position(run_pt, held)["label"] == "Core holding"

    _label(run_pt, held, "--clear", "--date", "2024-04-01")
    run_pt("--port", held, "rebuild")
    assert _position(run_pt, held)["label"] is None
    run_pt("--port", held, "validate")


def test_a_label_dated_before_its_position_is_refused(run_pt: CliRunner, held: str) -> None:
    """A row replay cannot apply would be reported by `pt validate` for ever."""
    error = _label(run_pt, held, "Too early", "--date", "2024-01-02", expect=4)["error"]

    assert "existed on 2024-01-02" in str(error["message"])
    con = sqlite3.connect(held)
    try:
        assert con.execute(
            'SELECT COUNT(*) FROM "transaction" WHERE txn_type = ?', ("position_label",)
        ).fetchone() == (0,)
    finally:
        con.close()


def test_a_label_needs_text_or_clear_but_not_both(run_pt: CliRunner, held: str) -> None:
    _label(run_pt, held, expect=4)
    _label(run_pt, held, "Both", "--clear", expect=4)
    _label(run_pt, held, "   ", expect=4)


def test_a_label_removes_no_valuation_snapshot(run_pt: CliRunner, held: str) -> None:
    """It changes no quantity and no cash, and a snapshot records no names."""
    run_pt("--port", held, "price", "set", "AAPL", "--date", "2024-01-04", "--price", "101.00",
           "--valuation-level", "1")  # fmt: skip
    run_pt("--port", held, "value", "--date", "2024-01-04")

    result = _label(run_pt, held, "Core holding", "--date", "2024-01-03")

    assert not [w for w in result["warnings"] if "valuation snapshot" in w]
    con = sqlite3.connect(held)
    try:
        assert con.execute("SELECT COUNT(*) FROM valuation_snapshot").fetchone() == (1,)
    finally:
        con.close()
    run_pt("--port", held, "validate")
