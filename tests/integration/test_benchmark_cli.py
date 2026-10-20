"""`pt benchmark show`, through the real CLI (ADR 0030)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner

pytestmark = pytest.mark.integration


@pytest.mark.gips
def test_port_gips_g02_a_new_portfolio_is_benchmarked_against_cash_at_zero(
    run_pt: CliRunner, portfolio: Path
) -> None:
    """The default, named and explained, in the envelope an agent reads."""
    shown = run_pt("--port", str(portfolio), "benchmark", "show")

    assert shown.data["in_effect"] == "Cash (0%)"
    assert shown.data["is_default"] is True
    assert "zero in every period" in shown.data["description"]
    assert any("No benchmark has been assigned" in d for d in shown.data["disclosures"])
    assert shown.data["rows"] == [
        {
            "effective_from": None,
            "benchmark": "Cash (0%)",
            "return_type": "total_return",
            "default": True,
            "change_type": None,
        }
    ]
