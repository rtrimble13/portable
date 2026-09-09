"""`pt transfer in` / `pt transfer out` through the real CLI.

ADR 0015. What matters at this boundary is what ends up in the *file*: that the
lot carries the delivering custodian's basis and not the transfer's value, that
its holding period runs from the original acquisition, that no cash moved, and
that a rebuild reproduces all of it from the ledger alone (invariant 3).

That last one is the reason this is a transaction type rather than a seed
table: a lot with no ledger row behind it cannot survive `pt rebuild`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner

pytestmark = [pytest.mark.integration]


@pytest.fixture
def seeded(run_pt: CliRunner, tmp_path: Path) -> Path:
    """A portfolio with one tax-deferred account and one ETF."""
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
    ).ok()
    run_pt(
        "--port",
        str(path),
        "instrument",
        "add",
        "VTI",
        "--name",
        "Vanguard Total Market",
        "--type",
        "etf",
    ).ok()
    return path


def _lots(path: Path) -> list[dict[str, object]]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM lot ORDER BY lot_id")]
    finally:
        con.close()


def _transfer_in(run_pt: CliRunner, path: Path, *extra: str) -> dict[str, object]:
    return (
        run_pt(
            "--port",
            str(path),
            "transfer",
            "in",
            "VTI",
            "--account",
            "IRA",
            "--qty",
            "100",
            "--value",
            "20000",
            "--basis",
            "12500",
            "--acquired",
            "2018-03-14",
            "--basis-source",
            "custodian_asserted",
            "-d",
            "2022-05-01",
            *extra,
        )
        .ok()
        .data
    )


# ── what lands in the file ───────────────────────────────────────────────────


def test_the_lot_carries_the_basis_not_the_transfer_value(
    run_pt: CliRunner, seeded: Path
) -> None:
    """The failure the whole transaction type exists to prevent.

    20,000 arrived; 12,500 was paid, in 2018. Record the first as basis and
    every future sale reports the gain since the transfer.
    """
    data = _transfer_in(run_pt, seeded)
    assert data["value"] == "20000"
    assert data["cost_basis"] == "12500.00"

    (lot,) = _lots(seeded)
    assert lot["adjusted_cost_basis"] == "12500.00"
    assert lot["per_unit_price"] == "125.00"


def test_the_holding_period_runs_from_the_original_acquisition(
    run_pt: CliRunner, seeded: Path
) -> None:
    """A change of custodian is not a disposition.

    Restarting it would convert long-term gains into short-term ones on the
    next sale — a wrong number in the direction of a larger tax bill.
    """
    _transfer_in(run_pt, seeded)
    (lot,) = _lots(seeded)
    assert lot["open_date"] == "2018-03-14"
    assert lot["holding_period_start"] == "2018-03-14"


def test_no_cash_moved(run_pt: CliRunner, seeded: Path) -> None:
    """No invented outflow, and therefore no invented deposit to fund it."""
    data = _transfer_in(run_pt, seeded)
    assert data["net_cash_effect"] == "0.00"

    cash = run_pt("--port", str(seeded), "holdings", "--as-of", "2022-06-01").ok().data
    assert cash["total_market_value"] is not None
    con = sqlite3.connect(seeded)
    try:
        balances = [r[0] for r in con.execute("SELECT balance FROM cash_balance")]
    finally:
        con.close()
    assert set(balances) <= {"0.00"}


def test_the_basis_source_is_recorded_on_the_lot(run_pt: CliRunner, seeded: Path) -> None:
    """ADR 0017. A report that consumes this lot has to be able to say which
    rung it rested on."""
    _transfer_in(run_pt, seeded)
    (lot,) = _lots(seeded)
    assert lot["basis_source"] == "custodian_asserted"


def test_a_rebuild_reproduces_the_lot_from_the_ledger(run_pt: CliRunner, seeded: Path) -> None:
    """Invariant 3, and the argument against a lot-seed table outside the
    ledger: a lot with no ledger row behind it cannot survive this."""
    _transfer_in(run_pt, seeded)
    before = _lots(seeded)
    run_pt("--port", str(seeded), "rebuild", "--yes").ok()
    after = _lots(seeded)

    assert len(after) == 1
    for field in (
        "open_date",
        "holding_period_start",
        "original_quantity",
        "adjusted_cost_basis",
        "basis_source",
    ):
        assert after[0][field] == before[0][field], field


def test_validate_is_clean_afterwards(run_pt: CliRunner, seeded: Path) -> None:
    _transfer_in(run_pt, seeded)
    assert run_pt("--port", str(seeded), "validate").ok().data["problems"] == 0


# ── the refusals, at the boundary ────────────────────────────────────────────


def test_a_derived_basis_source_is_refused(run_pt: CliRunner, seeded: Path) -> None:
    result = run_pt(
        "--port",
        str(seeded),
        "transfer",
        "in",
        "VTI",
        "--account",
        "IRA",
        "--qty",
        "100",
        "--value",
        "20000",
        "--basis",
        "12500",
        "--basis-source",
        "derived",
        "-d",
        "2022-05-01",
        expect=4,
    )
    assert result.json()["error"]["code"] == "PT-E-BASIS-SOURCE-INVALID"


def test_an_unknown_basis_source_exits_two(run_pt: CliRunner, seeded: Path) -> None:
    """A usage error, not a validation one: the word itself is wrong."""
    result = run_pt(
        "--port",
        str(seeded),
        "transfer",
        "in",
        "VTI",
        "--account",
        "IRA",
        "--qty",
        "100",
        "--value",
        "20000",
        "--basis",
        "12500",
        "--basis-source",
        "guessed",
        "-d",
        "2022-05-01",
        expect=2,
    )
    assert result.json()["error"]["code"] == "PT-E-USAGE"
    assert "custodian_asserted" in result.json()["error"]["remedy"]


def test_an_approximate_basis_must_state_its_assumption(
    run_pt: CliRunner, seeded: Path
) -> None:
    result = run_pt(
        "--port",
        str(seeded),
        "transfer",
        "in",
        "VTI",
        "--account",
        "IRA",
        "--qty",
        "100",
        "--value",
        "20000",
        "--basis",
        "12500",
        "--basis-source",
        "reconstructed",
        "-d",
        "2022-05-01",
        expect=4,
    )
    assert "needs a stated assumption" in result.json()["error"]["message"]


def test_an_approximate_basis_with_its_assumption_is_accepted(
    run_pt: CliRunner, seeded: Path
) -> None:
    run_pt(
        "--port",
        str(seeded),
        "transfer",
        "in",
        "VTI",
        "--account",
        "IRA",
        "--qty",
        "100",
        "--value",
        "20000",
        "--basis",
        "12500",
        "--basis-source",
        "reconstructed",
        "-d",
        "2022-05-01",
        "--assumption",
        "today's basis less every addition since the cutover",
    ).ok()
    (lot,) = _lots(seeded)
    assert lot["basis_source"] == "reconstructed"
    assert lot["basis_assumption"]


def test_a_duplicate_reference_is_refused(run_pt: CliRunner, seeded: Path) -> None:
    """Migration 0002's constraint still binds after the ledger was rebuilt."""
    _transfer_in(run_pt, seeded, "--ref", "seed-1")
    result = run_pt(
        "--port",
        str(seeded),
        "transfer",
        "in",
        "VTI",
        "--account",
        "IRA",
        "--qty",
        "10",
        "--value",
        "2000",
        "--basis",
        "1000",
        "--basis-source",
        "custodian_asserted",
        "-d",
        "2022-05-02",
        "--ref",
        "seed-1",
        expect=4,
    )
    assert result.json()["error"]["code"] == "PT-E-DUPLICATE-REF"


def test_the_ledger_is_still_append_only(seeded: Path, run_pt: CliRunner) -> None:
    """The triggers survived the rebuild that migration 0003 performed."""
    _transfer_in(run_pt, seeded)
    con = sqlite3.connect(seeded)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="PT-E-LEDGER-IMMUTABLE"):
            con.execute("UPDATE \"transaction\" SET note = 'x'")
    finally:
        con.close()


def test_dry_run_writes_nothing(run_pt: CliRunner, seeded: Path) -> None:
    run_pt(
        "--port",
        str(seeded),
        "--dry-run",
        "transfer",
        "in",
        "VTI",
        "--account",
        "IRA",
        "--qty",
        "100",
        "--value",
        "20000",
        "--basis",
        "12500",
        "--basis-source",
        "custodian_asserted",
        "-d",
        "2022-05-01",
    ).ok()
    assert _lots(seeded) == []


# ── the outward side ─────────────────────────────────────────────────────────


def test_a_transfer_out_relieves_the_lot_without_realizing_a_gain(
    run_pt: CliRunner, seeded: Path
) -> None:
    """Nothing was sold, so `pt tax` must not report a disposition."""
    _transfer_in(run_pt, seeded)
    run_pt(
        "--port",
        str(seeded),
        "transfer",
        "out",
        "VTI",
        "--account",
        "IRA",
        "--qty",
        "40",
        "--value",
        "9000",
        "-d",
        "2023-01-10",
        "--method",
        "fifo",
    ).ok()

    con = sqlite3.connect(seeded)
    con.row_factory = sqlite3.Row
    try:
        (lot,) = [dict(r) for r in con.execute("SELECT * FROM lot")]
        cash = [r[0] for r in con.execute("SELECT balance FROM cash_balance")]
    finally:
        con.close()
    assert lot["remaining_quantity"] == "60"
    assert set(cash) <= {"0.00"}


def test_a_transfer_out_asks_the_same_relief_question_a_sale_does(
    run_pt: CliRunner, seeded: Path
) -> None:
    """The account defaults to spec-ID, so a designation is required.

    Which lots leave is not a lesser question because no money changed hands:
    the basis and holding period that go with them are the same numbers.
    """
    _transfer_in(run_pt, seeded)
    result = run_pt(
        "--port",
        str(seeded),
        "transfer",
        "out",
        "VTI",
        "--account",
        "IRA",
        "--qty",
        "40",
        "--value",
        "9000",
        "-d",
        "2023-01-10",
        expect=4,
    )
    assert "specific identification" in result.json()["error"]["message"]
