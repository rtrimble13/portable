"""`pt import reconstruct` through the real CLI.

What matters at this boundary is that the *qualification* travels with the
numbers. A table of cutover positions where the reader cannot tell an exact
basis from a solved one is worse than no table: it is the silently-wrong-number
failure with a plausible magnitude and the right units.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from portable_core.lint._common import repo_root
from tests.integration.conftest import CliRunner

pytestmark = [pytest.mark.integration]

EXAMPLE = repo_root() / "examples" / "importers" / "example-brokerage"

SOURCE = """
broker = "acme"
name = "Acme Custody"

[format]
dates = ["%m/%d/%Y"]

[cash_equivalents]
default = ["SWEEP"]

[documents.holdings]
file = "holdings.csv"
[documents.holdings.columns]
as_of = "As Of"
account = "Account"
identifier = "Symbol"
quantity = "Quantity"
market_value = "Market Value"
cost_basis = "Cost Basis"

[documents.transactions]
file = "transactions.csv"
[documents.transactions.columns]
trade_date = "Trade Date"
account = "Account"
activity = "Activity"
identifier = "Symbol"
quantity = "Quantity"
amount = "Amount"

# Declared, so the adapter does not strip the column. Without this the basis
# never reaches the reconstruction at all — which is the capability model
# working, and would make this fixture test the wrong refusal.
[[capability]]
name = "cost_basis"
document = "holdings"
field = "cost_basis"
check = "populated"
"""

ACTIVITY = """
[[activity]]
match = "Bought"
txn_type = "buy"
quantity = "positive"
cash = "negative"

[[activity]]
match = "Sold"
txn_type = "sell"
quantity = "negative"
cash = "positive"
"""


@pytest.fixture
def adapter(tmp_path: Path) -> Path:
    root = tmp_path / "acme"
    root.mkdir()
    (root / "source.toml").write_text(textwrap.dedent(SOURCE), encoding="utf-8")
    (root / "activity_map.toml").write_text(textwrap.dedent(ACTIVITY), encoding="utf-8")
    (root / "holdings.csv").write_text(
        "As Of,Account,Symbol,Quantity,Market Value,Cost Basis\n"
        "06/30/2026,Main,AAPL,10,2000.00,1000.00\n"
        "06/30/2026,Main,SWEEP,500.00,500.00,500.00\n",
        encoding="utf-8",
    )
    # AAPL rolls back to a 90-share block, all 90 of which are then sold: the
    # 10 held today were bought afterwards, so nothing of the block survives to
    # anchor a solve. KO is liquidated entirely — the other no-anchor case.
    (root / "transactions.csv").write_text(
        "Trade Date,Account,Activity,Symbol,Quantity,Amount\n"
        "03/01/2025,Main,Sold,AAPL,90,18000.00\n"
        "04/01/2025,Main,Sold,KO,40,2400.00\n"
        "05/01/2025,Main,Bought,AAPL,10,1000.00\n",
        encoding="utf-8",
    )
    return root


def test_it_reports_the_cutover_state_and_writes_nothing(
    run_pt: CliRunner, tmp_path: Path
) -> None:
    data = run_pt("import", "reconstruct", str(EXAMPLE)).ok().data
    assert data["cutover"] == "2025-01-21"
    assert data["as_of"] == "2026-06-30"
    assert data["by_source"] == {"reconstructed": 1, "estimated": 1}
    assert not list(tmp_path.glob("*.port"))


def test_the_exact_share_is_a_decimal_string_not_a_float(run_pt: CliRunner) -> None:
    """`Decimal` serializes as a string in JSON, never as a float."""
    share = run_pt("import", "reconstruct", str(EXAMPLE)).ok().data["exact_basis_share"]
    assert isinstance(share, str)
    assert share.startswith("0.58")


def test_a_block_with_no_anchor_reports_null_basis_not_zero(
    run_pt: CliRunner, adapter: Path
) -> None:
    """The distinction the whole ADR turns on.

    Zero would say the custodian's evidence supports a basis of nothing; null
    says it supports no figure at all.
    """
    result = run_pt("import", "reconstruct", str(adapter)).ok()
    assert result.data["by_source"] == {"unavailable": 2}
    assert result.data["exact_basis_share"] is None

    for row in result.data["positions"]:
        assert row["cost_basis"] is None
        assert row["basis_source"] == "unavailable"
        assert "nothing of it survives to anchor a solve" in row["assumption"]
        assert "not a basis claim" in row["assumption"]


def test_a_liquidated_position_is_marked_as_no_longer_held(
    run_pt: CliRunner, adapter: Path
) -> None:
    rows = run_pt("import", "reconstruct", str(adapter)).ok().data["positions"]
    by_symbol = {row["identifier"]: row for row in rows}
    assert by_symbol["KO"]["still_held"] is False
    assert by_symbol["KO"]["quantity"] == "40"
    assert by_symbol["AAPL"]["still_held"] is True


def test_the_ladder_travels_in_the_machine_readable_output(
    run_pt: CliRunner, adapter: Path
) -> None:
    """The qualification is an envelope field, not a rendered string.

    Same reason the performance disclaimer is one: a consumer must not be able
    to drop it without noticing. A table footnote alone would vanish the moment
    anybody read this with `--format json`, which is how an agent reads it.
    """
    notes = " ".join(run_pt("import", "reconstruct", str(adapter)).ok().data["notes"])
    assert "Quantities are exact" in notes
    assert "not a basis claim" in notes
    assert "excludes those dispositions from every total" in notes


def test_uncertain_dispositions_are_enumerated(run_pt: CliRunner) -> None:
    """Counted would not let the reader review them individually."""
    items = run_pt("import", "reconstruct", str(EXAMPLE)).ok().data["uncertain_dispositions"]
    assert items == [
        {
            "account": "Brokerage",
            "identifier": "MSFT",
            "trade_date": "2025-04-15",
            "quantity": "10",
            "days_after_cutover": 84,
        }
    ]


def test_the_cutover_can_be_moved(run_pt: CliRunner) -> None:
    """A later cutover trades track record for precision — a real choice."""
    data = run_pt("import", "reconstruct", str(EXAMPLE), "--cutover", "2025-03-31").ok().data
    assert data["cutover"] == "2025-03-31"
    # The January contribution and purchase now precede the cutover, so VXUS is
    # seeded rather than opened after it.
    assert data["opened_after"] == []


def test_a_cutover_past_the_history_exits_four(run_pt: CliRunner) -> None:
    result = run_pt("import", "reconstruct", str(EXAMPLE), "--cutover", "2030-01-01", expect=4)
    assert result.json()["error"]["code"] == "PT-E-IMPORT-SOURCE"


def test_a_negative_roll_back_is_reported_as_a_finding(
    run_pt: CliRunner, adapter: Path
) -> None:
    """The completeness check, at the boundary the user sees."""
    (adapter / "transactions.csv").write_text(
        "Trade Date,Account,Activity,Symbol,Quantity,Amount\n"
        "03/01/2025,Main,Bought,AAPL,100,20000.00\n",
        encoding="utf-8",
    )
    findings = run_pt("import", "reconstruct", str(adapter)).ok().data["findings"]
    assert [f["kind"] for f in findings] == ["negative_rollback"]
    assert "corporate action" in findings[0]["detail"]


def test_the_output_is_identical_across_runs(run_pt: CliRunner) -> None:
    """Invariant 6, through the real CLI."""
    first = run_pt("import", "reconstruct", str(EXAMPLE)).ok().json()
    second = run_pt("import", "reconstruct", str(EXAMPLE)).ok().json()
    for payload in (first, second):
        payload.pop("generated_at")
    assert first == second
