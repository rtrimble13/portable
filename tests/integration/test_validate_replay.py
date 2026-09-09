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


# ── a ledger row replay cannot apply ─────────────────────────────────────────


def _insert_unreplayable_sale(port: Path) -> None:
    """Put a sale of more shares than were ever held into the ledger.

    Inserted directly, because every service path refuses to plan one -- which
    is the point. This stands in for the ways such a row really arrives: a
    derivation bug in an earlier version, a hand-edited file, or a batch import
    whose activity map mapped the wrong side of a transaction. The append-only
    triggers guard UPDATE and DELETE, not INSERT, so the ledger can grow a row
    that derived state cannot account for.
    """
    con = sqlite3.connect(port)
    try:
        account_id = con.execute("SELECT MIN(account_id) FROM account").fetchone()[0]
        instrument_id = con.execute("SELECT MIN(instrument_id) FROM instrument").fetchone()[0]
        con.execute(
            'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, '
            "instrument_id, quantity, price, gross_amount, net_cash_effect, created_at) "
            "VALUES (?, '2024-06-03', 1, 'sell', ?, '9999', '200.00', "
            "'1999800.00', '1999800.00', '2024-06-03T00:00:00Z')",
            (account_id, instrument_id),
        )
        con.commit()
    finally:
        con.close()


def test_validate_fails_on_a_ledger_row_replay_cannot_apply(
    run_pt: CliRunner, traded: Path
) -> None:
    """Derived state must be a function of the *whole* ledger.

    A row nothing can account for is an invariant break, not a note. `rebuild`
    has always collected these and its docstring has always said `pt validate`
    turns them into a non-zero exit; until this change `validate` discarded them
    and reported a clean file.
    """
    _insert_unreplayable_sale(traded)

    result = run_pt("--port", str(traded), "validate", expect=4)
    problems = result.json()["error"]["context"]["problems"]
    assert any("unreplayable" in p for p in problems), problems


def test_rebuild_does_not_make_an_unreplayable_row_go_away(
    run_pt: CliRunner, traded: Path
) -> None:
    """The break is in the ledger, so the usual remedy cannot clear it.

    This separates the two failures `validate` reports: stale derived state is
    fixed by `pt rebuild`, and a ledger row that cannot replay is not.
    """
    _insert_unreplayable_sale(traded)

    run_pt("--port", str(traded), "rebuild").ok()
    result = run_pt("--port", str(traded), "validate", expect=4)
    problems = result.json()["error"]["context"]["problems"]
    assert any("unreplayable" in p for p in problems), problems


def test_rebuild_reports_the_same_row_as_a_warning_and_still_succeeds(
    run_pt: CliRunner, traded: Path
) -> None:
    """The split between the two commands, asserted rather than assumed.

    `pt rebuild` rebuilds and reports; `pt validate` judges. Rebuild surfacing
    every problem in one pass is more useful than stopping at the first, and it
    is not rebuild's job to decide the book is broken.
    """
    _insert_unreplayable_sale(traded)

    envelope = run_pt("--port", str(traded), "rebuild").ok().json()
    assert any("9999" in w or "sell" in w for w in envelope["warnings"]), envelope["warnings"]
