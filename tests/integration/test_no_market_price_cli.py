"""An instrument nothing quotes, through the real CLI. ADR 0028, ADR 0020 R7.

Before: a contingent value right could not be priced, so every snapshot of the
account holding it was incomplete, and an incomplete snapshot is never used for
a return. One unquotable holding blocked performance for its whole account.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import CliRunner

pytestmark = pytest.mark.integration

ON = "2024-01-04"


@pytest.fixture
def held(run_pt: CliRunner, portfolio: Path) -> str:
    """B holds AAPL, which has a price, and CVR, which never will."""
    port = str(portfolio)
    for symbol in ("AAPL", "CVR"):
        run_pt("--port", port, "instrument", "add", symbol, "--type", "equity")
        run_pt("--port", port, "buy", symbol, "-a", "B", "--qty", "10", "--price", "1.00",
               "--date", "2024-01-03")  # fmt: skip
    run_pt("--port", port, "price", "set", "AAPL", "--date", ON, "--price", "101.00",
           "--valuation-level", "1")  # fmt: skip
    return port


def _snapshot(port: str) -> dict[str, Any]:
    con = sqlite3.connect(port)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT snapshot_id, ending_market_value, securities_value, is_complete, "
            "level5_market_value FROM valuation_snapshot WHERE snapshot_date = ?",
            (ON,),
        ).fetchone()
        prices = [
            dict(p)
            for p in con.execute(
                "SELECT i.symbol, p.price, p.market_value, p.source, p.valuation_level, "
                "p.price_id FROM valuation_snapshot_price p JOIN instrument i "
                "USING (instrument_id) WHERE snapshot_id = ? ORDER BY i.symbol",
                (row["snapshot_id"],),
            )
        ]
        return {**dict(row), "prices": prices}
    finally:
        con.close()


def test_an_unquoted_holding_left_the_snapshot_incomplete(run_pt: CliRunner, held: str) -> None:
    """The problem, stated: what the flag exists to change."""
    run_pt("--port", held, "value", "--date", ON)

    assert _snapshot(held)["is_complete"] == 0


def test_port_gips_a02_marked_it_is_valued_at_zero_at_level_5(
    run_pt: CliRunner, held: str
) -> None:
    run_pt("--port", held, "instrument", "market-price", "CVR", "--none")
    run_pt("--port", held, "value", "--date", ON)

    snapshot = _snapshot(held)
    assert snapshot["is_complete"] == 1
    assert snapshot["securities_value"] == "1010.00"
    assert snapshot["level5_market_value"] == "0.00"
    assert snapshot["prices"][1] == {
        "symbol": "CVR",
        "price": "0.00",
        "market_value": "0.00",
        "source": "none: no observable market price",
        "valuation_level": 5,
        "price_id": None,
    }
    run_pt("--port", held, "validate")


def test_marking_a_valued_holding_restates_its_snapshots_only_on_request(
    run_pt: CliRunner, held: str
) -> None:
    run_pt("--port", held, "value", "--date", ON)

    refused = run_pt("--port", held, "instrument", "market-price", "CVR", "--none",
                     expect=4).json()["error"]  # fmt: skip
    assert refused["code"] == "PT-E-RESTATES-HISTORY"
    assert refused["message"] == (
        f"marking CVR as having no market price changes 1 valuation snapshot of B dated {ON}"
    )
    assert _snapshot(held)["is_complete"] == 0

    run_pt("--port", held, "instrument", "market-price", "CVR", "--none", "--restate")
    assert _snapshot(held)["is_complete"] == 1
    run_pt("--port", held, "validate")

    run_pt("--port", held, "instrument", "market-price", "CVR", "--quoted", "--restate")
    assert _snapshot(held)["is_complete"] == 0
    run_pt("--port", held, "validate")


def test_an_instrument_that_has_had_a_price_cannot_be_marked(
    run_pt: CliRunner, held: str
) -> None:
    """The mark applies to every date, so it would revalue those at zero too."""
    error = run_pt("--port", held, "instrument", "market-price", "AAPL", "--none",
                   expect=4).json()["error"]  # fmt: skip

    assert error["message"].startswith(f"AAPL has 1 price(s), {ON}; marked as having")


def test_a_marked_instrument_takes_no_price(run_pt: CliRunner, held: str) -> None:
    run_pt("--port", held, "instrument", "market-price", "CVR", "--none")

    error = run_pt("--port", held, "price", "set", "CVR", "--date", ON, "--price", "1.00",
                   expect=4).json()["error"]  # fmt: skip

    assert "marked as having no market price" in error["message"]


def test_an_instrument_can_be_marked_when_it_is_added(
    run_pt: CliRunner, portfolio: Path
) -> None:
    added = run_pt("--port", str(portfolio), "instrument", "add", "CVR2", "--type", "equity",
                   "--no-market-price").data  # fmt: skip

    assert added["has_market_price"] is False
    shown = run_pt("--port", str(portfolio), "instrument", "show", "CVR2").data
    assert shown["has_market_price"] is False
