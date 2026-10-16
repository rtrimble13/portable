"""`pt cash-flows` and reversals, through the real CLI. ADR 0021, decided by the owner.

Before: the series was the ledger as booked. A reversed deposit appeared as
+X on its own date and -X on the reversal's date, and a reversal dated after
the window left +X standing on its own. Snapshots take their flows from the
corrected ledger -- every reversal and its original netted out -- and show
neither, so the two series the return engine could read disagreed in timing,
which is what the money-weighted return is sensitive to.

Now both are the corrected ledger, and a test holds them equal. The reversed
pair stays visible in `pt activity`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner

pytestmark = pytest.mark.integration


def _txn_id(port: str, txn_type: str, on: str) -> int:
    con = sqlite3.connect(port)
    try:
        row = con.execute(
            'SELECT txn_id FROM "transaction" WHERE txn_type = ? AND trade_date = ?',
            (txn_type, on),
        ).fetchone()
        return int(row[0])
    finally:
        con.close()


@pytest.fixture
def corrected(run_pt: CliRunner, portfolio: Path) -> str:
    """A deposit and a transfer, each reversed, beside the fixture's 500,000 deposit."""
    port = str(portfolio)
    run_pt("--port", port, "account", "add", "--name", "IRA", "--type", "tax_deferred",
           "--opened", "2024-01-02")  # fmt: skip
    run_pt("--port", port, "cash", "deposit", "-a", "B", "--amount", "1000",
           "--date", "2024-01-05")  # fmt: skip
    run_pt("--port", port, "cash", "transfer", "--from", "B", "--to", "IRA",
           "--amount", "100", "--date", "2024-01-06")  # fmt: skip
    run_pt("--port", port, "trade", "reverse", str(_txn_id(port, "deposit", "2024-01-05")),
           "--date", "2024-01-20")  # fmt: skip
    run_pt("--port", port, "trade", "reverse", str(_txn_id(port, "transfer", "2024-01-06")),
           "--date", "2024-01-07")  # fmt: skip
    return port


def _flows(run_pt: CliRunner, port: str, level: str, last: str) -> list[tuple[str, str, str]]:
    rows = run_pt("--port", port, "cash-flows", "--level", level, "--external-only",
                  "--from", "2024-01-01", "--to", last).data["rows"]  # fmt: skip
    return [(r["date"], r["account"], r["amount"]) for r in rows]


@pytest.mark.parametrize("level", ["account", "portfolio"])
def test_a_reversed_entry_and_its_reversal_are_not_flows(
    run_pt: CliRunner, corrected: str, level: str
) -> None:
    """Before: +1000.00 on 01-05 and -1000.00 on 01-20, and the transfer's two
    sides at account level, each reversed on 01-07."""
    assert _flows(run_pt, corrected, level, "2024-01-31") == [("2024-01-02", "B", "500000.00")]


def test_a_reversal_after_the_window_still_nets_its_original_out(
    run_pt: CliRunner, corrected: str
) -> None:
    """Before: the deposit stood alone at +1000.00, its reversal outside the window."""
    assert _flows(run_pt, corrected, "account", "2024-01-10") == [
        ("2024-01-02", "B", "500000.00")
    ]


def test_cash_flows_and_snapshot_flows_are_the_same_series(
    run_pt: CliRunner, corrected: str
) -> None:
    """The two series the return engine reads must not disagree in timing."""
    run_pt("--port", corrected, "value", "--from", "2024-01-02", "--to", "2024-01-31")
    con = sqlite3.connect(corrected)
    try:
        stored = [
            (str(on), str(name), str(amount))
            for on, name, amount in con.execute(
                "SELECT f.flow_date, a.name, f.amount FROM snapshot_flow f "
                "JOIN account a USING (account_id) ORDER BY f.flow_date, a.name"
            )
        ]
    finally:
        con.close()

    assert _flows(run_pt, corrected, "account", "2024-01-31") == stored


def test_activity_still_shows_both_rows_of_each_pair(run_pt: CliRunner, corrected: str) -> None:
    rows = run_pt("--port", corrected, "activity", "--from", "2024-01-01",
                  "--to", "2024-01-31").data["rows"]  # fmt: skip
    types = sorted(r["type"] for r in rows)
    assert types.count("reversal") == 2
    assert "transfer" in types and types.count("deposit") == 2
