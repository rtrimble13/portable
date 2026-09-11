"""`pt ca convert`: a share-class exchange, and the replay that reproduces it.

The claim under test is the one a conversion is most likely to get wrong: the
new lots are the old lots under a new name. Same basis, same acquisition
date, same holding period, nothing realised -- and `pt rebuild` arrives at the
same lots from the ledger alone (CLAUDE.md invariant 3).
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner

pytestmark = [pytest.mark.integration]


@pytest.fixture
def held(run_pt: CliRunner, tmp_path: Path) -> Path:
    path = tmp_path / "demo.port"
    run_pt("init", str(path), "--name", "Demo", "--inception", "2022-01-01").ok()
    run_pt(
        "--port",
        str(path),
        "account",
        "add",
        "--name",
        "IRA",
        "--type",
        "tax_deferred",
        "--opened",
        "2022-05-01",
        "--allows-fractional",
    ).ok()
    for symbol in ("FUNDI", "FUNDR"):
        run_pt(
            "--port",
            str(path),
            "instrument",
            "add",
            symbol,
            "--name",
            symbol,
            "--type",
            "mutual_fund",
        ).ok()
    run_pt(
        "--port",
        str(path),
        "cash",
        "deposit",
        "-a",
        "IRA",
        "--amount",
        "5000.00",
        "--date",
        "2022-05-02",
    ).ok()
    run_pt(
        "--port",
        str(path),
        "buy",
        "FUNDI",
        "-a",
        "IRA",
        "--qty",
        "100",
        "--price",
        "10.00",
        "--date",
        "2022-06-01",
    ).ok()
    run_pt(
        "--port",
        str(path),
        "buy",
        "FUNDI",
        "-a",
        "IRA",
        "--qty",
        "50",
        "--price",
        "12.00",
        "--date",
        "2023-02-01",
    ).ok()
    return path


def _lots(path: Path) -> list[dict[str, object]]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in con.execute(
                "SELECT instrument_id, open_date, remaining_quantity, adjusted_cost_basis, "
                "holding_period_start, status FROM lot ORDER BY open_date, lot_id"
            )
        ]
    finally:
        con.close()


def test_a_conversion_is_the_same_claim_under_a_new_name(run_pt: CliRunner, held: Path) -> None:
    result = run_pt(
        "--port",
        str(held),
        "ca",
        "convert",
        "FUNDI",
        "--to",
        "FUNDR",
        "--units",
        "148.5",
        "--date",
        "2024-03-10",
        "-a",
        "IRA",
    ).ok()
    assert result.data["converted"] == "150"
    assert result.data["received"] == "148.5"

    rows = {r["symbol"]: r for r in run_pt("--port", str(held), "holdings").ok().data["rows"]}
    assert "FUNDI" not in rows
    assert Decimal(rows["FUNDR"]["quantity"]) == Decimal("148.5")
    assert rows["FUNDR"]["cost_basis"] == "1600.00"  # 1,000 + 600, untouched

    opened = [lot for lot in _lots(held) if lot["status"] != "closed"]
    assert [lot["open_date"] for lot in opened] == ["2022-06-01", "2023-02-01"]
    assert [lot["holding_period_start"] for lot in opened] == ["2022-06-01", "2023-02-01"]
    assert [lot["adjusted_cost_basis"] for lot in opened] == ["1000.00", "600.00"]

    # Nothing realised: no disposition, no realized gain.
    con = sqlite3.connect(held)
    try:
        assert con.execute("SELECT count(*) FROM realized_gain").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM lot_disposition").fetchone()[0] == 0
    finally:
        con.close()


def test_a_rebuild_reproduces_the_conversion(run_pt: CliRunner, held: Path) -> None:
    """Invariant 3: the ledger row plus the reference row are enough."""
    run_pt(
        "--port",
        str(held),
        "ca",
        "convert",
        "FUNDI",
        "--to",
        "FUNDR",
        "--units",
        "148.5",
        "--date",
        "2024-03-10",
        "-a",
        "IRA",
    ).ok()
    before = _lots(held)
    run_pt("--port", str(held), "rebuild").ok()
    assert _lots(held) == before
    assert run_pt("--port", str(held), "validate").ok().data["problems"] == 0


def test_a_conversion_refuses_what_it_cannot_do(run_pt: CliRunner, held: Path) -> None:
    for args, complaint in (
        (["FUNDI", "--to", "FUNDI", "--units", "1"], "into itself"),
        (["FUNDI", "--to", "FUNDR", "--units", "0"], "must be positive"),
        (["FUNDR", "--to", "FUNDI", "--units", "1"], "holds no open lots"),
    ):
        result = run_pt(
            "--port",
            str(held),
            "ca",
            "convert",
            *args,
            "--date",
            "2024-03-10",
            "-a",
            "IRA",
            expect=4,
        )
        assert complaint in result.json()["error"]["message"]
