"""`pt tax` and `pt pnl` disclosing basis provenance, through the real CLI.

ADR 0017 §3 and §2b, at the boundary where the number is actually read.

The scenario is the one the reconstruction produces: two seeded positions, one
whose basis the custodian stated and one where no basis is derivable at all,
both sold. The second sale's stored figure is a real subtraction and is not a
gain — the lot was seeded at cutover market value so cash conservation would
close. If it reaches a total, the report says the owner made money it cannot
show they made, in a document that looks like a tax figure.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner

pytestmark = [pytest.mark.integration]


@pytest.fixture
def seeded(run_pt: CliRunner, tmp_path: Path) -> Path:
    """One anchored lot and one with no derivable basis, both sold in 2024.

    AAA: 100 shares, custodian basis 6,000, 40 sold at 150 -> proceeds 6,000,
    basis 2,400, gain 3,600. Exact arithmetic on a stated figure.

    ZZZ: 50 shares seeded at cutover value 5,000 with NO basis, all sold at 120
    -> proceeds 6,000. The 1,000 difference is the change since the cutover.
    """
    path = tmp_path / "d.port"

    def run(*args: str) -> None:
        run_pt("--port", str(path), *args).ok()

    run_pt("init", str(path), "--name", "D", "--inception", "2022-01-01").ok()
    run("account", "add", "--name", "Taxable", "--type", "taxable", "--opened", "2022-05-01")
    run(
        "account",
        "tax-rates",
        "set",
        "--account",
        "Taxable",
        "--short",
        "0.35",
        "--long",
        "0.15",
        "--effective-from",
        "2022-01-01",
    )
    run("instrument", "add", "AAA", "--name", "Anchored", "--type", "equity")
    run("instrument", "add", "ZZZ", "--name", "Orphan", "--type", "equity")
    run(
        "transfer",
        "in",
        "AAA",
        "--account",
        "Taxable",
        "--qty",
        "100",
        "--value",
        "10000",
        "--basis",
        "6000",
        "--acquired",
        "2019-01-10",
        "--basis-source",
        "custodian_asserted",
        "-d",
        "2022-05-01",
    )
    run(
        "transfer",
        "in",
        "ZZZ",
        "--account",
        "Taxable",
        "--qty",
        "50",
        "--value",
        "5000",
        "--basis-source",
        "unavailable",
        "--assumption",
        "block fully consumed after the cutover; nothing survives",
        "-d",
        "2022-05-01",
    )
    run(
        "sell",
        "AAA",
        "--account",
        "Taxable",
        "--qty",
        "40",
        "--price",
        "150",
        "-d",
        "2024-03-01",
        "--method",
        "fifo",
    )
    run(
        "sell",
        "ZZZ",
        "--account",
        "Taxable",
        "--qty",
        "50",
        "--price",
        "120",
        "-d",
        "2024-04-01",
        "--method",
        "fifo",
    )
    return path


def test_the_unreportable_gain_reaches_no_total(run_pt: CliRunner, seeded: Path) -> None:
    """The whole point. 3,600 is AAA alone; ZZZ's 1,000 is nowhere in it."""
    data = run_pt("--port", str(seeded), "tax", "--year", "2024").ok().data
    assert data["total_gain"] == "3600.00"
    assert data["cost_basis"] == "2400.00"
    assert data["proceeds"] == "6000.00"
    assert data["dispositions"] == 1
    assert data["total_estimated_tax"] == "540.00"


def test_the_year_is_reported_incomplete(run_pt: CliRunner, seeded: Path) -> None:
    data = run_pt("--port", str(seeded), "tax", "--year", "2024").ok().data
    assert data["is_complete"] is False


def test_the_excluded_disposition_is_listed_with_null_basis_and_gain(
    run_pt: CliRunner, seeded: Path
) -> None:
    """Null, not zero. A zero would be read as a figure."""
    data = run_pt("--port", str(seeded), "tax", "--year", "2024").ok().data
    (item,) = data["unreportable"]
    assert item["symbol"] == "ZZZ"
    assert item["proceeds"] == "6000.00"
    assert item["cost_basis"] is None
    assert item["gain"] is None


def test_the_excluded_disposition_is_not_in_the_rendered_rows(
    run_pt: CliRunner, seeded: Path
) -> None:
    """Not even alongside the totals: a number on the page gets read."""
    rows = run_pt("--port", str(seeded), "tax", "--year", "2024").ok().json()["data"]["rows"]
    assert [r["symbol"] for r in rows] == ["AAA"]


def test_each_reported_row_says_where_its_basis_came_from(
    run_pt: CliRunner, seeded: Path
) -> None:
    rows = run_pt("--port", str(seeded), "tax", "--year", "2024").ok().json()["data"]["rows"]
    assert rows[0]["basis_from"] == "custodian_asserted"


def test_the_provenance_breakdown_sums_to_the_reported_basis(
    run_pt: CliRunner, seeded: Path
) -> None:
    """A consumer adding up the rungs must land on the reported total, which is
    only true because the excluded rows are not among them."""
    data = run_pt("--port", str(seeded), "tax", "--year", "2024").ok().data
    total = sum(Decimal(p["cost_basis"]) for p in data["basis_provenance"])
    assert total == Decimal(data["cost_basis"])
    assert data["approximate_basis_share"] == "1"


def test_the_human_output_says_incomplete(run_pt: CliRunner, seeded: Path) -> None:
    """The qualification has to survive into the format a person reads."""
    text = run_pt("--port", str(seeded), "tax", "--year", "2024", fmt="table").ok().stdout
    assert "INCOMPLETE" in text
    assert "1099-B" in text
    assert "did not come from this portfolio's own ledger" in text


def test_pnl_excludes_it_too(run_pt: CliRunner, seeded: Path) -> None:
    """ADR 0017 §3 names `pt pnl` alongside `pt tax`. The same wrong number
    under a different heading is still the same wrong number."""
    result = run_pt("--port", str(seeded), "pnl", "--year", "2024").ok()
    assert result.data["realized"] == "3600.00"
    assert result.data["is_complete"] is False
    assert result.data["excluded_dispositions"] == 1
    assert any("excluded from" in w for w in result.json()["warnings"])


def test_a_fully_derived_portfolio_reports_complete_and_says_nothing_extra(
    run_pt: CliRunner, tmp_path: Path
) -> None:
    """The contrast that makes the disclosure mean something.

    An ordinary portfolio must not grow a caveat it has not earned.
    """
    path = tmp_path / "plain.port"

    def run(*args: str) -> None:
        run_pt("--port", str(path), *args).ok()

    run_pt("init", str(path), "--name", "P", "--inception", "2024-01-01").ok()
    run("account", "add", "--name", "T", "--type", "taxable", "--opened", "2024-01-01")
    run(
        "account",
        "tax-rates",
        "set",
        "--account",
        "T",
        "--short",
        "0.35",
        "--long",
        "0.15",
        "--effective-from",
        "2024-01-01",
    )
    run("instrument", "add", "BBB", "--name", "Bought", "--type", "equity")
    run("cash", "deposit", "--account", "T", "--amount", "10000", "-d", "2024-01-02")
    run("buy", "BBB", "--account", "T", "--qty", "50", "--price", "100", "-d", "2024-01-03")
    run(
        "sell",
        "BBB",
        "--account",
        "T",
        "--qty",
        "50",
        "--price",
        "120",
        "-d",
        "2024-06-03",
        "--method",
        "fifo",
    )

    data = run_pt("--port", str(path), "tax", "--year", "2024").ok().data
    assert data["is_complete"] is True
    assert data["unreportable"] == []
    assert data["approximate_basis"] == "0.00"
    assert data["approximate_basis_share"] == "0"
    assert [p["basis_source"] for p in data["basis_provenance"]] == ["derived"]

    text = run_pt("--port", str(path), "tax", "--year", "2024", fmt="table").ok().stdout
    assert "INCOMPLETE" not in text
    assert "did not come from this portfolio's own ledger" not in text
