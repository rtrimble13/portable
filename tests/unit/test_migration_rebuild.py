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
from collections.abc import Iterator
from pathlib import Path

import pytest

from portable_core.errors import PortfolioFileError
from portable_core.schema import migrations as M

pytestmark = pytest.mark.unit


#: Every connection the helpers below open, closed after each test. Tests hand
#: these to code that may raise mid-migration, so closing at the call site
#: would need a try/finally in each of them.
_OPENED: list[sqlite3.Connection] = []


@pytest.fixture(autouse=True)
def _close_what_was_opened() -> Iterator[None]:
    yield
    while _OPENED:
        _OPENED.pop().close()


def _connection(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    _OPENED.append(con)
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
        "CREATE TABLE txn_new (txn_id INTEGER PRIMARY KEY, account_id INTEGER, "
        "trade_date TEXT, seq INTEGER, txn_type TEXT, instrument_id INTEGER, "
        "position_id INTEGER, external_ref TEXT, "
        'reverses_txn_id INTEGER REFERENCES "transaction" (txn_id));\n'
        "INSERT INTO txn_new SELECT txn_id, account_id, trade_date, seq, txn_type, "
        'instrument_id, position_id, external_ref, reverses_txn_id FROM "transaction";\n'
        'DROP TABLE "transaction";\n'
        'ALTER TABLE txn_new RENAME TO "transaction";\n'
        # Recreated, because the runner refuses a rebuild that lost one --
        # which it does, correctly, if these lines are removed.
        'CREATE TRIGGER trg_transaction_no_update BEFORE UPDATE ON "transaction" '
        "BEGIN SELECT RAISE(ABORT, 'no'); END;\n"
        'CREATE TRIGGER trg_transaction_no_delete BEFORE DELETE ON "transaction" '
        "BEGIN SELECT RAISE(ABORT, 'no'); END;\n"
        # And the indexes, for the same reason (ADR 0019 §1, ADR 0022).
        'CREATE INDEX ix_txn_account_date ON "transaction" (account_id, trade_date);\n'
        'CREATE INDEX ix_txn_instrument_date ON "transaction" (instrument_id, trade_date);\n'
        'CREATE INDEX ix_txn_position ON "transaction" (position_id);\n'
        'CREATE INDEX ix_txn_type_date ON "transaction" (txn_type, trade_date);\n'
        'CREATE INDEX ix_txn_order ON "transaction" (trade_date, seq);\n'
        'CREATE UNIQUE INDEX ux_txn_account_external_ref ON "transaction" '
        "(account_id, external_ref) WHERE external_ref IS NOT NULL;\n"
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


# ── 0004, amended (ADR 0022) ─────────────────────────────────────────────────

#: 0004's text as it first shipped, kept so the equivalence below is tested
#: against what files actually ran rather than against a description of it.
_SHIPPED_0004 = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "migrations"
    / "0004_capital_gain_distributions.as_first_shipped.sql"
)


def _as_shipped() -> M.Migration:
    return M.Migration(
        version=4,
        name="capital_gain_distributions",
        path=_SHIPPED_0004,
        sql=_SHIPPED_0004.read_text(encoding="utf-8"),
    )


def _fourth() -> M.Migration:
    return next(m for m in M.available_migrations() if m.version == 4)


def _seed_transfers(con: sqlite3.Connection, *, transfer_in: bool) -> None:
    """In-kind rows on top of `_seed`: the rows 0003's four columns exist for."""
    if transfer_in:
        con.execute(
            'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, '
            "instrument_id, quantity, price, gross_amount, net_cash_effect, "
            "original_basis, original_acquired_date, basis_source, basis_assumption, "
            "created_at) VALUES (1,'2024-01-15',1,'transfer_in',1,'25','180.00',"
            "'4500.00','0.00','3125.50','2019-06-03','reconstructed',"
            "'rolled back from the 2024-08-27 snapshot','x')"
        )
    con.execute(
        'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, '
        "instrument_id, quantity, price, gross_amount, net_cash_effect, created_at) "
        "VALUES (1,'2024-03-01',1,'transfer_out',1,'5','190.00','950.00','0.00','x')"
    )
    con.commit()


def _snapshot(
    con: sqlite3.Connection,
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    """The file's schema and its ledger, row for row."""
    schema = sorted(
        tuple(r)
        for r in con.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    )
    ledger = [tuple(r) for r in con.execute('SELECT * FROM "transaction" ORDER BY txn_id')]
    return schema, ledger


def test_migration_0004_carries_every_column_0003_added(tmp_path: Path) -> None:
    """The defect ADR 0022 amends: a v3 file with a transfer_in could not upgrade.

    0004 as shipped copied 0002's column list into the rebuilt ledger and not
    the four 0003 added, so the transfer_in's CHECK refused the copy -- every
    portfolio seeded by `pt import broker` or `pt transfer in` was stuck at v3.
    """
    shipped = _at(tmp_path / "shipped.port", 3)
    _seed(shipped)
    _seed_transfers(shipped, transfer_in=True)
    with pytest.raises(PortfolioFileError, match="basis_source IS NOT NULL"):
        M._apply(shipped, _as_shipped())
    assert M.schema_version(shipped) == 3
    shipped.close()

    con = _at(tmp_path / "amended.port", 3)
    _seed(con)
    _seed_transfers(con, transfer_in=True)
    before = _snapshot(con)[1]

    M._apply(con, _fourth())

    assert M.schema_version(con) == 4
    assert _snapshot(con)[1] == before, "every column of every row, unchanged"
    row = con.execute(
        "SELECT original_basis, original_acquired_date, basis_source, basis_assumption "
        "FROM \"transaction\" WHERE txn_type = 'transfer_in'"
    ).fetchone()
    assert tuple(row) == (
        "3125.50",
        "2019-06-03",
        "reconstructed",
        "rolled back from the 2024-08-27 snapshot",
    )
    con.close()


def test_0004_as_shipped_and_as_amended_leave_the_same_file(tmp_path: Path) -> None:
    """ADR 0022's condition for amending at all, tested rather than argued.

    On every file the shipped text could migrate -- one with no transfer_in,
    since the CHECK refused the rest -- the two texts must produce the same
    schema and the same rows. That is what makes accepting the shipped
    checksum honest: the file cannot tell which text ran.
    """
    files = []
    for name, migration in (("shipped", _as_shipped()), ("amended", _fourth())):
        con = _at(tmp_path / f"{name}.port", 3)
        _seed(con)
        _seed_transfers(con, transfer_in=False)
        M._apply(con, migration)
        files.append(con)

    assert _snapshot(files[0]) == _snapshot(files[1])
    for con in files:
        assert M.verify_checksums(con) == []
        con.close()


def test_the_superseded_0004_checksum_is_the_text_that_shipped() -> None:
    """The accepted checksum is the fixture's, so the fixture is what shipped."""
    assert set(M.SUPERSEDED_CHECKSUMS[4]) == {_as_shipped().checksum}
    assert _as_shipped().checksum != _fourth().checksum


def test_any_other_checksum_is_still_an_edited_migration(tmp_path: Path) -> None:
    """Recording one superseded text must not loosen the check for any other."""
    con = _at(tmp_path / "p.port", 4)
    con.execute("UPDATE schema_migration SET checksum = 'not-a-shipped-text' WHERE version = 4")
    con.commit()

    problems = M.verify_checksums(con)

    assert len(problems) == 1
    assert "0004_capital_gain_distributions has been edited" in problems[0]
    con.close()


def test_a_rebuild_that_loses_an_index_is_refused(tmp_path: Path) -> None:
    """The index counterpart of the lost-trigger test (ADR 0019's open item).

    A file that lost `ux_txn_account_external_ref` looks entirely fine and
    accepts the duplicate references migration 0002 exists to refuse.
    """
    con = _at(tmp_path / "p.port", 3)
    _seed(con)
    with pytest.raises(PortfolioFileError) as excinfo:
        M._apply(
            con,
            _fake(f"{M.REBUILD_MARKER}\nDROP INDEX ux_txn_account_external_ref;\n"),
        )
    assert "did not restore index" in str(excinfo.value)
    assert excinfo.value.context["indexes"] == ["ux_txn_account_external_ref"]
    assert "ux_txn_account_external_ref" in M._indexes(con), "rolled back"
    con.close()


# ── 0005: position_regroup (ADR 0023) ────────────────────────────────────────


def _fifth() -> M.Migration:
    return next(m for m in M.available_migrations() if m.version == 5)


def test_migration_0005_keeps_every_column_of_every_row(tmp_path: Path) -> None:
    """The lesson of 0004, applied: copy all 33 columns, named one by one.

    Twelve of them are empty in this fixture; the 0006 test below is the one
    that requires every column to carry a value.
    """
    con = _at(tmp_path / "p.port", 4)
    _seed(con)
    _seed_transfers(con, transfer_in=True)
    columns = [r[1] for r in con.execute('PRAGMA table_info("transaction")')]
    listed = ", ".join(columns)
    before = [
        tuple(r) for r in con.execute(f'SELECT {listed} FROM "transaction" ORDER BY txn_id')
    ]

    M._apply(con, _fifth())

    assert M.schema_version(con) == 5
    after = [
        tuple(r) for r in con.execute(f'SELECT {listed} FROM "transaction" ORDER BY txn_id')
    ]
    assert after == before
    assert (
        con.execute(
            'SELECT COUNT(*) FROM "transaction" WHERE strategy_type IS NOT NULL'
        ).fetchone()[0]
        == 0
    )
    con.close()


def test_migration_0005_admits_a_regroup_and_binds_it(tmp_path: Path) -> None:
    """A regroup moves a leg and nothing else; a strategy belongs to a regroup only."""
    con = _at(tmp_path / "p.port", 5)
    _seed(con)
    regroup = (
        'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, instrument_id, '
        "quantity, net_cash_effect, strategy_type, created_at) "
        "VALUES (1, '2024-03-01', ?, 'position_regroup', ?, ?, ?, ?, 'x')"
    )
    con.execute(regroup, (1, 1, None, "0.00", "covered_call"))
    for seq, (instrument, quantity, cash, strategy) in enumerate(
        (
            (1, "10", "0.00", None),  # moves a quantity
            (1, None, "-5.00", None),  # moves cash
            (None, None, "0.00", None),  # names no leg
        ),
        start=2,
    ):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(regroup, (seq, instrument, quantity, cash, strategy))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        con.execute(
            'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, '
            "net_cash_effect, strategy_type, created_at) "
            "VALUES (1, '2024-03-02', 1, 'deposit', '1.00', 'custom', 'x')"
        )
    con.close()


def _sixth() -> M.Migration:
    return next(m for m in M.available_migrations() if m.version == 6)


def test_migration_0006_keeps_every_column_of_every_row(tmp_path: Path) -> None:
    """All 34 columns, tested with every one of them carrying a value somewhere."""
    con = _at(tmp_path / "p.port", 5)
    _seed(con)
    _seed_transfers(con, transfer_in=True)
    con.execute(
        'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, instrument_id, '
        "net_cash_effect, strategy_type, created_at) "
        "VALUES (1, '2024-03-01', 9, 'position_regroup', 1, '0.00', 'covered_call', 'x')"
    )
    # The columns the shared seed leaves empty, each carried by a row of a type
    # that may hold it.
    con.execute(
        "INSERT INTO account (name, account_type, opened_date, currency, created_at, "
        "updated_at) VALUES ('S','taxable','2024-01-01','USD','x','x')"
    )
    con.execute(
        'INSERT INTO "transaction" (account_id, trade_date, settlement_date, seq, txn_type, '
        "net_cash_effect, counter_account_id, fees, fee_class, note, created_at) "
        "VALUES (1, '2024-03-02', '2024-03-04', 1, 'transfer', '-10.00', 2, '1.00', "
        "'other_admin', 'to S', 'x')"
    )
    con.execute(
        'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, instrument_id, '
        "net_cash_effect, taxes_withheld, withholding_reclaimable, ex_date, pay_date, "
        "is_qualified, created_at) VALUES (1, '2024-03-03', 1, 'dividend', 1, '8.50', "
        "'1.50', '1.50', '2024-03-01', '2024-03-03', 1, 'x')"
    )
    con.execute(
        'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, instrument_id, '
        "quantity, price, net_cash_effect, position_id, related_txn_id, lot_selection, "
        "relief_method, created_at) VALUES (1, '2024-03-04', 1, 'sell', 1, '2', '110.00', "
        "'220.00', 7, 2, '1:2', 'spec', 'x')"
    )
    columns = [r[1] for r in con.execute('PRAGMA table_info("transaction")')]
    listed = ", ".join(columns)
    for column in columns:
        assert con.execute(
            f'SELECT COUNT(*) FROM "transaction" WHERE "{column}" IS NOT NULL'
        ).fetchone()[0], f"no row carries {column}, so the copy of it is untested"
    before = [
        tuple(r) for r in con.execute(f'SELECT {listed} FROM "transaction" ORDER BY txn_id')
    ]
    con.commit()

    M._apply(con, _sixth())

    assert M.schema_version(con) == 6
    after = [
        tuple(r) for r in con.execute(f'SELECT {listed} FROM "transaction" ORDER BY txn_id')
    ]
    assert after == before
    assert (
        con.execute('SELECT COUNT(*) FROM "transaction" WHERE label IS NOT NULL').fetchone()[0]
        == 0
    )


def test_migration_0006_admits_a_label_and_binds_it(tmp_path: Path) -> None:
    """A label names a position and moves nothing; only a label row carries one."""
    con = _at(tmp_path / "p.port", 6)
    _seed(con)
    opened = con.execute('SELECT MIN(txn_id) FROM "transaction"').fetchone()[0]
    label = (
        'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, instrument_id, '
        "quantity, net_cash_effect, related_txn_id, label, created_at) "
        "VALUES (1, '2024-03-01', ?, 'position_label', ?, ?, ?, ?, ?, 'x')"
    )
    con.execute(label, (1, None, None, "0.00", opened, "Core holding"))
    con.execute(label, (2, None, None, "0.00", opened, None))  # clears it
    for seq, (instrument, quantity, cash, related) in enumerate(
        (
            (1, None, "0.00", opened),  # names an instrument
            (None, "10", "0.00", opened),  # moves a quantity
            (None, None, "-5.00", opened),  # moves cash
            (None, None, "0.00", None),  # names no position
        ),
        start=3,
    ):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(label, (seq, instrument, quantity, cash, related, "x"))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        con.execute(
            'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, '
            "net_cash_effect, label, created_at) "
            "VALUES (1, '2024-03-02', 1, 'deposit', '1.00', 'oops', 'x')"
        )
    con.close()


def test_an_older_file_is_refused_until_it_is_migrated(tmp_path: Path) -> None:
    """PT-E-SCHEMA-TOO-OLD was defined and never raised: a v4 file read by v5
    code failed on its first new column with "No item with that key"."""
    con = _at(tmp_path / "p.port", 4)

    with pytest.raises(PortfolioFileError) as refused:
        M.check_openable(con)
    assert refused.value.code == "PT-E-SCHEMA-TOO-OLD"
    assert "pt migrate" in (refused.value.remedy or "")

    M.check_openable(con, allow_older=True)  # what `pt migrate` asks for
    con.close()
