"""`pt validate` against the invariant it exists to enforce.

ADR 0016. `CLAUDE.md` invariant 3 requires stored derived state to be exactly
reproducible from the ledger. Until this change `validate` rebuilt twice and
compared the two results -- which destroys the stored state before anything can
be compared against it, so it measured idempotence and reported a clean file on
a book that was demonstrably wrong.

These go through the real CLI because the exit code and the error code are as
much a part of the contract as the detection.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner

pytestmark = [pytest.mark.integration]


def _corrupt_a_lot(port: Path) -> str:
    """Change stored derived state behind `portable`'s back.

    A lot's basis is derived, so a ledger replay must overwrite whatever is put
    here. Editing it directly is how a test stands in for the real causes --
    a bug in a derivation path, an interrupted write, a back-dated append made
    before this fix landed.
    """
    con = sqlite3.connect(port)
    try:
        con.execute(
            "UPDATE lot SET adjusted_cost_basis = '1.00' "
            "WHERE lot_id = (SELECT MIN(lot_id) FROM lot)"
        )
        con.commit()
        row = con.execute(
            "SELECT adjusted_cost_basis FROM lot WHERE lot_id = (SELECT MIN(lot_id) FROM lot)"
        ).fetchone()
        return str(row[0])
    finally:
        con.close()


def _stored_basis(port: Path) -> str:
    con = sqlite3.connect(port)
    try:
        row = con.execute(
            "SELECT adjusted_cost_basis FROM lot WHERE lot_id = (SELECT MIN(lot_id) FROM lot)"
        ).fetchone()
        return str(row[0])
    finally:
        con.close()


@pytest.fixture
def traded(run_pt: CliRunner, portfolio: Path) -> Path:
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


def test_validate_passes_on_a_consistent_portfolio(run_pt: CliRunner, traded: Path) -> None:
    result = run_pt("--port", str(traded), "validate").ok()
    assert result.data["problems"] == 0


def test_validate_detects_stored_state_diverging_from_the_ledger(
    run_pt: CliRunner, traded: Path
) -> None:
    """The failure the whole architecture exists to make detectable."""
    _corrupt_a_lot(traded)

    result = run_pt("--port", str(traded), "validate", expect=4)
    envelope = result.json()

    assert envelope["error"]["code"] == "PT-E-REPLAY-MISMATCH"
    problems = envelope["error"]["context"]["problems"]
    assert any("does not match a replay" in p for p in problems), problems
    # Naming the table is the point: "something differs" is the least useful
    # thing a diagnostic can say about a book somebody files taxes from.
    assert any("lot" in p for p in problems), problems


def test_validate_does_not_repair_what_it_reports(run_pt: CliRunner, traded: Path) -> None:
    """`validate` must leave the file alone, including when it finds a break.

    A command that repaired the divergence it just reported would give a
    different answer the second time it ran, with nothing to show which was
    true. So the corruption must still be there afterwards, and the second run
    must report the same break as the first.
    """
    corrupted = _corrupt_a_lot(traded)

    run_pt("--port", str(traded), "validate", expect=4)
    assert _stored_basis(traded) == corrupted, "validate rebuilt over the state it inspected"

    run_pt("--port", str(traded), "validate", expect=4)
    assert _stored_basis(traded) == corrupted


def test_rebuild_is_what_repairs_it(run_pt: CliRunner, traded: Path) -> None:
    """The remedy the error names actually works, and validate then passes."""
    corrupted = _corrupt_a_lot(traded)

    run_pt("--port", str(traded), "rebuild").ok()
    assert _stored_basis(traded) != corrupted

    assert run_pt("--port", str(traded), "validate").ok().data["problems"] == 0
