"""The whole pipeline, end to end, through the real CLI.

`pt import broker` → review → `pt import batch` → `pt reconcile`.

The last of those is the point. `docs/broker-import.md` §9: an import is
acceptable when the resulting portfolio reconciles to the custodian's own
position statement. Everything upstream — the capability model, the roll-back,
the basis ladder, the batch's refusals — is credible only because that check
passes, and it is the only thing standing behind the reconstruction.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from portable_core.lint._common import repo_root
from tests.integration.conftest import CliRunner

pytestmark = [pytest.mark.integration]

EXAMPLE = repo_root() / "examples" / "importers" / "example-brokerage"


@pytest.fixture
def portfolio(run_pt: CliRunner, tmp_path: Path) -> Path:
    """A portfolio that knows the accounts, instruments and cutover prices."""
    path = tmp_path / "p.port"

    def run(*args: str) -> None:
        run_pt("--port", str(path), *args).ok()

    run_pt("init", str(path), "--name", "E2E", "--inception", "2025-01-01").ok()
    for account in ("Brokerage", "Roth IRA"):
        run("account", "add", "--name", account, "--type", "taxable", "--opened", "2025-01-01")
        run(
            "account",
            "tax-rates",
            "set",
            "--account",
            account,
            "--short",
            "0.35",
            "--long",
            "0.15",
            "--effective-from",
            "2025-01-01",
        )
    for symbol in ("VTI", "MSFT", "VXUS", "FDRXX", "SPAXX"):
        run("instrument", "add", symbol, "--name", f"{symbol} Inc", "--type", "etf")
    run("price", "set", "VTI", "--price", "201.00", "--date", "2025-01-21")
    run("price", "set", "MSFT", "--price", "410.00", "--date", "2025-01-21")
    return path


def _stated(tmp_path: Path) -> Path:
    """The custodian's own snapshot, as `pt reconcile` reads it."""
    source = EXAMPLE / "holdings.csv"
    target = tmp_path / "stated.csv"
    with source.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["account", "symbol", "quantity", "cash"])
        for row in rows:
            writer.writerow(
                [
                    row["Account"],
                    row["Symbol"],
                    row["Quantity"],
                    "true" if row["Symbol"] in {"FDRXX", "SPAXX"} else "false",
                ]
            )
    return target


# ── the acceptance test ──────────────────────────────────────────────────────


def test_the_imported_portfolio_reconciles_to_the_custodian(
    run_pt: CliRunner, portfolio: Path, tmp_path: Path
) -> None:
    """`docs/broker-import.md` §9, and the only thing standing behind any of it.

    Positions *and* cash. Quantities that reconcile while cash does not is the
    signature of a sign error or a dropped row, so a check that stopped at
    share counts would be the easy half of the question.
    """
    batch = tmp_path / "batch.json"
    run_pt("--port", str(portfolio), "import", "broker", str(EXAMPLE), "-o", str(batch)).ok()
    run_pt("--port", str(portfolio), "import", "batch", str(batch)).ok()

    assert run_pt("--port", str(portfolio), "validate").ok().data["problems"] == 0

    result = run_pt(
        "--port", str(portfolio), "reconcile", "--against", str(_stated(tmp_path))
    ).ok()
    assert result.data["breaks"] == 0
    assert result.data["lines"] == 5


def test_the_basis_the_reconstruction_solved_is_the_basis_the_custodian_states(
    run_pt: CliRunner, portfolio: Path, tmp_path: Path
) -> None:
    """The reconstruction's arithmetic, checked against its own anchor.

    MSFT's block was solved backwards under FIFO from the 11,240 the custodian
    states for 40 shares. After the seed and the sale, the ledger has to arrive
    back at exactly 11,240 — anything else means the solve and the relief
    disagree, which is the failure the FIFO assumption in the batch prevents.
    """
    batch = tmp_path / "batch.json"
    run_pt("--port", str(portfolio), "import", "broker", str(EXAMPLE), "-o", str(batch)).ok()
    run_pt("--port", str(portfolio), "import", "batch", str(batch)).ok()

    rows = run_pt("--port", str(portfolio), "holdings").ok().json()["data"]["rows"]
    by_symbol = {r["symbol"]: r for r in rows}
    assert by_symbol["MSFT"]["cost_basis"] == "11240.00"
    assert by_symbol["VTI"]["cost_basis"] == "24150.00"


# ── the batch itself ─────────────────────────────────────────────────────────


def test_the_batch_is_written_and_nothing_is_committed(
    run_pt: CliRunner, portfolio: Path, tmp_path: Path
) -> None:
    """Extraction writes a file. The review of that file is the point."""
    batch = tmp_path / "batch.json"
    data = (
        run_pt("--port", str(portfolio), "import", "broker", str(EXAMPLE), "-o", str(batch))
        .ok()
        .data
    )

    assert batch.is_file()
    assert data["seeded"] == 4  # two positions, two cash balances
    assert data["appended"] == 8
    assert data["skipped"] == 1
    assert data["cutover"] == "2025-01-21"
    assert run_pt("--port", str(portfolio), "holdings").ok().data["rows"] == []


def test_a_seed_row_keeps_the_value_and_the_basis_apart_in_the_file(
    run_pt: CliRunner, portfolio: Path, tmp_path: Path
) -> None:
    """The reviewer has to be able to see both, and see that they differ."""
    batch = tmp_path / "batch.json"
    run_pt("--port", str(portfolio), "import", "broker", str(EXAMPLE), "-o", str(batch)).ok()
    rows = json.loads(batch.read_text(encoding="utf-8"))["rows"]
    seed = next(r for r in rows if r.get("symbol") == "MSFT" and "cutover" in r["rule"])

    assert seed["txn_type"] == "transfer_in"
    assert seed["quantity"] == "50"
    assert seed["price"] == "410.00"
    assert seed["amount"] == "20500.00"  # market value at the cutover
    assert seed["original_basis"] == "14050.00"  # what was paid
    assert seed["basis_source"] == "estimated"
    assert "FIFO assumed" in seed["basis_assumption"]


def test_the_batch_round_trips_through_the_published_format(
    run_pt: CliRunner, portfolio: Path, tmp_path: Path
) -> None:
    """`dump_batch` and `load_batch` have to agree, or the extract stage and
    the commit stage are describing different files."""
    batch = tmp_path / "batch.json"
    run_pt("--port", str(portfolio), "import", "broker", str(EXAMPLE), "-o", str(batch)).ok()
    # A dry run loads and commits the whole file, then rolls it back.
    run_pt("--port", str(portfolio), "--dry-run", "import", "batch", str(batch)).ok()
    assert run_pt("--port", str(portfolio), "holdings").ok().data["rows"] == []


def test_committing_twice_is_refused_rather_than_doubling_the_position(
    run_pt: CliRunner, portfolio: Path, tmp_path: Path
) -> None:
    """The whole reason a seed's reference is synthesized deterministically."""
    batch = tmp_path / "batch.json"
    run_pt("--port", str(portfolio), "import", "broker", str(EXAMPLE), "-o", str(batch)).ok()
    run_pt("--port", str(portfolio), "import", "batch", str(batch)).ok()

    result = run_pt("--port", str(portfolio), "import", "batch", str(batch), expect=4)
    assert result.json()["error"]["code"] == "PT-E-DUPLICATE-REF"


# ── refusals ─────────────────────────────────────────────────────────────────


def test_a_missing_cutover_price_refuses_and_names_what_it_needs(
    run_pt: CliRunner, tmp_path: Path
) -> None:
    """There is no honest substitute for the market value at the cutover, and
    the basis — the number to hand — is the one that must not be used."""
    path = tmp_path / "bare.port"

    def run(*args: str) -> None:
        run_pt("--port", str(path), *args).ok()

    run_pt("init", str(path), "--name", "Bare", "--inception", "2025-01-01").ok()
    for account in ("Brokerage", "Roth IRA"):
        run("account", "add", "--name", account, "--type", "taxable", "--opened", "2025-01-01")
    for symbol in ("VTI", "MSFT", "VXUS", "FDRXX", "SPAXX"):
        run("instrument", "add", symbol, "--name", symbol, "--type", "etf")

    result = run_pt(
        "--port",
        str(path),
        "import",
        "broker",
        str(EXAMPLE),
        "-o",
        str(tmp_path / "b.json"),
        expect=5,
    )
    error = result.json()["error"]
    assert error["code"] == "PT-E-PRICE-MISSING"
    assert error["context"]["instruments"] == ["MSFT", "VTI"]
    assert "cannot stand in for it" in error["remedy"]


def test_the_output_is_identical_across_runs(
    run_pt: CliRunner, portfolio: Path, tmp_path: Path
) -> None:
    """Invariant 6. A batch is very often read through `git diff`, so a stable
    key order is what makes the second run useful."""
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    for target in (first, second):
        run_pt(
            "--port", str(portfolio), "import", "broker", str(EXAMPLE), "-o", str(target)
        ).ok()
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")
