"""Config rows dated into history, through the real CLI. ADR 0025.

Reproduced before the fix: a tax rate schedule effective before a recorded sale
was stored, and the sale's estimated tax was not. `pt tax` went on reporting
the tax at the old rate, with no warning, until something happened to rebuild;
only `pt validate` noticed. A return policy did the same to the large-flow
flags of stored snapshots. A second row for the same date failed with an
unexpected IntegrityError.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner

pytestmark = pytest.mark.integration


def _query(port: str, sql: str) -> list[tuple[object, ...]]:
    con = sqlite3.connect(port)
    try:
        return [tuple(row) for row in con.execute(sql)]
    finally:
        con.close()


@pytest.fixture
def sold(run_pt: CliRunner, portfolio: Path) -> str:
    """A 1,000.00 short-term gain in B, taxed at 0.37 + 0.05 + 0.038."""
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", "AAPL", "--type", "equity")
    run_pt("--port", port, "buy", "AAPL", "-a", "B", "--qty", "10", "--price", "100.00",
           "--date", "2024-01-03")  # fmt: skip
    run_pt("--port", port, "sell", "AAPL", "-a", "B", "--qty", "10", "--price", "200.00",
           "--date", "2024-06-03")  # fmt: skip
    return port


def _tax(run_pt: CliRunner, port: str) -> str:
    return str(run_pt("--port", port, "tax", "--year", "2024").data["total_estimated_tax"])


def _rates(port: str, state: str, on: str, *extra: str) -> tuple[str, ...]:
    return ("--port", port, "account", "tax-rates", "set", "-a", "B", "--short", "0.37",
            "--long", "0.20", "--state", state, "--niit", "0.038", "--effective-from", on,
            *extra)  # fmt: skip


def test_a_back_dated_tax_rate_is_refused_naming_what_it_changes(
    run_pt: CliRunner, sold: str
) -> None:
    """Accepted before, and `pt tax` kept reporting 458.00."""
    assert _tax(run_pt, sold) == "458.00"

    error = run_pt(*_rates(sold, "0.0575", "2024-03-01"), expect=4).json()["error"]

    assert error["code"] == "PT-E-RESTATES-HISTORY"
    assert error["message"] == (
        "a tax rate schedule effective 2024-03-01 changes the estimated tax on 1 realized "
        "gain in B dated 2024-06-03 (458.00 becomes 465.50)"
    )
    assert _tax(run_pt, sold) == "458.00"
    assert _query(sold, "SELECT COUNT(*) FROM tax_rate_schedule") == [(1,)]


def test_restate_records_the_rate_and_the_tax_it_implies(run_pt: CliRunner, sold: str) -> None:
    result = run_pt(*_rates(sold, "0.0575", "2024-03-01", "--restate")).json()

    assert result["data"]["restated"] is True
    assert result["warnings"] == [
        "restated the estimated tax on 1 realized gain in B dated 2024-06-03 "
        "(458.00 becomes 465.50)"
    ]
    assert _tax(run_pt, sold) == "465.50"
    run_pt("--port", sold, "validate")


def test_a_rate_for_a_later_date_changes_nothing_and_needs_no_flag(
    run_pt: CliRunner, sold: str
) -> None:
    result = run_pt(*_rates(sold, "0.0575", "2024-07-01")).json()

    assert result["data"]["restated"] is False
    assert _tax(run_pt, sold) == "458.00"
    run_pt("--port", sold, "validate")


def test_a_second_schedule_for_one_date_is_refused_by_name(
    run_pt: CliRunner, sold: str
) -> None:
    """It was an unexpected IntegrityError, exit 1."""
    error = run_pt(*_rates(sold, "0.05", "2024-01-01"), expect=4).json()["error"]

    assert error["message"] == "B already has a tax rate schedule effective 2024-01-01"


@pytest.fixture
def valued(run_pt: CliRunner, portfolio: Path) -> str:
    """The fixture's 500,000.00 deposit on 2024-01-02, valued with no policy."""
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", "AAPL", "--type", "equity")
    run_pt("--port", port, "buy", "AAPL", "-a", "B", "--qty", "10", "--price", "100.00",
           "--date", "2024-01-03")  # fmt: skip
    run_pt("--port", port, "price", "set", "AAPL", "--date", "2024-01-03", "--price", "101.00",
           "--valuation-level", "1")  # fmt: skip
    run_pt("--port", port, "value", "--from", "2024-01-02", "--to", "2024-01-03")
    return port


def _policy(port: str, on: str, *extra: str) -> tuple[str, ...]:
    return ("--port", port, "policy", "set", "--effective-from", on,
            "--large-flow-amount", "100000", *extra)  # fmt: skip


def _large_flags(port: str) -> list[tuple[object, ...]]:
    return _query(port, "SELECT flow_date, is_large FROM snapshot_flow ORDER BY flow_date")


def test_port_gips_b03_a_back_dated_policy_is_refused_naming_the_snapshots(
    run_pt: CliRunner, valued: str
) -> None:
    error = run_pt(*_policy(valued, "2024-01-01"), expect=4).json()["error"]

    assert error["code"] == "PT-E-RESTATES-HISTORY"
    assert error["message"] == (
        "a return policy effective 2024-01-01 changes the large-flow flags of 1 valuation "
        "snapshot of B dated 2024-01-02"
    )
    assert _large_flags(valued) == [("2024-01-02", 0)]
    assert _query(valued, "SELECT COUNT(*) FROM return_policy") == [(0,)]


def test_port_gips_b03_restate_keeps_the_prices_the_snapshots_recorded(
    run_pt: CliRunner, valued: str
) -> None:
    prices = _query(valued, "SELECT * FROM valuation_snapshot_price ORDER BY snapshot_id")

    result = run_pt(*_policy(valued, "2024-01-01", "--restate")).json()

    assert result["warnings"] == [
        "restated the large-flow flags of 1 valuation snapshot of B dated 2024-01-02, "
        "keeping the prices they recorded"
    ]
    assert _large_flags(valued) == [("2024-01-02", 1)]
    assert _query(valued, "SELECT * FROM valuation_snapshot_price ORDER BY snapshot_id") == (
        prices
    )
    run_pt("--port", valued, "validate")


def test_a_policy_after_every_snapshot_needs_no_flag(run_pt: CliRunner, valued: str) -> None:
    result = run_pt(*_policy(valued, "2024-02-01")).json()

    assert result["data"]["restated"] is False
    run_pt("--port", valued, "validate")
