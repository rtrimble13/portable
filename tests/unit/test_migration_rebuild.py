"""A migration that rebuilds a table. ADR 0019.

SQLite cannot alter a `CHECK` constraint, so adding a `txn_type` value means
rebuilding `"transaction"` -- the ledger, with the append-only triggers on it
and four tables' foreign keys pointing at it. The documented procedure needs
`PRAGMA foreign_keys = OFF` *outside* any transaction, which is the thing the
runner could not do.

What these test is not the SQL (0003 has its own file) but the **mechanism**:
that the pragma is handled correctly on both paths, that the enforcement it
switches off is reinstated before commit rather than skipped, and that a
rebuild which lost a trigger is refused. That last one matters most: a file
that lost `trg_transaction_no_update` looks entirely fine and is no longer
append-only, and no test of the migration's data would notice.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from portable_core.errors import PortfolioFileError
from portable_core.schema import migrations as M

pytestmark = pytest.mark.unit


def _connection(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA legacy_alter_table = OFF")
    return con


def _at(path: Path, version: int) -> sqlite3.Connection:
    """A file with migrations up to *version* applied."""
    con = _connection(path)
    for migration in M.available_migrations():
        if migration.version > version:
            break
        M._apply(con, migration)
    return con


def _seed(con: sqlite3.Connection) -> None:
    con.execute(
        "INSERT INTO account (name, account_type, opened_date, currency, created_at, "
        "updated_at) VALUES ('B','taxable','2024-01-01','USD','x','x')"
    )
    con.execute(
        "INSERT INTO instrument (symbol, instrument_type, name, currency, created_at, "
        "updated_at) VALUES ('AAPL','equity','Apple','USD','x','x')"
    )
    con.execute(
        'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, '
        "net_cash_effect, external_ref, created_at) "
        "VALUES (1,'2024-02-01',1,'deposit','1000.00','ref-1','x')"
    )
    con.execute(
        'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, '
        "instrument_id, quantity, price, gross_amount, net_cash_effect, created_at) "
        "VALUES (1,'2024-02-02',1,'buy',1,'10','100.00','1000.00','-1000.00','x')"
    )
    # A reversal: a ledger row referencing another ledger row. This is the
    # reference that matters, and the reason the whole mechanism exists. The
    # externally referencing tables -- lot, position, realized_gain -- are all
    # derived, so a migration can clear them and they stop being an obstacle.
    # A self-reference is ledger data and cannot be cleared.
    #
    # Without one, `DROP TABLE "transaction"` succeeds even with foreign keys
    # on. That is the shape of the bug this guards: it would pass on the
    # fixture and on any new file, and fail on a real portfolio.
    con.execute(
        'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, '
        "net_cash_effect, reverses_txn_id, created_at) "
        "VALUES (1,'2024-02-05',1,'reversal','-1000.00',1,'x')"
    )
    con.commit()


def _fake(sql: str, *, version: int = 99, name: str = "fake") -> M.Migration:
    return M.Migration(version=version, name=name, path=Path(f"{version}_{name}.sql"), sql=sql)


# ── the marker ───────────────────────────────────────────────────────────────


def test_a_migration_declares_that_it_rebuilds() -> None:
    """In the file, not in a registry: the fact travels with the thing."""
    by_version = {m.version: m for m in M.available_migrations()}
    assert by_version[1].rebuilds is False
    assert by_version[2].rebuilds is False
    assert by_version[3].rebuilds is True


def test_the_marker_is_inside_the_checksummed_text() -> None:
    """So a migration cannot be quietly promoted after it has been applied."""
    third = next(m for m in M.available_migrations() if m.version == 3)
    assert M.REBUILD_MARKER in third.sql
    without = _fake(third.sql.replace(M.REBUILD_MARKER, "-- nothing"))
    assert without.checksum != third.checksum
    assert without.rebuilds is False


def test_the_marker_must_be_near_the_top() -> None:
    """A marker buried in a comment two hundred lines down is not a
    declaration anybody would see while reviewing."""
    filler = "-- filler\n" * 10
    assert _fake("SELECT 1;\n" + filler + M.REBUILD_MARKER + "\n").rebuilds is False


# ── the pragma ───────────────────────────────────────────────────────────────


def test_only_a_rebuild_migration_can_drop_a_referenced_table(
    tmp_path: Path,
) -> None:
    """The whole reason the runner had to change, as observable behaviour.

    `PRAGMA foreign_keys = OFF` is a documented no-op inside a transaction, and
    `_apply` opens one before executing a migration's statements. So a plain
    `.sql` file cannot turn foreign keys off, and dropping a table other rows
    point at fails -- which is exactly what rebuilding `"transaction"` requires.
    Declaring the rebuild is what makes the same SQL work.

    Note what the two halves share: identical statements. The only difference
    is the marker.
    """
    swap = (
        "CREATE TABLE txn_new (txn_id INTEGER PRIMARY KEY, txn_type TEXT, "
        'reverses_txn_id INTEGER REFERENCES "transaction" (txn_id));\n'
        "INSERT INTO txn_new SELECT txn_id, txn_type, reverses_txn_id "
        'FROM "transaction";\n'
        'DROP TABLE "transaction";\n'
        'ALTER TABLE txn_new RENAME TO "transaction";\n'
        # Recreated, because the runner refuses a rebuild that lost one --
        # which it does, correctly, if these two lines are removed.
        'CREATE TRIGGER trg_transaction_no_update BEFORE UPDATE ON "transaction" '
        "BEGIN SELECT RAISE(ABORT, 'no'); END;\n"
        'CREATE TRIGGER trg_transaction_no_delete BEFORE DELETE ON "transaction" '
        "BEGIN SELECT RAISE(ABORT, 'no'); END;\n"
    )

    plain = _at(tmp_path / "plain.port", 2)
    _seed(plain)
    with pytest.raises(PortfolioFileError, match="FOREIGN KEY constraint failed"):
        M._apply(plain, _fake(swap))
    assert plain.execute('SELECT count(*) FROM "transaction"').fetchone()[0] == 3

    declared = _at(tmp_path / "declared.port", 2)
    _seed(declared)
    M._apply(declared, _fake(M.REBUILD_MARKER + "\n" + swap))
    # Same SQL, and now it works -- with every row carried across.
    assert declared.execute('SELECT count(*) FROM "transaction"').fetchone()[0] == 3
    # The pragma is back on afterwards, on the success path too.
    assert declared.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_foreign_keys_are_restored_after_a_failed_rebuild(tmp_path: Path) -> None:
    """Connection state, not file state.

    Left OFF, every later write on this connection silently skips referential
    integrity — in a CLI whose very next action is often `pt rebuild`.
    """
    con = _at(tmp_path / "p.port", 2)
    _seed(con)
    with pytest.raises(PortfolioFileError):
        M._apply(con, _fake(f"{M.REBUILD_MARKER}\nSELECT * FROM no_such_table;\n"))
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_a_non_rebuild_migration_never_touches_the_pragma(tmp_path: Path) -> None:
    con = _at(tmp_path / "p.port", 1)
    M._apply(con, next(m for m in M.available_migrations() if m.version == 2))
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# ── the checks that replace the enforcement ──────────────────────────────────


def test_a_rebuild_that_orphans_a_row_is_refused(tmp_path: Path) -> None:
    """The check is not skipped; it moves from per-statement to once at the end.

    Without `foreign_key_check` this migration would commit happily and leave a
    lot pointing at an account that does not exist.
    """
    con = _at(tmp_path / "p.port", 2)
    _seed(con)
    con.execute(
        'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, '
        "net_cash_effect, created_at) VALUES (1,'2024-02-03',1,'deposit','1.00','x')"
    )
    con.commit()

    with pytest.raises(PortfolioFileError) as excinfo:
        M._apply(
            con,
            _fake(
                f"{M.REBUILD_MARKER}\n"
                "INSERT INTO account (account_id, name, account_type, opened_date, "
                "currency, created_at, updated_at) "
                "VALUES (9,'X','taxable','2024-01-01','USD','x','x');\n"
                'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, '
                "net_cash_effect, created_at) "
                "VALUES (99,'2024-02-04',1,'deposit','1.00','x');\n"
            ),
        )
    assert "foreign key violation" in str(excinfo.value)
    assert excinfo.value.context["violations"]
    # All or nothing: the bad row is not in the file. Three from the seed,
    # plus the one this test appended before the migration ran.
    assert con.execute('SELECT count(*) FROM "transaction"').fetchone()[0] == 4


def test_a_rebuild_that_loses_a_trigger_is_refused(tmp_path: Path) -> None:
    """The one that would otherwise pass every data test.

    A file that lost `trg_transaction_no_update` looks entirely fine and is no
    longer append-only — `CLAUDE.md` invariant 2, gone silently.
    """
    con = _at(tmp_path / "p.port", 2)
    _seed(con)
    with pytest.raises(PortfolioFileError) as excinfo:
        M._apply(
            con,
            _fake(f"{M.REBUILD_MARKER}\nDROP TRIGGER trg_transaction_no_update;\n"),
        )
    assert "did not restore trigger" in str(excinfo.value)
    assert excinfo.value.context["triggers"] == ["trg_transaction_no_update"]
    # Rolled back, so the trigger is still there.
    with pytest.raises(sqlite3.IntegrityError, match="PT-E-LEDGER-IMMUTABLE"):
        con.execute("UPDATE \"transaction\" SET note = 'x' WHERE txn_id = 1")


def test_migration_0003_needs_the_rebuild_it_declares(tmp_path: Path) -> None:
    """The specific claim ADR 0019 rests on, checked against the real file.

    With the marker removed, 0003's own SQL fails on a ledger containing a
    reversal -- and succeeds on one without. That asymmetry is the reason the
    ADR exists: a migration that works on the fixture and on every new file,
    and breaks the owner's real portfolio, is the worst available outcome.
    """
    third = next(m for m in M.available_migrations() if m.version == 3)
    without = _fake(
        third.sql.replace(M.REBUILD_MARKER, "-- removed"), version=3, name=third.name
    )
    assert without.rebuilds is False

    con = _at(tmp_path / "real.port", 2)
    _seed(con)  # includes a reversal
    with pytest.raises(PortfolioFileError, match="FOREIGN KEY constraint failed"):
        M._apply(con, without)

    # Declared, the very same SQL goes through.
    M._apply(con, third)
    assert M.schema_version(con) == 3
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert con.execute('SELECT count(*) FROM "transaction"').fetchone()[0] == 3


def test_a_failed_rebuild_leaves_the_file_unchanged(tmp_path: Path) -> None:
    """All-or-nothing is why the rebuild runs inside a transaction at all."""
    con = _at(tmp_path / "p.port", 2)
    _seed(con)
    before = con.execute('SELECT count(*) FROM "transaction"').fetchone()[0]
    with pytest.raises(PortfolioFileError):
        M._apply(
            con,
            _fake(
                f"{M.REBUILD_MARKER}\n"
                'DELETE FROM "transaction" WHERE 1 = 0;\n'
                "SELECT * FROM no_such_table;\n"
            ),
        )
    assert con.execute('SELECT count(*) FROM "transaction"').fetchone()[0] == before
    assert M.schema_version(con) == 2


def test_migration_0004_preserves_every_ledger_row_and_admits_the_new_types(
    tmp_path: Path,
) -> None:
    """Fund capital-gain distributions: a rebuild that changes only the CHECK.
    Every row survives, the triggers come back, and the two new members are
    accepted where they were refused before."""
    con = _at(tmp_path / "real.port", 3)
    _seed(con)  # an account, an instrument, three rows including a reversal
    before = con.execute('SELECT count(*) FROM "transaction"').fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, '
            "net_cash_effect, created_at) VALUES (1, '2026-01-01', 99, 'capital_gain_lt', "
            "'0.00', 'x')"
        )
    con.rollback()
    fourth = next(m for m in M.available_migrations() if m.version == 4)
    assert fourth.rebuilds
    M._apply(con, fourth)
    assert M.schema_version(con) == 4
    assert con.execute('SELECT count(*) FROM "transaction"').fetchone()[0] == before
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert {"trg_transaction_no_update", "trg_transaction_no_delete"} <= M._triggers(con)
    for seq, kind in enumerate(("capital_gain_lt", "capital_gain_st"), start=100):
        con.execute(
            'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, '
            "net_cash_effect, created_at) VALUES (1, '2026-01-01', ?, ?, '0.00', 'x')",
            (seq, kind),
        )
    con.commit()
