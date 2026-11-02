"""`examples/sample.port` is generated, deterministic, and what the generator makes.

`CLAUDE.md` says the fixture is generated rather than hand-crafted, and that two
runs of `make fixtures` produce identical bytes (invariant 6). These tests hold
both claims, and hold the committed file to a fresh build so that it cannot
drift from the generator unnoticed.

Each build runs in its own process, as `make fixtures` does. Two builds in one
interpreter share its string-hash seed, so iterating over a set would give the
same order both times and hide the nondeterminism a second run would expose.

The layout of a SQLite file depends on the library as well as on the rows. The
same writes through SQLite 3.45.1 and 3.53.1 store the same rows in different
pages (ADR 0035). So the committed file is compared row for row everywhere, and
byte for byte where the library that wrote it is the library running the test.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import struct
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from portable_core.lint._common import repo_root
from portable_core.persistence.connection import open_portfolio

pytestmark = pytest.mark.integration

ROOT = repo_root()
GENERATOR = ROOT / "scripts" / "build_sample_portfolio.py"
COMMITTED = ROOT / "examples" / "sample.port"

#: The generator's `STAMP`, as stored. Every row in the fixture claims it.
STAMP = "2026-08-27T00:00:00Z"

#: Tables the fixture writes stamped rows into. The stamp check must cover
#: every one, or it would pass by finding nothing to check.
STAMPED_TABLES = frozenset(
    {
        "schema_migration",
        "return_policy",
        "account",
        "tax_rate_schedule",
        "instrument",
        "corporate_action",
        "price",
        "transaction",
    }
)

REMEDY = "Run `make fixtures` and commit examples/sample.port."


def _build(out: Path) -> Path:
    """Run the generator in a fresh interpreter, as `make fixtures` does."""
    completed = subprocess.run(
        [sys.executable, str(GENERATOR), "--out", str(out), "--force"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return out


@pytest.fixture(scope="module")
def fresh(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build(tmp_path_factory.mktemp("fresh") / "sample.port")


def _first_difference(a: bytes, b: bytes) -> str:
    if len(a) != len(b):
        return f"sizes differ: {len(a)} and {len(b)} bytes"
    offset = next(i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y)
    page_size = struct.unpack(">H", a[16:18])[0] or 65536
    differing_pages = {
        i // page_size for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y
    }
    return (
        f"first difference at byte {offset} (page {offset // page_size + 1}); "
        f"{len(differing_pages)} page(s) differ"
    )


def _written_by(data: bytes) -> tuple[int, int, int]:
    """The SQLite library version recorded in a file's header (bytes 96-99)."""
    number = struct.unpack(">I", data[96:100])[0]
    return number // 1_000_000, number // 1_000 % 1_000, number % 1_000


def _contents(path: Path, scratch: Path) -> list[str]:
    """Every schema object and every row of every table, one line each.

    Read from a copy, so that opening a file never leaves a `-shm` beside the
    one under test. Rows are in rowid order; every table here has one.
    """
    copy = scratch / f"read-{path.parent.name}-{path.name}"
    shutil.copyfile(path, copy)
    lines: list[str] = []
    with closing(open_portfolio(copy, read_only=True)) as con:
        for kind, name, sql in con.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ):
            lines.append(f"{kind} {name}: {sql}")
        tables = [
            str(row[0])
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        for table in tables:
            for row in con.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
                lines.append(f"{table}: {tuple(row)!r}")
    return lines


def _stamps(path: Path, scratch: Path) -> Iterator[tuple[str, str, object]]:
    """``(table, column, value)`` for every stamp column of every row."""
    copy = scratch / f"stamps-{path.name}"
    shutil.copyfile(path, copy)
    with closing(open_portfolio(copy, read_only=True)) as con:
        tables = [
            str(row[0])
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        for table in tables:
            columns = [str(c[1]) for c in con.execute(f'PRAGMA table_info("{table}")')]
            for column in (c for c in columns if c.endswith("_at")):
                for (value,) in con.execute(f'SELECT "{column}" FROM "{table}"'):
                    yield table, column, value
        for key in ("created_at", "updated_at"):
            row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            yield "meta", key, None if row is None else row[0]


def test_every_stamp_in_the_fixture_is_the_generators_clock(
    fresh: Path, tmp_path: Path
) -> None:
    """No row carries the time the generator happened to run (ADR 0035).

    Fails on the wall clock whenever it runs, unlike a comparison of two
    builds, which a fast enough machine can finish inside one second.
    """
    found = list(_stamps(fresh, tmp_path))
    wrong = sorted({(t, c, str(v)) for t, c, v in found if v != STAMP})
    assert not wrong, f"{len(wrong)} stamp(s) are not the generator's {STAMP}: {wrong[:10]}"
    covered = {table for table, _column, _value in found}
    assert covered >= STAMPED_TABLES, f"no stamped rows in {sorted(STAMPED_TABLES - covered)}"


def test_two_builds_are_byte_identical(fresh: Path, tmp_path: Path) -> None:
    """`CLAUDE.md` invariant 6: two runs of `make fixtures`, identical bytes."""
    second = _build(tmp_path / "sample.port")
    a, b = fresh.read_bytes(), second.read_bytes()
    if a != b:  # not `assert`: pytest would print both files
        pytest.fail(f"two builds differ: {_first_difference(a, b)}")


def test_committed_fixture_holds_what_a_fresh_build_holds(fresh: Path, tmp_path: Path) -> None:
    """The committed file has drifted from its generator when a row differs.

    Compared by content, so it holds on every SQLite library CI runs.
    """
    committed, built = _contents(COMMITTED, tmp_path), _contents(fresh, tmp_path)
    in_committed, in_built = set(committed), set(built)
    missing = [line for line in built if line not in in_committed]
    extra = [line for line in committed if line not in in_built]
    assert committed == built, (
        f"examples/sample.port is not what the generator builds. {REMEDY}\n"
        f"only in a fresh build ({len(missing)}): {missing[:5]}\n"
        f"only in the committed file ({len(extra)}): {extra[:5]}"
    )


def test_committed_fixture_is_byte_identical_to_a_fresh_build(fresh: Path) -> None:
    """Byte for byte, where the library that wrote the committed file is running.

    SQLite places the same rows on different pages from one library version to
    another, so on any other library this is skipped and the row-for-row
    comparison above is what holds the file to its generator.
    """
    committed = COMMITTED.read_bytes()
    written_by = _written_by(committed)
    if written_by != sqlite3.sqlite_version_info:
        pytest.skip(
            "examples/sample.port was written by SQLite "
            f"{'.'.join(map(str, written_by))} and this interpreter links "
            f"{sqlite3.sqlite_version}; page layout is the library's, so only the "
            "contents are compared (ADR 0035)"
        )
    built = fresh.read_bytes()
    if committed != built:  # not `assert`: pytest would print both files
        pytest.fail(
            f"examples/sample.port differs from a fresh build: "
            f"{_first_difference(committed, built)}. {REMEDY}"
        )
