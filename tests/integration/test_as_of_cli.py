"""Reports for a past date read the book as it stood on that date.

ADR 0021, through the real CLI. The reproduction: 500,000 deposited, 100 AAPL
bought 2024-01-03 and sold 2025-03-03, 10 MSFT bought 2025-03-04. On
2024-06-30 the account held 100 AAPL and 485,000 cash. Before ADR 0021 every
command below reported today's 10 MSFT and 501,000 cash for that date -- and
`pt value` stored it as a complete snapshot.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner

pytestmark = pytest.mark.integration

D = Decimal
PAST = "2024-06-30"


def _trade(
    run_pt: CliRunner, port: str, verb: str, symbol: str, qty: str, price: str, on: str
) -> None:
    run_pt(
        "--port", port, verb, symbol, "--qty", qty, "--price", price, "--date", on, "-a", "B"
    )


def _price(run_pt: CliRunner, port: str, symbol: str, price: str, on: str) -> None:
    run_pt(
        "--port", port, "price", "set", symbol, "--price", price, "--date", on,
        "--valuation-level", "1",
    )  # fmt: skip


@pytest.fixture
def book(run_pt: CliRunner, portfolio: Path) -> str:
    """The reproduction from ADR 0021, priced on 2024-06-28 and around the sale."""
    port = str(portfolio)
    for symbol in ("AAPL", "MSFT"):
        run_pt("--port", port, "instrument", "add", symbol, "--type", "equity")
    _trade(run_pt, port, "buy", "AAPL", "100", "150.00", "2024-01-03")
    _trade(run_pt, port, "sell", "AAPL", "100", "200.00", "2025-03-03")
    _trade(run_pt, port, "buy", "MSFT", "10", "400.00", "2025-03-04")
    _price(run_pt, port, "AAPL", "155.00", "2024-06-28")
    _price(run_pt, port, "MSFT", "300.00", "2024-06-28")
    _price(run_pt, port, "AAPL", "198.00", "2025-03-02")
    _price(run_pt, port, "MSFT", "390.00", "2025-03-04")
    return port


@pytest.mark.gips
def test_value_on_a_past_date_values_the_positions_held_then(
    run_pt: CliRunner, book: str
) -> None:
    """PORT-GIPS-A01 -- fair value on the measurement date, of what was held on it.

    The old answer was securities 3,000 (MSFT, not yet bought), cash 501,000,
    complete. A wrong number the return engine would have trusted.
    """
    row = run_pt("--port", book, "value", "--date", PAST).data["rows"][0]

    assert row["securities"] == "15500.00", "100 AAPL at 155.00"
    assert row["cash"] == "485000.00"
    assert row["ending"] == "500500.00"
    assert row["complete"] is True


def test_a_range_sees_each_trade_on_its_own_day(run_pt: CliRunner, book: str) -> None:
    """One walk, three days: held, sold, re-invested."""
    rows = run_pt("--port", book, "value", "--from", "2025-03-02", "--to", "2025-03-04").data[
        "rows"
    ]

    assert [(r["date"], r["securities"], r["cash"], r["complete"]) for r in rows] == [
        ("2025-03-02", "19800.00", "485000.00", True),
        ("2025-03-03", "0.00", "505000.00", True),
        ("2025-03-04", "3900.00", "501000.00", True),
    ]


def test_holdings_as_of_a_past_date_lists_what_was_held_then(
    run_pt: CliRunner, book: str
) -> None:
    held = run_pt("--port", book, "--as-of", PAST, "holdings").data

    assert {r["symbol"]: r["quantity"] for r in held["rows"]} == {"AAPL": "100", "CASH": None}
    assert held["total_market_value"] == "500500.00"


def test_pnl_as_of_a_past_date_has_no_gain_realized_after_it(
    run_pt: CliRunner, book: str
) -> None:
    """The 2025 sale had not happened on 2024-06-30; AAPL was 500 up."""
    pnl = run_pt("--port", book, "--as-of", PAST, "pnl").data

    assert pnl["realized"] == "0.00"
    assert pnl["unrealized"] == "500.00"


def test_lot_list_as_of_a_past_date_lists_the_lot_sold_since(
    run_pt: CliRunner, book: str
) -> None:
    lots = run_pt("--port", book, "--as-of", PAST, "lot", "list").data["rows"]

    assert [(r["symbol"], r["quantity"]) for r in lots] == [("AAPL", "100")]


def test_mark_on_a_past_date(run_pt: CliRunner, book: str) -> None:
    marked = run_pt("--port", book, "mark", "--date", PAST).data

    assert marked["total_market_value"] == "500500.00"


def test_reading_the_past_leaves_the_present_untouched(run_pt: CliRunner, book: str) -> None:
    """Every as-of read rolls its rewind back; `pt validate` would see one that didn't."""
    run_pt("--port", book, "--as-of", PAST, "holdings")
    run_pt("--port", book, "--as-of", PAST, "pnl")
    run_pt("--port", book, "--as-of", PAST, "lot", "list")
    run_pt("--port", book, "mark", "--date", PAST)

    run_pt("--port", book, "validate")
    now = run_pt("--port", book, "holdings").data
    assert {r["symbol"]: r["quantity"] for r in now["rows"]} == {"MSFT": "10", "CASH": None}


@pytest.mark.gips
def test_a_reversed_deposit_leaves_no_phantom_flow(run_pt: CliRunner, portfolio: Path) -> None:
    """PORT-GIPS-B02 -- a snapshot's flows and its value come from the same rows.

    A 50,000 deposit keyed in error on 2024-02-01 and reversed on 2024-02-20.
    The snapshot's cash never included it, so its flows must not either. The
    old snapshot recorded a 50,000 contribution on 2024-02-01 that never
    reached market value: a 50,000 loss that did not happen.
    """
    port = str(portfolio)
    mistake = run_pt(
        "--port",
        port,
        "cash",
        "deposit",
        "-a",
        "B",
        "--amount",
        "50000",
        "--date",
        "2024-02-01",
    ).data["txn_id"]
    run_pt(
        "--port", port, "trade", "reverse", str(mistake), "--date", "2024-02-20",
        "--note", "keyed against the wrong account",
    )  # fmt: skip

    row = run_pt("--port", port, "value", "--date", "2024-02-01").data["rows"][0]

    assert row["cash"] == "500000.00"
    assert row["flow_account"] == "0.00"
    assert row["flow_portfolio"] == "0.00"
