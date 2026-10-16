"""Valuation snapshots, `pt validate` and `pt rebuild`, through the real CLI. ADR 0024.

Reproduced before the fix:

* `pt value` then `pt validate` failed with ``PT-E-REPLAY-MISMATCH`` naming
  ``valuation_snapshot``. Validate compared snapshots against a replay of the
  ledger, which cannot produce them, so every valued portfolio failed.
* `pt rebuild`, the remedy validate named, deleted every snapshot.
* A deposit dated before a stored snapshot, but after the ledger's last row,
  was applied incrementally and left that snapshot describing cash the account
  no longer had.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner

pytestmark = pytest.mark.integration

DAYS = ("2024-01-03", "2024-01-04", "2024-01-05")


def _query(port: str, sql: str, *params: object) -> list[tuple[object, ...]]:
    con = sqlite3.connect(port)
    try:
        return [tuple(row) for row in con.execute(sql, params)]
    finally:
        con.close()


def _write(port: str, sql: str, *params: object) -> None:
    con = sqlite3.connect(port)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def _snapshots(port: str) -> list[tuple[object, ...]]:
    return _query(
        port,
        "SELECT a.name, s.snapshot_date, s.ending_market_value FROM valuation_snapshot s "
        "JOIN account a USING (account_id) ORDER BY a.name, s.snapshot_date",
    )


@pytest.fixture
def valued(run_pt: CliRunner, portfolio: Path) -> str:
    """Two accounts, one holding, priced and valued over three days."""
    port = str(portfolio)
    run_pt("--port", port, "account", "add", "--name", "IRA", "--type", "tax_deferred",
           "--opened", "2024-01-02")  # fmt: skip
    run_pt("--port", port, "cash", "deposit", "-a", "IRA", "--amount", "1000",
           "--date", "2024-01-02")  # fmt: skip
    run_pt("--port", port, "instrument", "add", "AAPL", "--type", "equity")
    run_pt("--port", port, "buy", "AAPL", "-a", "B", "--qty", "10", "--price", "100.00",
           "--date", "2024-01-02")  # fmt: skip
    for on, price in zip(DAYS, ("101.00", "102.00", "103.00"), strict=True):
        run_pt("--port", port, "price", "set", "AAPL", "--date", on, "--price", price,
               "--valuation-level", "1")  # fmt: skip
    run_pt("--port", port, "value", "--from", DAYS[0], "--to", DAYS[-1])
    return port


def test_a_valued_portfolio_validates(run_pt: CliRunner, valued: str) -> None:
    """Exit 4, PT-E-REPLAY-MISMATCH naming valuation_snapshot, before the fix."""
    assert len(_snapshots(valued)) == 6

    assert run_pt("--port", valued, "validate").data["problems"] == 0


def test_rebuild_keeps_valuation_history(run_pt: CliRunner, valued: str) -> None:
    """`pt rebuild` deleted every snapshot. A replay cannot produce them back."""
    before = _snapshots(valued)

    run_pt("--port", valued, "rebuild")

    assert _snapshots(valued) == before
    run_pt("--port", valued, "validate")


def test_a_tampered_snapshot_fails_validate_naming_it(run_pt: CliRunner, valued: str) -> None:
    _write(
        valued,
        "UPDATE valuation_snapshot SET ending_market_value = '999.99' "
        "WHERE snapshot_date = '2024-01-04' "
        "AND account_id = (SELECT account_id FROM account WHERE name = 'B')",
    )

    error = run_pt("--port", valued, "validate", expect=4).json()["error"]

    assert error["code"] == "PT-E-SNAPSHOT-STALE"
    problems = error["context"]["problems"]
    assert [p for p in problems if "B 2024-01-04" in p and "ending_market_value" in p], problems
    # The day after chains its beginning value from the tampered ending.
    assert [p for p in problems if "B 2024-01-05" in p and "beginning_market_value" in p]
    assert not [p for p in problems if "IRA" in p or "2024-01-03" in p], problems


def test_a_tampered_price_set_fails_validate_naming_it(run_pt: CliRunner, valued: str) -> None:
    _write(
        valued,
        "UPDATE valuation_snapshot_price SET price = '150.00' WHERE snapshot_id = ("
        "SELECT snapshot_id FROM valuation_snapshot WHERE snapshot_date = '2024-01-03' "
        "AND account_id = (SELECT account_id FROM account WHERE name = 'B'))",
    )

    error = run_pt("--port", valued, "validate", expect=4).json()["error"]

    problems = error["context"]["problems"]
    assert [p for p in problems if "B 2024-01-03" in p and "recorded AAPL at 150.00" in p]
    assert [p for p in problems if "B 2024-01-03" in p and "prices" in p], problems


def test_port_gips_a09_a_replaced_price_names_the_snapshots_that_used_it(
    run_pt: CliRunner, valued: str
) -> None:
    """PORT-GIPS-A09: a final price replacing the one a snapshot used."""
    run_pt("--port", valued, "price", "set", "AAPL", "--date", "2024-01-04", "--price",
           "102.50", "--valuation-level", "1")  # fmt: skip

    problems = run_pt("--port", valued, "validate", expect=4).json()["error"]["context"][
        "problems"
    ]
    assert len(problems) == 1, problems
    assert "B 2024-01-04: recorded AAPL at 102.00 at level 1" in problems[0]
    assert "now says 102.50 at level 1" in problems[0]

    # Valuing that day again restates it, and carries its new ending value into
    # the beginning value of the day after, which keeps the prices it recorded.
    valued_again = run_pt("--port", valued, "value", "--date", "2024-01-04").json()
    assert (
        "updated the stored B snapshot of 2024-01-05: its beginning value follows the day "
        "valued before it"
    ) in valued_again["warnings"]
    run_pt("--port", valued, "validate")


def test_an_entry_removes_only_the_snapshots_it_changes(run_pt: CliRunner, valued: str) -> None:
    """Dated after the ledger's last row, so applied incrementally, not rebuilt.

    Before the fix the snapshot stayed as it was: cash 0.00, while the account
    held 500.00 more than that.
    """
    result = run_pt("--port", valued, "cash", "deposit", "-a", "B", "--amount", "500",
                    "--date", "2024-01-04")  # fmt: skip

    remaining = _snapshots(valued)
    assert [(name, on) for name, on, _ in remaining] == [
        ("B", "2024-01-03"),
        ("IRA", "2024-01-03"),
        ("IRA", "2024-01-04"),
        ("IRA", "2024-01-05"),
    ]
    warnings = result.json()["warnings"]
    assert [w for w in warnings if w.startswith("removed 2 valuation snapshots of B")], warnings
    assert "pt value --from 2024-01-04 --to 2024-01-05" in " ".join(warnings)
    run_pt("--port", valued, "validate")


def test_a_transfer_removes_both_sides(run_pt: CliRunner, valued: str) -> None:
    run_pt("--port", valued, "cash", "transfer", "--from", "IRA", "--to", "B",
           "--amount", "100", "--date", "2024-01-05")  # fmt: skip

    assert [(name, on) for name, on, _ in _snapshots(valued)] == [
        ("B", "2024-01-03"),
        ("B", "2024-01-04"),
        ("IRA", "2024-01-03"),
        ("IRA", "2024-01-04"),
    ]
    run_pt("--port", valued, "validate")


def test_a_reversal_removes_from_the_date_of_what_it_reverses(
    run_pt: CliRunner, valued: str
) -> None:
    """The reversed entry is netted out of history from its own date (ADR 0021)."""
    deposit = _query(
        valued,
        'SELECT txn_id FROM "transaction" t JOIN account a USING (account_id) '
        "WHERE a.name = 'IRA' AND t.txn_type = 'deposit'",
    )[0][0]

    run_pt("--port", valued, "trade", "reverse", str(deposit), "--date", "2024-01-05")

    assert [name for name, _, _ in _snapshots(valued)] == ["B", "B", "B"]
    run_pt("--port", valued, "validate")


def test_an_import_says_what_it_removes_and_its_dry_run_what_it_would(
    run_pt: CliRunner, valued: str, tmp_path: Path
) -> None:
    """A dry-run import is the real commit, rolled back, so it removes nothing."""
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps(
            {
                "format": "portable-import-batch",
                "format_version": 1,
                "source": {
                    "broker": "example",
                    "capabilities": ["COST_BASIS"],
                    "files": [],
                    "period": {"from": "2024-01-02", "to": "2024-01-31"},
                },
                "rows": [
                    {
                        "action": "append",
                        "rule": "activity:test",
                        "source_row": {"a": "b"},
                        "external_ref": "ex:1",
                        "account": "B",
                        "txn_type": "buy",
                        "trade_date": "2024-01-04",
                        "symbol": "AAPL",
                        "quantity": "1",
                        "price": "100.00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    before = _snapshots(valued)

    rehearsed = run_pt("--port", valued, "--dry-run", "import", "batch", str(batch))

    assert _snapshots(valued) == before
    warnings = rehearsed.json()["warnings"]
    assert [w for w in warnings if w.startswith("would remove 2 valuation snapshots of B")]
    assert not [w for w in warnings if w.startswith("removed")], warnings

    committed = run_pt("--port", valued, "import", "batch", str(batch))

    assert len(_snapshots(valued)) == len(before) - 2
    assert [w for w in committed.json()["warnings"] if w.startswith("removed 2 valuation")]
    run_pt("--port", valued, "validate")


def test_a_regroup_removes_nothing(run_pt: CliRunner, valued: str) -> None:
    """It moves a leg between positions; no snapshot records either (ADR 0023)."""
    run_pt("--port", valued, "instrument", "add", "MSFT", "--type", "equity")
    run_pt("--port", valued, "buy", "MSFT", "-a", "B", "--qty", "1", "--price", "50.00",
           "--date", "2024-01-02")  # fmt: skip
    for on in DAYS:
        run_pt("--port", valued, "price", "set", "MSFT", "--date", on, "--price", "50.00",
               "--valuation-level", "1")  # fmt: skip
    run_pt("--port", valued, "value", "--from", DAYS[0], "--to", DAYS[-1])
    before = _snapshots(valued)
    legs = {
        str(symbol): leg_id
        for symbol, leg_id in _query(
            valued,
            "SELECT i.symbol, l.leg_id FROM position_leg l "
            "JOIN instrument i USING (instrument_id)",
        )
    }
    into = _query(valued, "SELECT position_id FROM position_leg WHERE leg_id = ?", legs["AAPL"])

    run_pt("--port", valued, "position", "group", str(legs["MSFT"]), "--into", str(into[0][0]),
           "--date", "2024-01-03")  # fmt: skip

    assert _snapshots(valued) == before
    run_pt("--port", valued, "validate")
