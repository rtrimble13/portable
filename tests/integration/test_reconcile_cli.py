"""`pt reconcile` through the real CLI.

Exit 6 is part of the contract: a reconciliation break is neither a bug in
portable nor a bad argument, and a script driving an import needs to tell it
apart from both.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner

pytestmark = [pytest.mark.integration]


@pytest.fixture
def two_accounts(run_pt: CliRunner, portfolio: Path) -> Path:
    run_pt(
        "--port",
        str(portfolio),
        "account",
        "add",
        "--name",
        "IRA",
        "--type",
        "tax_deferred",
        "--opened",
        "2024-01-02",
    ).ok()
    run_pt(
        "--port",
        str(portfolio),
        "cash",
        "deposit",
        "-a",
        "IRA",
        "--amount",
        "50000",
        "--date",
        "2024-01-02",
    ).ok()
    run_pt(
        "--port",
        str(portfolio),
        "instrument",
        "add",
        "AAPL",
        "--type",
        "equity",
        "--cusip",
        "037833100",
    ).ok()
    for account, qty in (("B", "100"), ("IRA", "40")):
        run_pt(
            "--port",
            str(portfolio),
            "buy",
            "AAPL",
            "-a",
            account,
            "--qty",
            qty,
            "--price",
            "100.00",
            "--date",
            "2024-01-03",
        ).ok()
    return portfolio


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_a_matching_statement_reconciles(
    run_pt: CliRunner, two_accounts: Path, tmp_path: Path
) -> None:
    statement = _write(
        tmp_path / "s.csv",
        "account,symbol,quantity,cash,market_value\n"
        "B,AAPL,100,,\n"
        "B,SWEEP,,true,490000.00\n"
        "IRA,037833100,40,,\n"  # by CUSIP, as a custodian often states it
        "IRA,SWEEP,,true,46000.00\n",
    )
    result = run_pt("--port", str(two_accounts), "reconcile", "--against", str(statement)).ok()
    assert result.data["breaks"] == 0
    assert result.data["accounts"] == ["B", "IRA"]


def test_a_break_exits_six_and_names_the_line(
    run_pt: CliRunner, two_accounts: Path, tmp_path: Path
) -> None:
    statement = _write(
        tmp_path / "s.csv",
        "account,symbol,quantity,cash,market_value\n"
        "B,AAPL,100,,\n"
        "B,SWEEP,,true,489000.00\n"
        "IRA,AAPL,40,,\n"
        "IRA,SWEEP,,true,46000.00\n",
    )
    result = run_pt(
        "--port", str(two_accounts), "reconcile", "--against", str(statement), expect=6
    )
    error = result.json()["error"]
    assert error["code"] == "PT-E-RECONCILE-BREAK"
    assert any("B cash" in b for b in error["context"]["breaks"])


def test_offsetting_errors_across_accounts_still_break(
    run_pt: CliRunner, two_accounts: Path, tmp_path: Path
) -> None:
    """140 shares held, 140 stated, split wrongly between the two accounts."""
    statement = _write(
        tmp_path / "s.csv",
        "account,symbol,quantity,cash,market_value\n"
        "B,AAPL,110,,\n"
        "B,SWEEP,,true,490000.00\n"
        "IRA,AAPL,30,,\n"
        "IRA,SWEEP,,true,46000.00\n",
    )
    result = run_pt(
        "--port", str(two_accounts), "reconcile", "--against", str(statement), expect=6
    )
    assert len(result.json()["error"]["context"]["breaks"]) == 2


def test_one_account_at_a_time_needs_no_account_column(
    run_pt: CliRunner, two_accounts: Path, tmp_path: Path
) -> None:
    statement = _write(tmp_path / "s.csv", "symbol,quantity\nAAPL,100\n")
    run_pt(
        "--port",
        str(two_accounts),
        "reconcile",
        "--against",
        str(statement),
        "--account",
        "B",
        "--cash",
        "490000.00",
    ).ok()


def test_an_unattributable_statement_is_refused(
    run_pt: CliRunner, two_accounts: Path, tmp_path: Path
) -> None:
    statement = _write(tmp_path / "s.csv", "symbol,quantity\nAAPL,140\n")
    result = run_pt(
        "--port", str(two_accounts), "reconcile", "--against", str(statement), expect=4
    )
    assert "which account" in result.json()["error"]["message"]


def test_cash_without_an_account_is_refused(
    run_pt: CliRunner, two_accounts: Path, tmp_path: Path
) -> None:
    statement = _write(tmp_path / "s.csv", "symbol,quantity\nAAPL,100\n")
    run_pt(
        "--port",
        str(two_accounts),
        "reconcile",
        "--against",
        str(statement),
        "--cash",
        "1.00",
        expect=4,
    )


def test_as_of_is_refused_rather_than_ignored(
    run_pt: CliRunner, two_accounts: Path, tmp_path: Path
) -> None:
    """Reconcile compares current holdings; accepting --as-of would answer
    a question nobody asked, against positions as they stand today."""
    statement = _write(tmp_path / "s.csv", "symbol,quantity\nAAPL,100\n")
    result = run_pt(
        "--port",
        str(two_accounts),
        "--as-of",
        "2024-06-01",
        "reconcile",
        "--against",
        str(statement),
        "--account",
        "B",
        expect=4,
    )
    assert "--as-of does not apply" in result.json()["error"]["message"]


def test_a_line_with_no_identifier_is_refused(
    run_pt: CliRunner, two_accounts: Path, tmp_path: Path
) -> None:
    statement = _write(tmp_path / "s.csv", "symbol,quantity\n,100\n")
    result = run_pt(
        "--port",
        str(two_accounts),
        "reconcile",
        "--against",
        str(statement),
        "--account",
        "B",
        expect=4,
    )
    assert "no symbol, cusip or isin" in result.json()["error"]["message"]


def test_a_missing_statement_is_a_clean_refusal(
    run_pt: CliRunner, two_accounts: Path, tmp_path: Path
) -> None:
    run_pt(
        "--port",
        str(two_accounts),
        "reconcile",
        "--against",
        str(tmp_path / "nope.csv"),
        "--account",
        "B",
        expect=4,
    )


def test_nothing_to_reconcile_against_is_refused(run_pt: CliRunner, two_accounts: Path) -> None:
    result = run_pt("--port", str(two_accounts), "reconcile", expect=4)
    assert "nothing to reconcile against" in result.json()["error"]["message"]


def test_realized_needs_an_adapter_with_a_realized_document(
    run_pt: CliRunner, two_accounts: Path, tmp_path: Path
) -> None:
    nowhere = str(tmp_path / "nowhere")
    result = run_pt("--port", str(two_accounts), "reconcile", "--realized", nowhere, expect=4)
    assert "adapter directory not found" in result.json()["error"]["message"]
    example = (
        Path(__file__).resolve().parents[2] / "examples" / "importers" / "example-brokerage"
    )
    result = run_pt(
        "--port", str(two_accounts), "reconcile", "--realized", str(example), expect=4
    )
    assert "declares no realized document" in result.json()["error"]["message"]
