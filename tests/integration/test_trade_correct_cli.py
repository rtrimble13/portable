"""`pt trade correct`: a reversing entry and the trade it should have been, in one step.

Issue 38. The ledger is append-only (`CLAUDE.md` invariant 2), so a mistake is
corrected by reversing it and recording the trade again. Done as two commands,
a failure between them leaves the trade reversed and nothing in its place;
`pt trade correct` writes both or neither.
"""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import CliRunner

pytestmark = pytest.mark.integration


def _setup(run_pt: CliRunner, portfolio: Path) -> tuple[str, dict[str, int]]:
    """Two buys of XYZ, at 50 and at 60, and a sale of 50 typed at 7.00, not 70.00."""
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    ids = {
        "buy_50": run_pt("--port", port, "buy", "XYZ", "-a", "B", "--qty", "100", "--price",
                         "50", "--date", "2024-02-01").data["txn_id"],
        "buy_60": run_pt("--port", port, "buy", "XYZ", "-a", "B", "--qty", "100", "--price",
                         "60", "--date", "2024-03-01").data["txn_id"],
        "sale": run_pt("--port", port, "sell", "XYZ", "-a", "B", "--qty", "50", "--price",
                       "7.00", "--date", "2024-04-01", "--note", "typed from the confirm",
                       "--ref", "CONFIRM-1").data["txn_id"],
    }  # fmt: skip
    return port, ids


def _ledger(run_pt: CliRunner, port: str) -> list[dict[str, Any]]:
    return run_pt("--port", port, "trade", "list").data["rows"]  # type: ignore[no-any-return]


def _gain(run_pt: CliRunner, port: str) -> D:
    return D(run_pt("--port", port, "tax", "--year", "2024").data["total_gain"])


def test_a_mistyped_price_is_corrected_in_one_step(run_pt: CliRunner, portfolio: Path) -> None:
    """FIFO: 50 from the lot at 50, sold at 70, is a gain of 1,000.00."""
    port, ids = _setup(run_pt, portfolio)
    assert _gain(run_pt, port) == D("-2150.00")

    data = run_pt("--port", port, "--as-of", "2024-04-05", "trade", "correct", str(ids["sale"]),
                  "--price", "70.00", "--reason", "price typo").data  # fmt: skip

    assert data["corrects_txn_id"] == ids["sale"]
    assert data["changed"]["price"] == {"was": "7.00", "now": "70.00"}
    assert data["changed"]["net_cash_effect"] == {"was": "350.00", "now": "3500.00"}
    assert set(data["changed"]) == {"price", "net_cash_effect"}
    assert _gain(run_pt, port) == D("1000.00")
    run_pt("--port", port, "validate")

    rows = {row["txn_id"]: row for row in _ledger(run_pt, port)}
    reversal = run_pt("--port", port, "trade", "show", str(data["reversal_txn_id"])).data
    assert reversal["reverses_txn_id"] == ids["sale"]
    assert reversal["note"] == "price typo"
    assert rows[ids["sale"]]["price"] == "7.00", "the original stays, reversed, not edited"
    assert rows[data["txn_id"]]["price"] == "70.00"


def test_what_is_not_restated_is_kept_and_the_reference_is_not(
    run_pt: CliRunner, portfolio: Path
) -> None:
    port, ids = _setup(run_pt, portfolio)
    data = run_pt("--port", port, "trade", "correct", str(ids["sale"]), "--qty", "40").data
    corrected = run_pt("--port", port, "trade", "show", str(data["txn_id"])).data

    assert (corrected["quantity"], corrected["price"]) == ("40", "7.00")
    assert corrected["trade_date"] == "2024-04-01"
    assert corrected["note"] == "typed from the confirm"
    # The original keeps CONFIRM-1, and a reference names one row per account.
    assert corrected["external_ref"] is None


def test_a_dry_run_writes_neither_entry(run_pt: CliRunner, portfolio: Path) -> None:
    port, ids = _setup(run_pt, portfolio)
    before = _ledger(run_pt, port)
    data = run_pt("--port", port, "--dry-run", "trade", "correct", str(ids["sale"]),
                  "--price", "70.00").data  # fmt: skip
    assert data["changed"]["price"] == {"was": "7.00", "now": "70.00"}
    assert _ledger(run_pt, port) == before
    assert _gain(run_pt, port) == D("-2150.00")


def test_a_refused_correction_leaves_the_original_in_effect(
    run_pt: CliRunner, portfolio: Path
) -> None:
    """A fee with no class is refused (PORT-GIPS-D01), and so is the reversal with it."""
    port, ids = _setup(run_pt, portfolio)
    before = _ledger(run_pt, port)
    error = run_pt("--port", port, "trade", "correct", str(ids["sale"]), "--fees", "5",
                   expect=4).json()["error"]  # fmt: skip
    assert error["code"] == "PT-E-FEE-CLASS-MISSING"
    assert _ledger(run_pt, port) == before


def test_a_designation_travels_unless_the_relief_is_restated(
    run_pt: CliRunner, portfolio: Path
) -> None:
    """Spec-ID on the lot at 60: sold at 70, a gain of 500.00. FIFO would give 1,000.00."""
    port, ids = _setup(run_pt, portfolio)
    lots = run_pt("--port", port, "lot", "list", "XYZ").data["rows"]
    at_60 = next(lot["lot_id"] for lot in lots if D(lot["basis"]) == D("6000.00"))
    designated = run_pt("--port", port, "trade", "correct", str(ids["sale"]),
                        "--lots", f"{at_60}:50").data  # fmt: skip

    priced = run_pt("--port", port, "trade", "correct", str(designated["txn_id"]),
                    "--price", "70.00").data  # fmt: skip
    assert _gain(run_pt, port) == D("500.00"), "the designation was kept"

    run_pt("--port", port, "trade", "correct", str(priced["txn_id"]), "--method", "fifo")
    assert _gain(run_pt, port) == D("1000.00"), "restating the method dropped it"


def test_only_a_trade_in_effect_can_be_corrected(run_pt: CliRunner, portfolio: Path) -> None:
    port, ids = _setup(run_pt, portfolio)
    deposit = _ledger(run_pt, port)[0]
    assert deposit["txn_type"] == "deposit"
    error = run_pt("--port", port, "trade", "correct", str(deposit["txn_id"]), "--price", "1",
                   expect=4).json()["error"]  # fmt: skip
    assert "not a trade" in error["message"]

    run_pt("--port", port, "trade", "correct", str(ids["sale"]), "--price", "70.00")
    again = run_pt("--port", port, "trade", "correct", str(ids["sale"]), "--price", "71.00",
                   expect=4).json()["error"]  # fmt: skip
    assert "already reversed" in again["message"]
    twice = run_pt("--port", port, "trade", "reverse", str(ids["sale"]), expect=4)
    assert "already reversed" in twice.json()["error"]["message"]
