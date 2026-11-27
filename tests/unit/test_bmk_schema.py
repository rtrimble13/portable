"""The `.bmk` schema, version 1 (ADR 0050 §2).

A `.bmk` is SQLite like a `.port`, migrated by the same runner from its own
directory. Its definition rows are never edited, a correction names a row of
the same key, and its CHECKs refuse what no method can mean. A `.bmk` and a
`.port` each refuse to be read as the other.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from portable_core.clock import FixedClock
from portable_core.errors import PortfolioFileError
from portable_core.persistence.connection import open_index, open_portfolio
from portable_core.schema import bmk
from portable_core.schema import migrations as M

pytestmark = pytest.mark.unit

CLOCK = FixedClock(datetime(2026, 11, 27, tzinfo=UTC))
_OPENED: list[sqlite3.Connection] = []


@pytest.fixture(autouse=True)
def _close() -> Iterator[None]:
    yield
    while _OPENED:
        _OPENED.pop().close()


def _fresh(path: Path) -> sqlite3.Connection:
    con = open_index(path, must_exist=False)
    _OPENED.append(con)
    bmk.initialise(con, clock=CLOCK, applied_by="portable test")
    return con


def test_a_fresh_file_is_at_version_one_stamped_from_the_clock(tmp_path: Path) -> None:
    con = _fresh(tmp_path / "i.bmk")
    assert M.schema_version(con) == bmk.CURRENT_BMK_SCHEMA_VERSION == 1
    (row,) = con.execute("SELECT version, name, applied_at, applied_by FROM schema_migration")
    assert tuple(row) == (1, "initial", CLOCK.stamp(), "portable test")
    bmk.check_openable(con, tmp_path / "i.bmk")
    assert bmk.verify_checksums(con) == []


def test_the_bmk_migrations_are_not_the_port_s() -> None:
    assert [m.version for m in bmk.available_migrations()] == [1]
    port = {m.path.parent for m in M.available_migrations()}
    assert bmk.BMK_SCHEMA_DIR not in port


def test_each_tool_refuses_the_other_s_file(tmp_path: Path) -> None:
    """A `.bmk` read as a `.port` at version 1 would be offered `pt migrate`,
    which would apply the portfolio's migrations to an index."""
    index = _fresh(tmp_path / "i.bmk")
    with pytest.raises(PortfolioFileError, match=r"not a \.port portfolio"):
        M.check_openable(index, tmp_path / "i.bmk", allow_older=True)
    with pytest.raises(PortfolioFileError, match=r"not a \.port portfolio"):
        M.migrate(index, tmp_path / "i.bmk", backup=False)

    port = open_portfolio(tmp_path / "p.port", must_exist=False)
    _OPENED.append(port)
    M.initialise(port, clock=CLOCK)
    with pytest.raises(PortfolioFileError, match=r"not a \.bmk file"):
        bmk.check_openable(port, tmp_path / "p.port")


def _method(con: sqlite3.Connection, on: str, weighting: str, rule: str, band: str | None,
            corrects: int | None = None) -> None:  # fmt: skip
    con.execute(
        "INSERT INTO methodology (effective_from, weighting, rebalance_rule, drift_band, "
        "created_at, corrects_methodology_id) VALUES (?, ?, ?, ?, 'x', ?)",
        (on, weighting, rule, band, corrects),
    )


@pytest.mark.parametrize(
    ("weighting", "rule", "band", "why"),
    [
        ("cap", "drift_band", "0.05", "a cap target moves every day"),
        ("price", "drift_band", "0.05", "a price-weighted index has no target"),
        ("price", "monthly", None, "price-weighted holds one unit until reconstitution"),
        ("custom", "drift_band", None, "a drift band names its width"),
        ("equal", "monthly", "0.05", "a width without the rule"),
    ],
)
def test_a_methodology_no_method_can_mean_is_refused(
    tmp_path: Path, weighting: str, rule: str, band: str | None, why: str
) -> None:
    con = _fresh(tmp_path / "i.bmk")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        _method(con, "2024-01-02", weighting, rule, band)
    assert why


def test_definition_rows_are_corrected_on_their_date_and_never_edited(tmp_path: Path) -> None:
    con = _fresh(tmp_path / "i.bmk")
    _method(con, "2024-01-02", "equal", "quarterly", None)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _method(con, "2024-01-02", "custom", "monthly", None)
    with pytest.raises(sqlite3.IntegrityError, match="same date"):
        _method(con, "2024-02-01", "custom", "monthly", None, corrects=1)
    _method(con, "2024-01-02", "custom", "monthly", None, corrects=1)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _method(con, "2024-01-02", "custom", "annual", None, corrects=1)

    con.execute(
        "INSERT INTO reconstitution (effective_date, created_at) VALUES ('2024-01-02', 'x')"
    )
    con.execute(
        "INSERT INTO security (fafnir_sid, symbol, security_type, source, created_at) "
        "VALUES (7, 'AAA', 'equity', 'test', 'x')"
    )
    con.execute(
        "INSERT INTO reconstitution_member (reconstitution_id, security_id) VALUES (1, 1)"
    )
    for statement in (
        "UPDATE methodology SET note = 'x'",
        "DELETE FROM methodology",
        "UPDATE reconstitution SET note = 'x'",
        "DELETE FROM reconstitution_member",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="not edited"):
            con.execute(statement)


def test_inputs_carry_their_basis_whole_or_not_at_all(tmp_path: Path) -> None:
    con = _fresh(tmp_path / "i.bmk")
    con.execute(
        "INSERT INTO security (fafnir_sid, symbol, security_type, source, created_at) "
        "VALUES (7, 'AAA', 'equity', 'test', 'x')"
    )
    # A cap from `duk mc` states what it multiplied; a file's is whole.
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        con.execute(
            "INSERT INTO market_cap (security_id, cap_date, market_cap, close_raw, source, "
            "as_of) VALUES (1, '2024-01-02', '5', '1', 'fafnir:duk mc', 'x')"
        )
    con.execute(
        "INSERT INTO market_cap (security_id, cap_date, market_cap, source, as_of) "
        "VALUES (1, '2024-01-02', '5', 'file:caps.csv', 'x')"
    )
    # A float factor says where it came from.
    con.execute(
        "INSERT INTO reconstitution (effective_date, created_at) VALUES ('2024-01-02', 'x')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        con.execute(
            "INSERT INTO reconstitution_member (reconstitution_id, security_id, float_factor) "
            "VALUES (1, 1, '0.9')"
        )
    # A delisting states what it paid, even if that is nothing.
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        con.execute(
            "INSERT INTO security_event (security_id, event_date, event_type, source, "
            "created_at) VALUES (1, '2024-05-01', 'delisting', 'x', 'x')"
        )
    # An irregular payer states the longest gap it may show.
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        con.execute(
            "INSERT INTO distribution_schedule (security_id, effective_from, schedule, "
            "source, created_at) VALUES (1, '2024-01-02', 'irregular', 'x', 'x')"
        )
    # One build at a time: a build replaces every derived row.
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        con.execute(
            "INSERT INTO build (build_id, inputs_hash, first_date, last_date, built_at, "
            "built_by) VALUES (2, 'h', 'a', 'b', 'c', 'd')"
        )


def test_no_column_is_binary_floating_point() -> None:
    """ADR 0005: decimals are canonical TEXT, here as in the `.port`."""
    con = sqlite3.connect(":memory:")
    for migration in bmk.available_migrations():
        for statement in M.split_statements(migration.sql):
            con.execute(statement)
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        for column in con.execute(f'PRAGMA table_info("{table}")'):
            assert str(column[2]).upper() in {"TEXT", "INTEGER"}, (table, column[1], column[2])
    con.close()
