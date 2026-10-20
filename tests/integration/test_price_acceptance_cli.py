"""Price history accepted against a statement, through the real CLI (ADR 0031).

`pt reconcile` compared quantities and cash, and only as they stand today. A
year-end statement is the anchor price history is accepted against, so it now
reconciles as of its own date, checks each position's price to the cent a unit,
and `--record` keeps the lines so that `pt price check` re-decides them
whenever prices change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import CliRunner

pytestmark = pytest.mark.integration

YEAR_END = "2024-12-31"


@pytest.fixture
def book(run_pt: CliRunner, portfolio: Path) -> str:
    """AAPL and MSFT held over 2024; more AAPL bought after the year-end."""
    port = str(portfolio)
    for symbol in ("AAPL", "MSFT"):
        run_pt("--port", port, "instrument", "add", symbol, "--type", "equity")
    for symbol, qty, price, on in (
        ("AAPL", "100", "100.00", "2024-03-01"),
        ("MSFT", "10", "400.00", "2024-06-03"),
        ("AAPL", "50", "200.00", "2025-02-03"),
    ):
        run_pt("--port", port, "buy", symbol, "--qty", qty, "--price", price, "--date", on,
               "-a", "B")  # fmt: skip
    for symbol, price in (("AAPL", "250.42"), ("MSFT", "421.50")):
        run_pt("--port", port, "price", "set", symbol, "--price", price, "--date",
               "2024-12-30", "--valuation-level", "1")  # fmt: skip
    return port


def _statement(tmp_path: Path, aapl_value: str = "25042.00") -> str:
    path = tmp_path / "statement-2024.csv"
    path.write_text(
        "account,symbol,quantity,market_value,cash\n"
        f"B,AAPL,100,{aapl_value},\n"
        "B,MSFT,10,4215.00,\n"
        "B,CASH,,486000.00,yes\n",
        encoding="utf-8",
    )
    return str(path)


def test_a_year_end_statement_reconciles_against_that_year_end(
    run_pt: CliRunner, book: str, tmp_path: Path
) -> None:
    """Holdings and cash as they stood then; every price checked against the statement's."""
    tied = run_pt("--port", book, "--as-of", YEAR_END, "reconcile", "--against",
                  _statement(tmp_path))  # fmt: skip

    assert tied.data["as_of"] == YEAR_END
    assert tied.data["breaks"] == 0
    assert [(p["symbol"], p["status"], p["price_date"]) for p in tied.data["prices"]] == [
        ("AAPL", "tied", "2024-12-30"),
        ("MSFT", "tied", "2024-12-30"),
    ], "valued from the newest price on or before the year-end"

    today = run_pt("--port", book, "reconcile", "--against", _statement(tmp_path), expect=6)
    assert any("AAPL" in b for b in today.json()["error"]["context"]["breaks"]), (
        "against today's book the 2025 purchase is a quantity break: the date matters"
    )


def test_a_price_from_another_day_is_a_break_named_by_line(
    run_pt: CliRunner, book: str, tmp_path: Path
) -> None:
    broken = run_pt("--port", book, "--as-of", YEAR_END, "reconcile", "--against",
                    _statement(tmp_path, "25142.00"), expect=6)  # fmt: skip

    error = broken.json()["error"]
    assert error["code"] == "PT-E-RECONCILE-BREAK"
    assert error["context"]["breaks"] == [
        "B AAPL 2024-12-31: custodian 251.420000 vs portable 250.42 "
        "(manual, 2024-12-30), difference -100.00"
    ]
    assert "pt price accept" in error["remedy"]


def test_recording_needs_the_statements_own_date(
    run_pt: CliRunner, book: str, tmp_path: Path
) -> None:
    """A defaulted date would file a year-end statement under today."""
    refused = run_pt("--port", book, "reconcile", "--against", _statement(tmp_path),
                     "--record", expect=4)  # fmt: skip
    assert "explicit --as-of" in refused.json()["error"]["message"]


def test_a_recorded_break_blocks_returns_until_accepted_and_again_when_its_price_moves(
    run_pt: CliRunner, book: str, tmp_path: Path
) -> None:
    run_pt("--port", book, "--as-of", YEAR_END, "reconcile", "--against",
           _statement(tmp_path, "25142.00"), "--record", expect=6)  # fmt: skip

    def check(expect: int) -> dict[str, Any]:
        return run_pt("--port", book, "price", "check", expect=expect).json()

    def value_warnings() -> list[str]:
        result = run_pt("--port", book, "value", "--date", YEAR_END).json()
        return [w for w in result["warnings"] if "returns are blocked" in w]

    blocked = check(6)
    assert "B 2024-12-31" in blocked["error"]["message"]
    assert value_warnings() == [
        "B: returns are blocked from 2024-12-31 -- the statement's AAPL line is break. "
        "`pt price check` lists it; fix the price or accept the difference with "
        "`pt price accept`."
    ]

    accepted = run_pt("--port", book, "price", "accept", "AAPL", "-a", "B", "--date", YEAR_END,
                      "--reason", "custodian used a later print")  # fmt: skip
    assert accepted.data["status"] == "accepted"
    assert check(0)["data"]["counts"] == {"tied": 1, "accepted": 1, "break": 0, "unpriced": 0}
    assert value_warnings() == []

    run_pt("--port", book, "price", "set", "AAPL", "--price", "250.43", "--date",
           "2024-12-30", "--valuation-level", "1")  # fmt: skip
    assert "B 2024-12-31" in check(6)["error"]["message"], "a new price is a new question"


def test_a_dry_run_records_nothing(run_pt: CliRunner, book: str, tmp_path: Path) -> None:
    run_pt("--port", book, "--as-of", YEAR_END, "--dry-run", "reconcile", "--against",
           _statement(tmp_path), "--record")  # fmt: skip

    nothing = run_pt("--port", book, "price", "check", expect=4)
    assert "no statement lines are recorded" in nothing.json()["error"]["message"]


def test_anchors_and_acceptances_travel_with_an_export(
    run_pt: CliRunner, book: str, tmp_path: Path
) -> None:
    run_pt("--port", book, "--as-of", YEAR_END, "reconcile", "--against",
           _statement(tmp_path, "25142.00"), "--record", expect=6)  # fmt: skip
    run_pt("--port", book, "price", "accept", "AAPL", "-a", "B", "--date", YEAR_END,
           "--reason", "custodian used a later print")  # fmt: skip
    exported = tmp_path / "export.json"
    run_pt("--port", book, "export", "-o", str(exported))

    copy = tmp_path / "copy.port"
    run_pt("import", "portfolio", str(exported), "--into", str(copy))

    counts = run_pt("--port", str(copy), "price", "check").data["counts"]
    assert counts == {"tied": 1, "accepted": 1, "break": 0, "unpriced": 0}
