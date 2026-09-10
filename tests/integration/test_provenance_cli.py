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


# ── duplicate references, through the CLI ────────────────────────────────────


def test_a_duplicate_ref_is_refused_with_the_row_it_collides_with(
    run_pt: CliRunner, held: Path
) -> None:
    run_pt(
        "--port",
        str(held),
        "cash",
        "interest",
        "-a",
        "B",
        "--amount",
        "10.00",
        "--date",
        "2024-02-01",
        "--ref",
        "wb:int-1",
    ).ok()
    result = run_pt(
        "--port",
        str(held),
        "cash",
        "interest",
        "-a",
        "B",
        "--amount",
        "10.00",
        "--date",
        "2024-03-01",
        "--ref",
        "wb:int-1",
        expect=4,
    )
    error = result.json()["error"]
    assert error["code"] == "PT-E-DUPLICATE-REF"
    assert "already has transaction" in error["message"]


def test_a_command_that_bypasses_the_service_refuses_the_same_way(
    run_pt: CliRunner, held: Path
) -> None:
    """The corporate-action path builds its row directly, not through a service.

    Before the refusal moved into `TransactionRepository.append` this surfaced
    as `PT-E-GENERIC: unexpected error: IntegrityError` at exit 1 — a bare
    exception, reported as a bug in `portable` when it is a duplicate the user
    can fix.
    """
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
        "wb:ca-1",
    ).ok()
    result = run_pt(
        "--port",
        str(held),
        "ca",
        "split",
        "AAPL",
        "--ratio",
        "2:1",
        "--ex-date",
        "2024-07-03",
        "--ref",
        "wb:ca-1",
        expect=4,
    )
    assert result.json()["error"]["code"] == "PT-E-DUPLICATE-REF"


def test_dry_run_refuses_rather_than_planning_an_uncommittable_trade(
    run_pt: CliRunner, held: Path
) -> None:
    """A dry run that reports success on a trade that cannot commit is a lie."""
    run_pt(
        "--port",
        str(held),
        "cash",
        "interest",
        "-a",
        "B",
        "--amount",
        "10.00",
        "--date",
        "2024-02-01",
        "--ref",
        "wb:int-2",
    ).ok()
    result = run_pt(
        "--port",
        str(held),
        "--dry-run",
        "buy",
        "AAPL",
        "-a",
        "B",
        "--qty",
        "5",
        "--price",
        "190",
        "--date",
        "2024-02-05",
        "--ref",
        "wb:int-2",
        expect=4,
    )
    assert result.json()["error"]["code"] == "PT-E-DUPLICATE-REF"


def test_the_same_ref_in_another_account_is_accepted(run_pt: CliRunner, held: Path) -> None:
    """One corporate action, one reference, several accounts."""
    run_pt(
        "--port",
        str(held),
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
        str(held),
        "cash",
        "deposit",
        "-a",
        "B",
        "--amount",
        "500.00",
        "--date",
        "2024-02-01",
        "--ref",
        "shared",
    ).ok()
    run_pt(
        "--port",
        str(held),
        "cash",
        "deposit",
        "-a",
        "IRA",
        "--amount",
        "500.00",
        "--date",
        "2024-02-01",
        "--ref",
        "shared",
    ).ok()


def test_an_export_round_trip_survives_the_constraint(
    run_pt: CliRunner, held: Path, tmp_path: Path
) -> None:
    """`pt export` → `pt import` still produces identical bytes.

    The new index exists in the fresh file from its first row, so a portfolio
    carrying references has to import cleanly rather than colliding with it.
    """
    run_pt(
        "--port",
        str(held),
        "cash",
        "interest",
        "-a",
        "B",
        "--amount",
        "10.00",
        "--date",
        "2024-02-01",
        "--ref",
        "wb:int-3",
    ).ok()

    first = tmp_path / "a.json"
    run_pt("--port", str(held), "export", "-o", str(first)).ok()
    run_pt("import", "portfolio", str(first), "--into", str(tmp_path / "copy.port")).ok()
    second = tmp_path / "b.json"
    run_pt("--port", str(tmp_path / "copy.port"), "export", "-o", str(second)).ok()
    assert first.read_bytes() == second.read_bytes()


def test_a_reinvested_dividend_opens_a_lot_and_leaves_cash_alone(
    run_pt: CliRunner, held: Path
) -> None:
    """One event, one row. The gross is the income and the cost of the units."""
    before = run_pt("--port", str(held), "account", "show", "B").ok().data["cash"]
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
        "--reinvest-units",
        "0.5",
        "--pay-date",
        "2024-03-15",
        "--ex-date",
        "2024-03-01",
    ).ok()
    assert created.data["type"] == "dividend_reinvest"
    assert created.data["reinvested_units"] == "0.5"
    after = run_pt("--port", str(held), "account", "show", "B").ok().data["cash"]
    assert after == before

    rows = run_pt("--port", str(held), "holdings").ok().data["rows"]
    aapl = next(r for r in rows if r["symbol"] == "AAPL")
    assert aapl["quantity"] == "100.5"
    assert aapl["cost_basis"] == "18600.00"  # 18,500 paid plus the 100 reinvested
