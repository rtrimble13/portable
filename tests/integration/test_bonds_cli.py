"""A bond trade, through the CLI. ADR 0041 §1, §2, §4.

What each refusal prevents: a bond trade with no accrued interest leaves cash
short by it and the next coupon counted in full; a quantity that is not whole
bonds is a quantity in the wrong unit; a short bond accrues against itself and
nothing here values it.
"""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner

pytestmark = pytest.mark.integration

BOND = "T-4.25-2030"


@pytest.fixture
def bond(run_pt: CliRunner, portfolio: Path) -> str:
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", BOND, "--type", "bond", "--issuer",
           "US Treasury", "--coupon", "0.0425", "--coupon-frequency", "2",
           "--maturity", "2030-05-15", "--face", "1000")  # fmt: skip
    return port


def _buy(run_pt: CliRunner, port: str, *extra: str, expect: int = 0) -> dict[str, object]:
    return run_pt("--port", port, "buy", BOND, "--qty", "10000", "--price", "101.24",
                  "--date", "2024-06-20", "-a", "B", *extra, expect=expect).json()  # fmt: skip


def test_a_bond_trade_states_its_accrued_interest(run_pt: CliRunner, bond: str) -> None:
    error = _buy(run_pt, bond, expect=4)["error"]
    assert error["code"] == "PT-E-ACCRUED-INTEREST-REQUIRED"  # type: ignore[index]


def test_a_quantity_is_whole_bonds_of_face(run_pt: CliRunner, bond: str) -> None:
    error = run_pt("--port", bond, "buy", BOND, "--qty", "10500", "--price", "100",
                   "--date", "2024-06-20", "-a", "B", "--accrued-interest", "0",
                   expect=4).json()["error"]  # fmt: skip
    assert error["code"] == "PT-E-BOND-DENOMINATION"


def test_a_short_bond_is_refused(run_pt: CliRunner, bond: str) -> None:
    error = run_pt("--port", bond, "short", BOND, "--qty", "10000", "--price", "100",
                   "--date", "2024-06-20", "-a", "B", "--accrued-interest", "0",
                   expect=4).json()["error"]  # fmt: skip
    assert error["code"] == "PT-E-BOND-UNSUPPORTED"


def test_accrued_interest_on_a_stock_is_refused(run_pt: CliRunner, portfolio: Path) -> None:
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    error = run_pt("--port", port, "buy", "XYZ", "--qty", "10", "--price", "50", "--date",
                   "2024-06-20", "-a", "B", "--accrued-interest", "1",
                   expect=4).json()["error"]  # fmt: skip
    assert "not a bond" in error["message"]


def test_a_stated_figure_far_from_settlement_is_flagged(run_pt: CliRunner, bond: str) -> None:
    """To settlement on 06-21, 37 of 184 days: 42.73. 40.00 is two days off."""
    payload = _buy(run_pt, bond, "--settlement", "2024-06-21", "--accrued-interest", "40.00")
    assert any("42.73" in w for w in payload["warnings"])  # type: ignore[attr-defined]
    clean = _buy(run_pt, bond, "--settlement", "2024-06-21", "--accrued-interest", "42.73")
    assert not [w for w in clean["warnings"] if "accrued" in w]  # type: ignore[attr-defined]


def test_a_sale_receives_accrued_interest_outside_its_proceeds(
    run_pt: CliRunner, bond: str
) -> None:
    """Sold on 08-20 at 102.00, settling 08-21: 98 days accrued, 113.18.

    The gain is 10,200.00 - 10,124.00 = 76.00. The accrued interest is cash
    received and no part of the proceeds.
    """
    _buy(run_pt, bond, "--accrued-interest", "42.73")
    sold = run_pt("--port", bond, "sell", BOND, "--qty", "10000", "--price", "102.00",
                  "--date", "2024-08-20", "--settlement", "2024-08-21",
                  "--accrued-interest", "113.18", "-a", "B").data  # fmt: skip
    assert D(sold["net_cash_effect"]) == D("10313.18")

    tax = run_pt("--port", bond, "tax", "--year", "2024").data
    assert D(tax["total_gain"]) == D("76.00")
    assert tax["bonds"]["held"] == [BOND]
    assert any("premium" in item for item in tax["bonds"]["not_computed"])


def test_a_report_without_a_bond_says_nothing_about_bonds(
    run_pt: CliRunner, portfolio: Path
) -> None:
    assert run_pt("--port", str(portfolio), "tax", "--year", "2024").data["bonds"] is None
