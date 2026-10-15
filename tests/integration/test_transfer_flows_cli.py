"""A cash transfer is two flows at account level and none at portfolio level.

PORT-GIPS-B02 and ADR 0007, through the real CLI. The destination's half used
to be produced nowhere: on the day 20,000 moved from S to B, B's snapshot
showed its cash rising by 20,000 with no external flow beside it, so B's
account-level return counted the transfer as a 40% gain.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner

pytestmark = [pytest.mark.integration, pytest.mark.gips]

ON = "2024-03-01"


@pytest.fixture
def transferred(run_pt: CliRunner, portfolio: Path) -> str:
    """B (funded 500,000 by the fixture) receives 20,000 from S (funded 50,000)."""
    port = str(portfolio)
    run_pt(
        "--port", port, "account", "add", "--name", "S", "--type", "taxable",
        "--opened", "2024-01-02",
    )  # fmt: skip
    run_pt(
        "--port", port, "cash", "deposit", "-a", "S", "--amount", "50000",
        "--date", "2024-01-02",
    )  # fmt: skip
    run_pt(
        "--port", port, "cash", "transfer", "-a", "S", "--to", "B", "--amount", "20000",
        "--date", ON,
    )  # fmt: skip
    return port


def test_the_receiving_account_records_the_transfer_as_its_inflow(
    run_pt: CliRunner, transferred: str
) -> None:
    """PORT-GIPS-B02 -- -amount for the source and +amount for the destination."""
    rows = run_pt("--port", transferred, "value", "--date", ON).data["rows"]
    by_account = {r["account"]: r for r in rows}

    assert (by_account["S"]["cash"], by_account["S"]["flow_account"]) == (
        "30000.00",
        "-20000.00",
    )
    assert (by_account["B"]["cash"], by_account["B"]["flow_account"]) == (
        "520000.00",
        "20000.00",
    )
    assert by_account["S"]["flow_portfolio"] == by_account["B"]["flow_portfolio"] == "0.00", (
        "and still no flow at all at portfolio level"
    )


def test_the_account_level_series_carries_both_sides(
    run_pt: CliRunner, transferred: str
) -> None:
    """`pt cash-flows --level account` lists the destination's inflow too."""
    flows = run_pt(
        "--port", transferred, "cash-flows", "--level", "account", "--external-only",
        "--from", ON, "--to", ON,
    ).data  # fmt: skip

    assert sorted((r["account"], r["amount"]) for r in flows["rows"]) == [
        ("B", "20000.00"),
        ("S", "-20000.00"),
    ]
    assert flows["net_external_flow"] == "0.00"


def test_the_portfolio_level_series_carries_neither(
    run_pt: CliRunner, transferred: str
) -> None:
    """The netting rule ADR 0007 exists for, unchanged by the fix."""
    flows = run_pt(
        "--port", transferred, "cash-flows", "--level", "portfolio", "--external-only",
        "--from", ON, "--to", ON,
    ).data  # fmt: skip

    assert flows["rows"] == []
