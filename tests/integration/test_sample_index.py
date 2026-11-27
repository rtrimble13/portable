"""`examples/sample.bmk` is generated, deterministic, and right by hand (ADR 0050).

As `examples/sample.port` is (test_sample_fixture.py): two runs of the
generator write the same bytes, the committed file is what a fresh run writes,
and one level is worked from first principles. Bytes 96-99 of the header
record the SQLite library that last wrote the file and are set aside (ADR 0035).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from decimal import Decimal as D
from decimal import localcontext
from pathlib import Path

import pytest

from tests.integration.conftest import ROOT, CliResult
from tests.unit.test_json_schemas import _validator

pytestmark = pytest.mark.integration

GENERATOR = ROOT / "scripts" / "build_sample_index.py"
COMMITTED = ROOT / "examples" / "sample.bmk"
REMEDY = "Run `make fixtures` and commit examples/sample.bmk."


def _build(out: Path) -> Path:
    completed = subprocess.run(
        [sys.executable, str(GENERATOR), "--out", str(out), "--force"],
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return out


def _body(path: Path) -> bytes:
    data = path.read_bytes()
    return data[:96] + bytes(4) + data[100:]


def _bmk(tmp_path: Path, index: Path, *args: str) -> CliResult:
    env = dict(os.environ) | {
        "PYTHONPATH": str(ROOT / "src"),
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
    }
    env.pop("PORTABLE_BMK", None)
    return CliResult(
        subprocess.run(
            [
                sys.executable,
                "-m",
                "portable_bmk",
                "--bmk",
                str(index),
                "--format",
                "json",
                *args,
            ],
            capture_output=True,
            encoding="utf-8",
            check=False,
            cwd=tmp_path,
            env=env,
        )
    )


def test_two_builds_are_byte_identical(tmp_path: Path) -> None:
    first = _build(tmp_path / "a.bmk")
    second = _build(tmp_path / "b.bmk")
    assert first.read_bytes() == second.read_bytes()


def test_the_committed_sample_is_what_a_fresh_build_writes(tmp_path: Path) -> None:
    fresh = _build(tmp_path / "fresh.bmk")
    assert _body(COMMITTED) == _body(fresh), REMEDY


def test_the_sample_reads_validates_and_is_current(tmp_path: Path) -> None:
    copy = tmp_path / "sample.bmk"
    shutil.copyfile(COMMITTED, copy)
    levels = _bmk(tmp_path, copy, "levels").ok().json()
    errors = sorted(_validator("bmk-levels-1.0.json").iter_errors(levels), key=str)
    assert not errors, [e.message for e in errors[:5]]
    rows = levels["data"]["rows"]
    assert (rows[0]["date"], rows[-1]["date"], len(rows)) == ("2025-01-02", "2025-06-30", 122)
    assert [r["date"] for r in rows if r["reconstitution"]] == ["2025-01-02", "2025-04-01"]
    assert [r["date"] for r in rows if r["rebalance"]] == [
        "2025-01-02",
        "2025-03-31",
        "2025-04-01",
    ]
    assert _bmk(tmp_path, copy, "validate").ok().data["problems"] == 0


def test_the_first_day_by_hand() -> None:
    """2025-01-03, the first session after the base. Cap-weighted at 01-02's
    caps, CHAR's at 80%, so each weight is cap x float / total, and

        level = 1,000 x sum(weight x close on 01-03 / close on 01-02)

    read from the closes and caps the file holds, not from its levels.
    """
    with closing(sqlite3.connect(COMMITTED)) as con:

        def one(sql: str, *args: object) -> D:
            return D(con.execute(sql, args).fetchone()[0])

        symbols = ["ALFA", "BRAV", "CHAR", "DELT"]
        sid = {
            s: int(one("SELECT security_id FROM security WHERE symbol = ?", s)) for s in symbols
        }
        free = {"CHAR": D("0.8")}
        with localcontext() as ctx:
            ctx.prec = 34
            caps = {
                s: one("SELECT market_cap FROM market_cap WHERE security_id = ? AND "
                       "cap_date = '2025-01-02'", sid[s]) * free.get(s, D(1))
                for s in symbols
            }  # fmt: skip
            total = sum(caps.values(), D(0))
            expected = D(1000) * sum(
                (
                    caps[s] / total
                    * one("SELECT close FROM close WHERE security_id = ? AND "
                          "trade_date = '2025-01-03'", sid[s])
                    / one("SELECT close FROM close WHERE security_id = ? AND "
                          "trade_date = '2025-01-02'", sid[s])
                    for s in symbols
                ),
                D(0),
            )  # fmt: skip
        level = one("SELECT price_level FROM level WHERE level_date = '2025-01-03'")
    # Units are rounded to 34 digits when they are set, so the last digits may
    # differ; anything more is a wrong number.
    assert abs(level - expected) < D("1e-25")
