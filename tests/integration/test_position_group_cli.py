"""Grouping positions, through the real CLI. ADR 0023.

Reproduced before the fix: `pt position group` moved a leg, `pt validate` then
failed (stored state no longer matched a replay), and `pt rebuild` split the
group back apart. `pt buy --position N` recorded N on the ledger and ignored
it. Every command here also survives a rebuild, because each is a ledger row.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from portable_core.schema import migrations as M
from tests.integration.conftest import CliRunner

pytestmark = pytest.mark.integration


@pytest.fixture
def held(run_pt: CliRunner, portfolio: Path) -> str:
    port = str(portfolio)
    for symbol in ("AAPL", "MSFT", "NVDA"):
        run_pt("--port", port, "instrument", "add", symbol, "--type", "equity")
    for symbol, on in (("AAPL", "2024-01-03"), ("MSFT", "2024-01-04")):
        run_pt("--port", port, "buy", symbol, "--qty", "10", "--price", "100.00", "--date", on,
               "-a", "B")  # fmt: skip
    return port


def _positions(run_pt: CliRunner, port: str) -> dict[str, tuple[int, str]]:
    rows = run_pt("--port", port, "position", "list").data["rows"]
    return {
        r["symbol"]: (r["position_id"], r["strategy"]) for r in rows if r["status"] == "open"
    }


def _leg(port: str, symbol: str) -> str:
    with sqlite3.connect(port) as con:
        row = con.execute(
            "SELECT l.leg_id FROM position_leg l JOIN instrument i USING (instrument_id) "
            "WHERE i.symbol = ? AND l.status = 'open'",
            (symbol,),
        ).fetchone()
    con.close()
    return str(row[0])


def test_a_grouping_survives_a_rebuild_and_validates(run_pt: CliRunner, held: str) -> None:
    into = str(_positions(run_pt, held)["AAPL"][0])

    grouped = run_pt("--port", held, "position", "group", _leg(held, "MSFT"), "--into", into,
                     "--strategy", "custom", "--date", "2024-02-01").data  # fmt: skip
    assert grouped["strategy"] == "custom"
    run_pt("--port", held, "validate")  # exit 4 before: stored state disagreed with replay

    run_pt("--port", held, "rebuild")
    after = _positions(run_pt, held)
    assert after["AAPL"] == after["MSFT"], "one position, after the rebuild too"
    assert after["MSFT"][1] == "custom"


def test_ungroup_and_reverse_are_ledger_entries_too(run_pt: CliRunner, held: str) -> None:
    into = str(_positions(run_pt, held)["AAPL"][0])
    regroup = run_pt("--port", held, "position", "group", _leg(held, "MSFT"), "--into", into,
                     "--date", "2024-02-01").data["txn_ids"][0]  # fmt: skip

    run_pt("--port", held, "trade", "reverse", str(regroup), "--date", "2024-02-02")
    reversed_ = _positions(run_pt, held)
    assert reversed_["AAPL"][0] != reversed_["MSFT"][0]

    run_pt("--port", held, "position", "group", _leg(held, "MSFT"), "--into",
           str(reversed_["AAPL"][0]), "--date", "2024-02-03")  # fmt: skip
    run_pt("--port", held, "position", "ungroup", _leg(held, "MSFT"), "--date", "2024-02-04")
    run_pt("--port", held, "rebuild")
    apart = _positions(run_pt, held)
    assert apart["AAPL"][0] != apart["MSFT"][0]
    assert apart["MSFT"][1] == "single"


def test_a_trade_can_join_a_position(run_pt: CliRunner, held: str) -> None:
    """`--position` was recorded on the ledger and ignored."""
    into = str(_positions(run_pt, held)["AAPL"][0])

    run_pt("--port", held, "buy", "NVDA", "--qty", "1", "--price", "100.00", "--date",
           "2024-01-05", "-a", "B", "--position", into)  # fmt: skip
    run_pt("--port", held, "rebuild")

    after = _positions(run_pt, held)
    assert after["NVDA"][0] == after["AAPL"][0]


def test_the_position_flags_refuse_what_they_cannot_do(run_pt: CliRunner, held: str) -> None:
    into = str(_positions(run_pt, held)["AAPL"][0])

    moved = run_pt("--port", held, "buy", "MSFT", "--qty", "1", "--price", "100.00", "--date",
                   "2024-01-05", "-a", "B", "--position", into, expect=4)  # fmt: skip
    assert "already held in position" in moved.json()["error"]["message"]

    second = run_pt("--port", held, "buy", "MSFT", "--qty", "1", "--price", "100.00", "--date",
                    "2024-01-05", "-a", "B", "--new-position", expect=4)  # fmt: skip
    assert "--new-position" in second.json()["error"]["message"]

    run_pt("--port", held, "buy", "NVDA", "--qty", "1", "--price", "100.00", "--date",
           "2024-01-05", "-a", "B", "--new-position")  # fmt: skip


def test_an_older_file_asks_for_a_migration(run_pt: CliRunner, tmp_path: Path) -> None:
    """It used to be read as-is, and fail on the first new column as a bug report."""
    path = tmp_path / "v4.port"
    con = sqlite3.connect(path, isolation_level=None)
    con.row_factory = sqlite3.Row
    for migration in M.available_migrations():
        if migration.version <= 4:
            M._apply(con, migration)
    con.execute(
        "INSERT INTO meta (key, value) VALUES ('portfolio_name', 'Old'), "
        "('inception_date', '2024-01-02'), ('base_currency', 'USD'), "
        "('fiscal_year_end', '12-31') "
        "ON CONFLICT(key) DO NOTHING"
    )
    con.close()

    refused = run_pt("--port", str(path), "holdings", expect=3)
    assert refused.json()["error"]["code"] == "PT-E-SCHEMA-TOO-OLD"

    run_pt("--port", str(path), "migrate", "--yes")
    run_pt("--port", str(path), "holdings")
