"""`--dry-run` is the real write, rolled back. ADR 0026.

Before: most commands returned early under `--dry-run`, before anything was
written, so a dry run could not report what happens while writing -- a
back-dated entry's rebuild, the valuation snapshots it removes -- and accepted
what the real run refuses at write time: a tax rate that restates history, a
duplicate `--ref`. `pt import batch` alone rehearsed the commit.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import CliRunner

pytestmark = pytest.mark.integration


def _contents(port: str) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table, so 'nothing was written' means exactly that."""
    con = sqlite3.connect(port)
    try:
        tables = [
            str(row[0])
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            table: [
                tuple(row) for row in con.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
            ]
            for table in tables
        }
    finally:
        con.close()


@pytest.fixture
def book(run_pt: CliRunner, portfolio: Path) -> str:
    """B holds AAPL and MSFT, priced and valued for two days; IRA is funded."""
    port = str(portfolio)
    run_pt("--port", port, "account", "add", "--name", "IRA", "--type", "tax_deferred",
           "--opened", "2024-01-02")  # fmt: skip
    run_pt("--port", port, "cash", "deposit", "-a", "IRA", "--amount", "1000",
           "--date", "2024-01-02")  # fmt: skip
    for symbol in ("AAPL", "MSFT"):
        run_pt("--port", port, "instrument", "add", symbol, "--type", "equity")
        run_pt("--port", port, "buy", symbol, "-a", "B", "--qty", "10", "--price", "100.00",
               "--date", "2024-01-03")  # fmt: skip
        for on in ("2024-01-04", "2024-01-05"):
            run_pt("--port", port, "price", "set", symbol, "--date", on, "--price", "101.00",
                   "--valuation-level", "1")  # fmt: skip
    run_pt("--port", port, "value", "--from", "2024-01-04", "--to", "2024-01-05")
    return port


def _txn(port: str, txn_type: str) -> str:
    con = sqlite3.connect(port)
    try:
        row = con.execute(
            'SELECT MIN(txn_id) FROM "transaction" WHERE txn_type = ?', (txn_type,)
        ).fetchone()
        return str(row[0])
    finally:
        con.close()


def _leg(port: str, symbol: str) -> str:
    con = sqlite3.connect(port)
    try:
        row = con.execute(
            "SELECT l.leg_id FROM position_leg l JOIN instrument i USING (instrument_id) "
            "WHERE i.symbol = ?",
            (symbol,),
        ).fetchone()
        return str(row[0])
    finally:
        con.close()


#: One of each kind of write, with arguments the real run accepts.
WRITES: list[tuple[str, ...]] = [
    tuple(command.split())
    for command in (
        "account add --name C --type taxable --opened 2024-01-02",
        "account close IRA --date 2024-02-01",
        "account tax-rates set -a B --short 0.35 --long 0.15 --effective-from 2025-01-01",
        "instrument add NVDA --type equity",
        "cash deposit -a B --amount 50 --date 2024-01-04",
        "cash transfer --from IRA --to B --amount 10 --date 2024-01-06",
        "buy AAPL -a B --qty 1 --price 100.00 --date 2024-01-04",
        "sell MSFT -a B --qty 5 --price 110.00 --date 2024-01-08",
        "income dividend AAPL -a B --amount 5.00 --ex-date 2024-01-04 --pay-date 2024-01-10",
        "price set AAPL --date 2024-01-08 --price 105.00",
        "value --date 2024-01-08",
        "policy set --effective-from 2024-01-01 --large-flow-pct 0.10 --restate",
        "ca split AAPL --ratio 2:1 --ex-date 2024-01-08",
        "ca symbol-change MSFT --to MSFX --date 2024-01-08",
        "rebuild",
    )
]


@pytest.mark.parametrize("args", WRITES, ids=lambda a: " ".join(a[:2]))
def test_a_dry_run_writes_nothing_and_says_so(
    run_pt: CliRunner, book: str, args: tuple[str, ...]
) -> None:
    before = _contents(book)

    result = run_pt("--port", book, "--dry-run", *args).json()

    assert _contents(book) == before
    assert result["data"]["dry_run"] is True
    assert "DRY RUN: nothing was written to the portfolio." in result["warnings"]


def test_reverse_regroup_and_ungroup_rehearse_too(run_pt: CliRunner, book: str) -> None:
    """The ledger writes that name other rows by id."""
    before = _contents(book)
    into = run_pt("--port", book, "position", "list").data["rows"][0]["position_id"]

    for args in (
        ("trade", "reverse", _txn(book, "buy"), "--date", "2024-01-08"),
        ("position", "group", _leg(book, "MSFT"), "--into", str(into), "--date", "2024-01-08"),
    ):
        assert run_pt("--port", book, "--dry-run", *args).data["dry_run"] is True
        assert _contents(book) == before


def test_a_dry_run_reports_the_snapshots_it_would_remove(run_pt: CliRunner, book: str) -> None:
    """Before, only `pt import batch` could say this."""
    warnings = run_pt("--port", book, "--dry-run", "cash", "deposit", "-a", "B",
                      "--amount", "50", "--date", "2024-01-04").json()["warnings"]  # fmt: skip

    assert [w for w in warnings if w.startswith("would remove 2 valuation snapshots of B")]


def test_a_dry_run_refuses_what_the_real_run_refuses(run_pt: CliRunner, book: str) -> None:
    """Both of these were accepted by a dry run."""
    run_pt("--port", book, "sell", "AAPL", "-a", "B", "--qty", "10", "--price", "120.00",
           "--date", "2024-01-08")  # fmt: skip

    restating = run_pt("--port", book, "--dry-run", "account", "tax-rates", "set", "-a", "B",
                       "--short", "0.35", "--long", "0.15", "--effective-from", "2024-01-05",
                       expect=4).json()  # fmt: skip
    assert restating["error"]["code"] == "PT-E-RESTATES-HISTORY"

    run_pt("--port", book, "cash", "deposit", "-a", "B", "--amount", "1", "--date",
           "2024-01-09", "--ref", "dup")  # fmt: skip
    duplicate = run_pt("--port", book, "--dry-run", "cash", "deposit", "-a", "B",
                       "--amount", "1", "--date", "2024-01-09", "--ref", "dup",
                       expect=4).json()  # fmt: skip
    assert duplicate["error"]["code"] == "PT-E-DUPLICATE-REF"


def test_a_dry_run_needs_no_confirmation(run_pt: CliRunner, book: str) -> None:
    """`pt account close` prompts; a rehearsal keeps nothing, so it does not."""
    run_pt("--port", book, "--dry-run", "account", "close", "IRA")
