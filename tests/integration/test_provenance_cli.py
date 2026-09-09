"""`--ref` and withholding through the real CLI.

The import prerequisites are only useful if they are reachable from the command
line an importer will drive, so these go through `python -m portable_pt` rather
than calling functions: the flag names, the exit codes, and what lands in the
ledger are the contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner

pytestmark = [pytest.mark.integration]


@pytest.fixture
def held(run_pt: CliRunner, portfolio: Path) -> Path:
    run_pt("--port", str(portfolio), "instrument", "add", "AAPL", "--type", "equity")
    run_pt(
        "--port",
        str(portfolio),
        "buy",
        "AAPL",
        "-a",
        "B",
        "--qty",
        "100",
        "--price",
        "185.00",
        "--date",
        "2024-01-03",
    )
    return portfolio


def test_a_dividend_records_gross_and_net_separately(run_pt: CliRunner, held: Path) -> None:
    """Return is earned on the gross; the cash balance moved by the net."""
    created = run_pt(
        "--port",
        str(held),
        "income",
        "dividend",
        "AAPL",
        "-a",
        "B",
        "--amount",
        "100.00",
        "--withheld",
        "15.00",
        "--pay-date",
        "2024-03-15",
        "--ex-date",
        "2024-03-01",
        "--ref",
        "wb:div-1",
    ).ok()
    assert created.data["taxes_withheld"] == "15.00"
    assert created.data["net_cash_effect"] == "85.00"

    shown = run_pt("--port", str(held), "trade", "show", str(created.data["txn_id"])).ok()
    assert shown.data["gross_amount"] == "100.00"
    assert shown.data["net_cash_effect"] == "85.00"
    assert shown.data["taxes_withheld"] == "15.00"
    assert shown.data["external_ref"] == "wb:div-1"
    assert shown.data["source"] == "manual"


def test_withholding_is_not_reported_as_a_fee(run_pt: CliRunner, held: Path) -> None:
    """A fee reduces a return basis under PORT-GIPS-D01; withholding does not.

    If withholding were folded into `fees` the schema would also demand a
    `fee_class` for it, which is the tell that it would be the wrong bucket.
    """
    created = run_pt(
        "--port",
        str(held),
        "income",
        "dividend",
        "AAPL",
        "-a",
        "B",
        "--amount",
        "100.00",
        "--withheld",
        "15.00",
        "--pay-date",
        "2024-03-15",
    ).ok()
    shown = run_pt("--port", str(held), "trade", "show", str(created.data["txn_id"])).ok()
    assert shown.data["fees"] == "0.00"
    assert shown.data["fee_class"] is None


def test_the_reclaimable_split_round_trips(run_pt: CliRunner, held: Path) -> None:
    created = run_pt(
        "--port",
        str(held),
        "income",
        "dividend",
        "AAPL",
        "-a",
        "B",
        "--amount",
        "100.00",
        "--withheld",
        "15.00",
        "--reclaimable",
        "5.00",
        "--pay-date",
        "2024-03-15",
    ).ok()
    shown = run_pt("--port", str(held), "trade", "show", str(created.data["txn_id"])).ok()
    assert shown.data["withholding_reclaimable"] == "5.00"


def test_no_withholding_leaves_the_reclaim_null(run_pt: CliRunner, held: Path) -> None:
    """Explicit null, never a zero standing in for "nobody said"."""
    created = run_pt(
        "--port",
        str(held),
        "income",
        "dividend",
        "AAPL",
        "-a",
        "B",
        "--amount",
        "100.00",
        "--pay-date",
        "2024-03-15",
    ).ok()
    shown = run_pt("--port", str(held), "trade", "show", str(created.data["txn_id"])).ok()
    assert shown.data["taxes_withheld"] == "0.00"
    assert shown.data["withholding_reclaimable"] is None


def test_a_reclaim_above_the_withholding_is_refused(run_pt: CliRunner, held: Path) -> None:
    result = run_pt(
        "--port",
        str(held),
        "income",
        "dividend",
        "AAPL",
        "-a",
        "B",
        "--amount",
        "100.00",
        "--withheld",
        "15.00",
        "--reclaimable",
        "20.00",
        "--pay-date",
        "2024-03-15",
        expect=4,
    )
    assert result.json()["error"]["code"] == "PT-E-WITHHOLDING-INVALID"


def test_withholding_above_the_gross_is_refused(run_pt: CliRunner, held: Path) -> None:
    """The likeliest real mistake: passing the net as `--amount`."""
    result = run_pt(
        "--port",
        str(held),
        "income",
        "dividend",
        "AAPL",
        "-a",
        "B",
        "--amount",
        "85.00",
        "--withheld",
        "100.00",
        "--pay-date",
        "2024-03-15",
        expect=4,
    )
    assert result.json()["error"]["code"] == "PT-E-WITHHOLDING-INVALID"


@pytest.mark.parametrize(
    "command",
    [
        ("cash", "interest", "-a", "B", "--amount", "12.50", "--date", "2024-02-01"),
        (
            "cash",
            "fee",
            "-a",
            "B",
            "--amount",
            "9.99",
            "--fee-class",
            "other_admin",
            "--date",
            "2024-02-01",
        ),
        ("cash", "margin-interest", "-a", "B", "--amount", "4.25", "--date", "2024-02-01"),
    ],
)
def test_ref_reaches_the_ledger_from_commands_that_had_no_flag(
    run_pt: CliRunner, held: Path, command: tuple[str, ...]
) -> None:
    """These were the gap: written, then unmatchable to their source row."""
    created = run_pt("--port", str(held), *command, "--ref", "wb:row-7").ok()
    shown = run_pt("--port", str(held), "trade", "show", str(created.data["txn_id"])).ok()
    assert shown.data["external_ref"] == "wb:row-7"


def test_a_corporate_action_carries_its_ref_too(run_pt: CliRunner, held: Path) -> None:
    """A split is one ledger row per account, so one `--ref` stays unambiguous."""
    run_pt(
        "--port",
        str(held),
        "ca",
        "split",
        "AAPL",
        "--ratio",
        "2:1",
        "--ex-date",
        "2024-06-03",
        "--ref",
        "wb:split-1",
    ).ok()
    listed = run_pt("--port", str(held), "trade", "list").ok()
    splits = [r for r in listed.data["rows"] if r["txn_type"] == "split"]
    assert len(splits) == 1
    shown = run_pt("--port", str(held), "trade", "show", str(splits[0]["txn_id"])).ok()
    assert shown.data["external_ref"] == "wb:split-1"
