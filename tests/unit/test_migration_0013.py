"""Migration 13: asset classes, classifications and the allocation policy. ADR 0048.

Additive: five new tables, nothing else touched. It seeds the classification
from the free-text `instrument.asset_class` the owner typed (answer 5), and
stamps every row it writes from the runner's clock (ADR 0035). The rows it
creates are effective-dated reference rows: never edited, corrected on their
own date (ADR 0025), and `cash` is built in.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from portable_core.clock import FixedClock
from portable_core.schema import migrations as M

pytestmark = pytest.mark.unit

CLOCK = FixedClock(datetime(2026, 11, 25, tzinfo=UTC))
STAMP = CLOCK.stamp()
NEW_TABLES = {
    "asset_class",
    "classification",
    "classification_part",
    "allocation_policy",
    "allocation_target",
}
_OPENED: list[sqlite3.Connection] = []


@pytest.fixture(autouse=True)
def _close_what_was_opened() -> Iterator[None]:
    yield
    while _OPENED:
        _OPENED.pop().close()


def _at(path: Path, version: int) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    _OPENED.append(con)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    for migration in M.available_migrations():
        if migration.version > version:
            break
        M._apply(con, migration, CLOCK)
    return con


def _instrument(con: sqlite3.Connection, symbol: str, asset_class: str | None) -> None:
    con.execute(
        "INSERT INTO instrument (symbol, instrument_type, name, currency, asset_class, "
        "created_at, updated_at) VALUES (?, 'equity', ?, 'USD', ?, 'x', 'x')",
        (symbol, symbol, asset_class),
    )


def _twelve(path: Path) -> sqlite3.Connection:
    """A file at schema 12 whose owner typed some asset classes, and not others."""
    con = _at(path, 12)
    con.execute("INSERT INTO meta (key, value) VALUES ('inception_date', '2021-01-04')")
    _instrument(con, "VTI", "us_equity")
    _instrument(con, "AAPL", " us_equity ")  # the same class, typed loosely
    _instrument(con, "T-4.25-2030", "us_treasury")
    _instrument(con, "VMFXX", "cash")  # a money-market fund the owner calls cash
    _instrument(con, "ACME", None)
    _instrument(con, "NEWCO", "  ")
    con.commit()
    return con


def _thirteen() -> M.Migration:
    return next(m for m in M.available_migrations() if m.version == 13)


def _dump(con: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    tables = [
        str(r[0])
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]
    return {
        t: [tuple(r) for r in con.execute(f'SELECT * FROM "{t}" ORDER BY 1')] for t in tables
    }


def test_migration_0013_seeds_the_classes_the_owner_typed(tmp_path: Path) -> None:
    con = _twelve(tmp_path / "p.port")
    M._apply(con, _thirteen(), CLOCK)
    assert M.schema_version(con) == 13

    classes = [tuple(r) for r in con.execute("SELECT * FROM asset_class ORDER BY class_id")]
    assert [(c[1], c[3], c[4]) for c in classes] == [
        ("cash", 1, STAMP),
        ("us_equity", 0, STAMP),
        ("us_treasury", 0, STAMP),
    ]
    seeded = con.execute(
        "SELECT i.symbol, c.effective_from, a.name, p.weight, c.created_at "
        "FROM classification c JOIN instrument i USING (instrument_id) "
        "JOIN classification_part p USING (classification_id) "
        "JOIN asset_class a USING (class_id) ORDER BY i.symbol"
    ).fetchall()
    assert [tuple(r) for r in seeded] == [
        ("AAPL", "2021-01-04", "us_equity", "1", STAMP),
        ("T-4.25-2030", "2021-01-04", "us_treasury", "1", STAMP),
        ("VMFXX", "2021-01-04", "cash", "1", STAMP),
        ("VTI", "2021-01-04", "us_equity", "1", STAMP),
    ]
    # Nothing is guessed for an instrument with no class typed.
    unclassified = con.execute(
        "SELECT symbol FROM instrument WHERE instrument_id NOT IN "
        "(SELECT instrument_id FROM classification) ORDER BY symbol"
    ).fetchall()
    assert [r[0] for r in unclassified] == ["ACME", "NEWCO"]


def test_migration_0013_changes_no_existing_row(tmp_path: Path) -> None:
    """Additive, so reversed by dropping its five tables: every other table,
    every row, as it was. That is the direction a migration down would go."""
    con = _twelve(tmp_path / "p.port")
    before = _dump(con)
    M._apply(con, _thirteen(), CLOCK)
    after = _dump(con)
    assert set(after) - set(before) == NEW_TABLES
    for table, rows in before.items():
        if table in {"meta", "schema_migration"}:
            continue
        assert after[table] == rows, table

    # meta changes by its schema version only (this file, built by hand, has
    # no row for it; `pt init` writes one).
    def unversioned(rows: list[tuple[object, ...]]) -> dict[object, object]:
        return {k: v for k, v in rows if k != "schema_version"}

    assert unversioned(after["meta"]) == unversioned(before["meta"])


def test_a_fresh_file_has_cash_and_nothing_else(tmp_path: Path) -> None:
    con = _at(tmp_path / "p.port", 13)
    assert [tuple(r)[1:] for r in con.execute("SELECT * FROM asset_class")] == [
        (
            "cash",
            "Cash: each account's cash balance less its margin loan, sweep vehicles, "
            "and dividends declared and not yet paid. Built in.",
            1,
            STAMP,
        )
    ]
    assert con.execute("SELECT COUNT(*) FROM classification").fetchone()[0] == 0


def test_cash_stays_and_no_class_is_renamed(tmp_path: Path) -> None:
    con = _twelve(tmp_path / "p.port")
    M._apply(con, _thirteen(), CLOCK)
    with pytest.raises(sqlite3.IntegrityError, match="built-in"):
        con.execute("DELETE FROM asset_class WHERE name = 'cash'")
    with pytest.raises(sqlite3.IntegrityError, match="keeps its name"):
        con.execute("UPDATE asset_class SET name = 'equity' WHERE name = 'us_equity'")
    # A class something names cannot go either: the foreign key holds it.
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        con.execute("DELETE FROM asset_class WHERE name = 'us_equity'")
    # Its description is free to change.
    con.execute("UPDATE asset_class SET description = 'US stocks' WHERE name = 'us_equity'")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE classification SET effective_from = '2022-01-01'",
        "DELETE FROM classification",
        "UPDATE classification_part SET weight = '0.5'",
        "DELETE FROM classification_part",
    ],
)
def test_a_classification_is_never_edited(tmp_path: Path, statement: str) -> None:
    con = _twelve(tmp_path / "p.port")
    M._apply(con, _thirteen(), CLOCK)
    with pytest.raises(sqlite3.IntegrityError, match="correct it on its date"):
        con.execute(statement)


def test_one_original_per_date_and_a_correction_names_its_own(tmp_path: Path) -> None:
    """ADR 0025 decision 5, as tax rates and the return policy have it."""
    con = _at(tmp_path / "p.port", 13)
    _instrument(con, "VTI", None)
    _instrument(con, "BND", None)

    def classify(instrument: int, on: str, corrects: int | None) -> None:
        con.execute(
            "INSERT INTO classification (instrument_id, effective_from, created_at, "
            "corrects_classification_id) VALUES (?, ?, 'x', ?)",
            (instrument, on, corrects),
        )

    classify(1, "2024-01-01", None)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        classify(1, "2024-01-01", None)
    classify(1, "2024-01-01", 1)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        classify(1, "2024-01-01", 1)  # a row is corrected once
    with pytest.raises(sqlite3.IntegrityError, match="same instrument and date"):
        classify(2, "2024-01-01", 2)

    def policy(on: str, corrects: int | None) -> None:
        con.execute(
            "INSERT INTO allocation_policy (effective_from, created_at, corrects_policy_id) "
            "VALUES (?, 'x', ?)",
            (on, corrects),
        )

    policy("2024-01-01", None)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        policy("2024-01-01", None)
    with pytest.raises(sqlite3.IntegrityError, match="same date"):
        policy("2024-02-01", 1)
    policy("2024-01-01", 1)
    with pytest.raises(sqlite3.IntegrityError, match="not edited"):
        con.execute("UPDATE allocation_policy SET note = 'x'")


def test_the_rows_it_writes_are_stamped_from_the_runner_s_clock(tmp_path: Path) -> None:
    """ADR 0035: two runs with one pinned clock write the same bytes."""
    first, second = _twelve(tmp_path / "a.port"), _twelve(tmp_path / "b.port")
    for con in (first, second):
        M._apply(con, _thirteen(), CLOCK)
    assert _dump(first) == _dump(second)
    stamps = {r[0] for r in first.execute("SELECT created_at FROM asset_class")}
    applied = first.execute(
        "SELECT applied_at FROM schema_migration WHERE version = 13"
    ).fetchone()[0]
    assert stamps == {applied} == {STAMP}
