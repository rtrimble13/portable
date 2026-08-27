"""The schema, its migrations, and the constraints that carry invariants.

Several of these assert things the database enforces rather than things Python
enforces. That distinction matters: a CHECK constraint binds every writer,
including `pt import`, a future broker adapter, and somebody poking at the file
with the `sqlite3` shell. A Python-side check binds only the paths we
remembered.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from portable_core.errors import PortfolioFileError
from portable_core.persistence.connection import open_portfolio
from portable_core.schema import migrations as M

pytestmark = pytest.mark.unit

NOW = "2026-08-27T00:00:00Z"


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    """A fresh, migrated portfolio file."""
    con = open_portfolio(tmp_path / "t.port", must_exist=False)
    M.initialise(con)
    return con


def _account(con: sqlite3.Connection, account_id: int = 1, name: str = "Brokerage") -> None:
    con.execute(
        "INSERT INTO account (account_id, name, account_type, opened_date,"
        " created_at, updated_at)"
        " VALUES (?, ?, 'taxable', '2024-01-01', ?, ?)",
        (account_id, name, NOW, NOW),
    )


# ── migrations ───────────────────────────────────────────────────────────────


def test_migrations_are_numbered_uniquely_and_in_order() -> None:
    migrations = M.available_migrations()
    versions = [m.version for m in migrations]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
    assert versions[-1] == M.CURRENT_SCHEMA_VERSION, (
        "CURRENT_SCHEMA_VERSION must equal the highest shipped migration"
    )


def test_initialise_then_migrate_is_a_no_op(db: sqlite3.Connection, tmp_path: Path) -> None:
    """Migrations are idempotent: re-running finds nothing to do."""
    before, after, pending, backup = M.migrate(db, tmp_path / "t.port")
    assert (before, after, pending, backup) == (
        M.CURRENT_SCHEMA_VERSION,
        M.CURRENT_SCHEMA_VERSION,
        [],
        None,
    )


def test_migration_is_atomic_despite_executescript(db: sqlite3.Connection) -> None:
    """Statements are split and run in one transaction.

    `executescript` issues an implicit COMMIT before running, which would make
    a migration non-atomic exactly where atomicity matters. The splitter must
    handle a CREATE TRIGGER body, whose semicolons do not end the statement.
    """
    statements = M.split_statements(M.available_migrations()[0].sql)
    triggers = [s for s in statements if s.upper().startswith("CREATE TRIGGER")]
    assert len(triggers) == 2, "both ledger triggers must survive splitting whole"
    for trigger in triggers:
        assert trigger.rstrip().upper().endswith("END;")
        assert "RAISE(ABORT" in trigger


def test_editing_an_applied_migration_is_detected(db: sqlite3.Connection) -> None:
    """A checksum mismatch means the DDL on disk no longer describes this file."""
    assert M.verify_checksums(db) == []
    db.execute("UPDATE schema_migration SET checksum = 'tampered' WHERE version = 1")
    problems = M.verify_checksums(db)
    assert len(problems) == 1
    assert "edited since it was applied" in problems[0]


def test_a_newer_schema_is_refused_not_guessed_at(db: sqlite3.Connection) -> None:
    """Forward compatibility is one-directional on purpose."""
    db.execute(
        "INSERT INTO schema_migration (version, name, checksum, applied_at, applied_by)"
        " VALUES (?, 'future', 'x', ?, 'portable 9.9.9')",
        (M.CURRENT_SCHEMA_VERSION + 1, NOW),
    )
    with pytest.raises(PortfolioFileError) as exc:
        M.check_openable(db)
    assert exc.value.code == "PT-E-SCHEMA-TOO-NEW"
    assert exc.value.exit_code == 3
    assert "upgrade portable" in (exc.value.remedy or "").lower()


def test_missing_file_is_an_error_not_an_empty_database(tmp_path: Path) -> None:
    """SQLite creates a file on connect; a typo must not become a portfolio."""
    with pytest.raises(PortfolioFileError) as exc:
        open_portfolio(tmp_path / "typo.port")
    assert exc.value.code == "PT-E-PORTFOLIO-NOT-FOUND"
    assert not (tmp_path / "typo.port").exists()


# ── the invariants the database itself carries ───────────────────────────────


def test_ledger_rejects_update(db: sqlite3.Connection) -> None:
    """CLAUDE.md invariant 2, enforced by trigger rather than by convention."""
    _account(db)
    db.execute(
        'INSERT INTO "transaction" (txn_id, account_id, trade_date, seq, txn_type,'
        " net_cash_effect, created_at) VALUES (1, 1, '2024-01-02', 1, 'deposit', '1000.00', ?)",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="PT-E-LEDGER-IMMUTABLE"):
        db.execute("UPDATE \"transaction\" SET note = 'edited' WHERE txn_id = 1")


def test_ledger_rejects_delete(db: sqlite3.Connection) -> None:
    _account(db)
    db.execute(
        'INSERT INTO "transaction" (txn_id, account_id, trade_date, seq, txn_type,'
        " net_cash_effect, created_at) VALUES (1, 1, '2024-01-02', 1, 'deposit', '1000.00', ?)",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="PT-E-LEDGER-IMMUTABLE"):
        db.execute('DELETE FROM "transaction" WHERE txn_id = 1')


@pytest.mark.gips
def test_fee_classification_exhaustive(db: sqlite3.Connection) -> None:
    """PORT-GIPS-D01 -- a NULL fee_class on a fee-bearing row fails.

    The three return bases are derived from this column, so an unclassified
    fee is refused rather than defaulted. The constraint lives in the schema so
    it binds `pt import` and any future broker adapter too, not merely the
    paths we remembered to guard.
    """
    _account(db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        db.execute(
            'INSERT INTO "transaction" (txn_id, account_id, trade_date, seq, txn_type,'
            " fees, net_cash_effect, created_at)"
            " VALUES (1, 1, '2024-01-03', 1, 'buy', '1.00', '-100.00', ?)",
            (NOW,),
        )

    for i, fee_class in enumerate(
        [
            "transaction_cost",
            "embedded_fund_fee",
            "external_mgmt_fee",
            "internal_mgmt_cost",
            "other_admin",
        ],
        start=1,
    ):
        db.execute(
            'INSERT INTO "transaction" (txn_id, account_id, trade_date, seq, txn_type,'
            " fees, fee_class, net_cash_effect, created_at)"
            " VALUES (?, 1, '2024-01-03', ?, 'fee', '1.00', ?, '-1.00', ?)",
            (10 + i, i, fee_class, NOW),
        )

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            'INSERT INTO "transaction" (txn_id, account_id, trade_date, seq, txn_type,'
            " fees, fee_class, net_cash_effect, created_at)"
            " VALUES (99, 1, '2024-01-03', 99, 'fee', '1.00', 'invented_class', '-1.00', ?)",
            (NOW,),
        )


def test_transfer_must_carry_a_counter_account(db: sqlite3.Connection) -> None:
    """ADR 0007 -- a transfer is ONE row with two sides, not two rows.

    That is what makes portfolio-level netting structural. Two independent
    rows that happen to cancel would still trigger a sub-period break under
    PORT-GIPS-B03 and would still appear in the daily flow series.
    """
    _account(db, 1, "A")
    _account(db, 2, "B")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        db.execute(
            'INSERT INTO "transaction" (txn_id, account_id, trade_date, seq, txn_type,'
            " net_cash_effect, created_at)"
            " VALUES (1, 1, '2024-02-01', 1, 'transfer', '-500.00', ?)",
            (NOW,),
        )
    db.execute(
        'INSERT INTO "transaction" (txn_id, account_id, trade_date, seq, txn_type,'
        " counter_account_id, net_cash_effect, created_at)"
        " VALUES (1, 1, '2024-02-01', 1, 'transfer', 2, '-500.00', ?)",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        db.execute(
            'INSERT INTO "transaction" (txn_id, account_id, trade_date, seq, txn_type,'
            " counter_account_id, net_cash_effect, created_at)"
            " VALUES (2, 1, '2024-02-02', 1, 'deposit', 2, '500.00', ?)",
            (NOW,),
        )


def test_a_reversal_must_name_what_it_reverses(db: sqlite3.Connection) -> None:
    _account(db)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        db.execute(
            'INSERT INTO "transaction" (txn_id, account_id, trade_date, seq, txn_type,'
            " net_cash_effect, created_at)"
            " VALUES (1, 1, '2024-02-01', 1, 'reversal', '500.00', ?)",
            (NOW,),
        )


@pytest.mark.gips
def test_price_only_benchmark_cannot_be_created_by_omission(db: sqlite3.Connection) -> None:
    """PORT-GIPS-G01 -- return_type is NOT NULL with NO DEFAULT, deliberately.

    A defaulted return_type is how a price-only series gets used by accident,
    and a price index understates its benchmark by roughly the dividend yield
    every year. The refusal at report time is in the service layer; this is the
    schema making the mistake unrepresentable in the first place.
    """
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed"):
        db.execute(
            "INSERT INTO benchmark (benchmark_id, name, description, created_at)"
            " VALUES (1, 'SPX', 'S&P 500 price index', ?)",
            (NOW,),
        )
    db.execute(
        "INSERT INTO benchmark (benchmark_id, name, description, return_type, created_at)"
        " VALUES (1, 'SPXTR', 'S&P 500 total return', 'total_return', ?)",
        (NOW,),
    )


def test_valuation_level_is_confined_to_the_gips_hierarchy(db: sqlite3.Connection) -> None:
    """PORT-GIPS-A02 -- five levels, and nothing outside them."""
    db.execute(
        "INSERT INTO instrument (instrument_id, symbol, instrument_type,"
        " created_at, updated_at) VALUES (1, 'AAPL', 'equity', ?, ?)",
        (NOW, NOW),
    )
    for level in (1, 5):
        db.execute(
            "INSERT INTO price (instrument_id, price_date, price, source, as_of,"
            " valuation_level, valuation_basis, created_at)"
            " VALUES (1, ?, '100.00', 'manual', ?, ?, 'manual', ?)",
            (f"2024-01-0{level}", NOW, level, NOW),
        )
    for bad in (0, 6):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            db.execute(
                "INSERT INTO price (instrument_id, price_date, price, source, as_of,"
                " valuation_level, valuation_basis, created_at)"
                " VALUES (1, '2024-02-01', '100.00', 'manual', ?, ?, 'manual', ?)",
                (NOW, bad, NOW),
            )


def test_significant_and_large_flow_thresholds_are_separate_fields(
    db: sqlite3.Connection,
) -> None:
    """PORT-GIPS-E09 -- they are different thresholds for different purposes.

    A large flow triggers revaluation and a sub-period return. A significant
    flow triggers temporary removal from a composite. A flow can be one without
    being the other, so modelling them with one field would be wrong.
    """
    db.execute(
        "INSERT INTO return_policy (policy_id, effective_from, large_flow_basis,"
        " large_flow_value, created_at) VALUES (1, '2024-01-01', 'percent', '0.10', ?)",
        (NOW,),
    )
    row = db.execute("SELECT * FROM return_policy WHERE policy_id = 1").fetchone()
    assert row["significant_flow_value"] is None, "the two are independent"

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        db.execute(
            "INSERT INTO return_policy (policy_id, effective_from, large_flow_basis,"
            " large_flow_value, significant_flow_basis, created_at)"
            " VALUES (2, '2025-01-01', 'percent', '0.10', 'percent', ?)",
            (NOW,),
        )


def test_foreign_keys_are_enforced_on_every_connection(db: sqlite3.Connection) -> None:
    """SQLite defaults foreign_keys OFF; without the pragma, REFERENCES is decoration."""
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        db.execute(
            'INSERT INTO "transaction" (txn_id, account_id, trade_date, seq, txn_type,'
            " net_cash_effect, created_at)"
            " VALUES (1, 999, '2024-01-02', 1, 'deposit', '1000.00', ?)",
            (NOW,),
        )


def test_no_real_or_numeric_column_exists_anywhere(db: sqlite3.Connection) -> None:
    """ADR 0005, asserted against the live database rather than the DDL text.

    The lint rule reads the SQL; this reads what SQLite actually built. They
    can disagree -- an affinity is inferred, not declared -- and the one that
    matters is what is in the file.
    """
    offenders: list[str] = []
    for table in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        for column in db.execute(f'PRAGMA table_info("{table["name"]}")').fetchall():
            declared = (column["type"] or "").upper()
            if any(k in declared for k in ("REAL", "FLOA", "DOUB", "NUMERIC")):
                name = f"{table['name']}.{column['name']}"
                offenders.append(f"{name} {declared}")
    assert offenders == [], offenders


def test_every_table_is_documented_in_the_generated_reference() -> None:
    """`make docs` output must cover every table, or the reference is a lie."""
    import io

    from portable_core.schema import docgen

    buffer = io.StringIO()
    docgen.main(buffer)
    rendered = buffer.getvalue()

    con = sqlite3.connect(":memory:")
    for statement in M.split_statements(M.available_migrations()[0].sql):
        con.execute(statement)
    tables = [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    missing = [t for t in tables if f"## `{t}`" not in rendered]
    assert missing == [], missing
