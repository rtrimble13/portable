"""A symbol recorded in error, corrected through the real CLI. ADR 0034."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner

pytestmark = pytest.mark.integration

REASON = "statements name ARIMX for this fund"


@pytest.fixture
def held(run_pt: CliRunner, portfolio: Path) -> str:
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", "ARSMX", "--type", "mutual_fund")
    run_pt("--port", port, "buy", "ARSMX", "-a", "B", "--qty", "10", "--price", "1.00",
           "--date", "2024-01-03")  # fmt: skip
    return port


def _symbols(port: str) -> list[str]:
    con = sqlite3.connect(port)
    try:
        return [r[0] for r in con.execute("SELECT symbol FROM instrument ORDER BY symbol")]
    finally:
        con.close()


def test_the_holding_is_reported_under_the_corrected_symbol(
    run_pt: CliRunner, held: str
) -> None:
    result = run_pt("--port", held, "instrument", "correct-symbol", "ARSMX", "--to", "ARIMX",
                    "--reason", REASON)  # fmt: skip
    assert result.data["from"] == "ARSMX"
    assert result.data["to"] == "ARIMX"
    assert result.data["periods"] == [{"valid_from": "1900-01-01", "valid_to": None}]

    holdings = run_pt("--port", held, "--as-of", "2024-01-04", "holdings").data
    assert [r["symbol"] for r in holdings["rows"] if r["symbol"] != "CASH"] == ["ARIMX"]
    run_pt("--port", held, "validate").ok()


def test_a_dry_run_writes_nothing(run_pt: CliRunner, held: str) -> None:
    run_pt("--port", held, "--dry-run", "instrument", "correct-symbol", "ARSMX", "--to",
           "ARIMX", "--reason", REASON).ok()  # fmt: skip
    assert "ARSMX" in _symbols(held)


def test_a_correction_without_a_reason_is_a_usage_error(run_pt: CliRunner, held: str) -> None:
    run_pt("--port", held, "instrument", "correct-symbol", "ARSMX", "--to", "ARIMX",
           expect=2)  # fmt: skip
    assert "ARSMX" in _symbols(held)
