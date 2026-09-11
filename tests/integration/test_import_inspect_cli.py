"""`pt import inspect` through the real CLI.

The first thing anybody runs against a new custodian, and the thing they are
supposed to read before trusting a number that comes out later. What matters at
this boundary is that the capability set is *reported* -- declared, withheld,
and what each absence costs -- rather than being an internal detail that only
shows up as a refusal months afterwards.
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

[documents.transactions]
file = "transactions.csv"
[documents.transactions.columns]
trade_date = "Trade Date"
settlement_date = "Settle Date"
account = "Account"
activity = "Activity"
identifier = "Symbol"
quantity = "Quantity"
amount = "Amount"

[[capability]]
name = "settlement_date"
document = "transactions"
field = "settlement_date"
check = "not_before_trade_date"
"""

ACTIVITY = """
[[activity]]
match = "Bought"
txn_type = "buy"
quantity = "positive"
cash = "negative"
"""

HOLDINGS = """\
As Of,Account,Symbol,Quantity
03/31/2026,Main,AAPL,100
03/31/2026,Main,SWEEP,2500.00
"""


@pytest.fixture
def adapter(tmp_path: Path) -> Path:
    """A custodian whose settlement column holds dates before its trade dates."""
    root = tmp_path / "acme"
    root.mkdir()
    (root / "source.toml").write_text(textwrap.dedent(SOURCE), encoding="utf-8")
    (root / "activity_map.toml").write_text(textwrap.dedent(ACTIVITY), encoding="utf-8")
    (root / "holdings.csv").write_text(HOLDINGS, encoding="utf-8")
    (root / "transactions.csv").write_text(
        "Trade Date,Settle Date,Account,Activity,Symbol,Quantity,Amount\n"
        "01/06/2026,01/02/2026,Main,Bought,AAPL,100,15000.00\n",
        encoding="utf-8",
    )
    return root


def test_it_reports_the_capability_set_and_writes_nothing(
    run_pt: CliRunner, tmp_path: Path
) -> None:
    result = run_pt("import", "inspect", str(EXAMPLE)).ok()
    data = result.data

    assert data["broker"] == "example-brokerage"
    assert data["accounts"] == ["Brokerage", "Roth IRA"]
    assert data["holdings"] == 5
    assert data["transactions"] == 11
    assert "cost_basis" in data["capabilities"]["declared"]
    # Nothing is created: this reads files and reports.
    assert not list(tmp_path.glob("*.port"))


def test_a_withheld_capability_says_what_it_costs(run_pt: CliRunner) -> None:
    """The consequence travels with the absence, in the machine-readable output.

    A consumer that only sees a list of declared capabilities has to know ADR
    0018's table by heart to interpret it.
    """
    withheld = {
        entry["capability"]: entry
        for entry in run_pt("import", "inspect", str(EXAMPLE))
        .ok()
        .data["capabilities"]["withheld"]
    }
    assert "lot_detail" in withheld
    assert "specific identification" in withheld["lot_detail"]["absence_means"]
    assert withheld["lot_detail"]["check"] is None


def test_a_column_that_fails_its_check_is_withheld_with_the_reason(
    run_pt: CliRunner, adapter: Path
) -> None:
    """ADR 0018 §3, at the boundary a user actually sees."""
    data = run_pt("import", "inspect", str(adapter)).ok().data
    assert "settlement_date" not in data["capabilities"]["declared"]

    finding = next(
        entry
        for entry in data["capabilities"]["withheld"]
        if entry["capability"] == "settlement_date"
    )
    assert finding["check"] == "not_before_trade_date"
    assert "before their own trade date" in finding["reason"]


def test_an_unmapped_activity_exits_four_and_quotes_the_row(
    run_pt: CliRunner, adapter: Path
) -> None:
    (adapter / "transactions.csv").write_text(
        "Trade Date,Settle Date,Account,Activity,Symbol,Quantity,Amount\n"
        "01/06/2026,01/07/2026,Main,Bought,AAPL,100,15000.00\n"
        "02/02/2026,02/04/2026,Main,Reorganization,AAPL,0,0.00\n",
        encoding="utf-8",
    )
    result = run_pt("import", "inspect", str(adapter), expect=4)
    assert result.returncode == 4
    payload = result.json()
    assert payload["error"]["code"] == "PT-E-ACTIVITY-UNMAPPED"
    assert payload["error"]["context"]["activity"] == "Reorganization"
    assert payload["error"]["context"]["row"] == 2


def test_a_missing_document_exits_four(run_pt: CliRunner, adapter: Path) -> None:
    (adapter / "holdings.csv").unlink()
    result = run_pt("import", "inspect", str(adapter), expect=4)
    assert result.json()["error"]["code"] == "PT-E-IMPORT-SOURCE"


def test_a_mismapped_column_names_what_the_file_does_have(
    run_pt: CliRunner, adapter: Path
) -> None:
    (adapter / "holdings.csv").write_text(
        HOLDINGS.replace("Symbol", "Ticker"), encoding="utf-8"
    )
    result = run_pt("import", "inspect", str(adapter), expect=4)
    payload = result.json()
    assert payload["error"]["code"] == "PT-E-IMPORT-COLUMN"
    assert "Ticker" in payload["error"]["context"]["present"]
    assert payload["error"]["context"]["missing"] == ["Symbol"]


def test_the_output_is_identical_across_runs(run_pt: CliRunner) -> None:
    """Invariant 6, through the real CLI."""
    first = run_pt("import", "inspect", str(EXAMPLE)).ok().json()
    second = run_pt("import", "inspect", str(EXAMPLE)).ok().json()
    for payload in (first, second):
        payload.pop("generated_at")
    assert first == second


def test_the_shipped_example_adapter_reads(run_pt: CliRunner) -> None:
    """The worked example is exercised, not merely written down.

    An example nobody runs is an example that stops matching the code.
    """
    data = run_pt("import", "inspect", str(EXAMPLE)).ok().data
    assert data["as_of"] == "2026-06-30"
    assert data["period"] == ["2025-01-22", "2025-06-30"]
    assert data["skipped"] == 3
    assert [r["activity"] for r in data["skipped_rows"]] == [
        "Position Memo",  # a memo line
        "MoneyTransfer",  # a sweep movement, cash either way (ADR 0013)
        "Expense",  # the receiving leg of a paired transfer (ADR 0014)
    ]
    assert all(r["reason"] for r in data["skipped_rows"])
    assert len(data["files"]) == 2
