"""`bmk`, end to end on files (ADR 0050).

The same two-stock index the service tests build, here through the CLI as the
owner would: equal-weighted, never rebalanced, from 2024-01-02.

    day        AAA   BBB
    01-02       10    20
    01-03       11    20     BBB goes ex 0.40
    01-04       12    22

    price   100, 105, 115
    tr      100, 106, 106 x 115 / 105
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import ROOT, CliResult
from tests.unit.test_json_schemas import _validator

pytestmark = pytest.mark.integration

CALENDAR = "exchange,date\nXNYS,2024-01-02\nXNYS,2024-01-03\nXNYS,2024-01-04\n"
PRICES = (
    "symbol,date,price\nAAA,2024-01-02,10\nAAA,2024-01-03,11\nAAA,2024-01-04,12\n"
    "BBB,2024-01-02,20\nBBB,2024-01-03,20\nBBB,2024-01-04,22\n"
)
ACTIONS = (
    "symbol,action_type,ex_date,cash_amount\nBBB,cash_dividend,2023-10-03,0.40\n"
    "BBB,cash_dividend,2024-01-03,0.40\n"
)


class Bmk:
    """`bmk` in a directory of its own, reading its files from there."""

    def __init__(self, root: Path) -> None:
        self.root = root
        for name, text in (("cal.csv", CALENDAR), ("px.csv", PRICES), ("ca.csv", ACTIONS)):
            (root / name).write_text(text, encoding="utf-8")
        self.file = root / "t.bmk"

    def __call__(
        self, *args: str, expect: int | None = 0, cli: str = "portable_bmk"
    ) -> CliResult:
        env = dict(os.environ)
        env |= {
            "PYTHONPATH": str(ROOT / "src"),
            "NO_COLOR": "1",
            "HOME": str(self.root),
            "USERPROFILE": str(self.root),
            "PORTABLE_SOURCE": "file",
            "PORTABLE_PRICE_FILE": str(self.root / "px.csv"),
            "PORTABLE_ACTION_FILE": str(self.root / "ca.csv"),
            "PORTABLE_CALENDAR_FILE": str(self.root / "cal.csv"),
            "PORTABLE_BMK": str(self.file),
        }
        env.pop("PORTABLE_PORT", None)
        completed = subprocess.run(
            [sys.executable, "-m", cli, "--format", "json", *args],
            capture_output=True,
            encoding="utf-8",
            check=False,
            cwd=self.root,
            env=env,
        )
        result = CliResult(completed)
        if expect is not None:
            assert completed.returncode == expect, (args, completed.stdout, completed.stderr)
        return result


@pytest.fixture
def bmk(tmp_path: Path) -> Bmk:
    return Bmk(tmp_path)


def _define(bmk: Bmk) -> None:
    bmk("init", str(bmk.file), "--name", "Two-stock equal weight", "--description", "test",
        "--base-date", "2024-01-02")  # fmt: skip
    for symbol in ("AAA", "BBB"):
        bmk("security", "add", symbol, "--type", "equity")
    bmk("methodology", "set", "--weighting", "equal", "--rebalance", "never",
        "--effective-from", "2024-01-02")  # fmt: skip
    bmk("reconstitute", "AAA", "BBB", "--effective-date", "2024-01-02")
    bmk("refresh", "--through", "2024-01-04")


def _built(bmk: Bmk) -> None:
    _define(bmk)
    bmk("schedule", "accept")
    bmk("build")


def _valid(payload: dict[str, Any], schema: str) -> dict[str, Any]:
    errors = sorted(_validator(schema).iter_errors(payload), key=str)
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors[:5]]
    data: dict[str, Any] = payload["data"]
    return data


def test_an_index_is_defined_refreshed_and_built_from_files(bmk: Bmk) -> None:
    _define(bmk)
    refused = bmk("build", expect=4).json()["error"]
    assert refused["code"] == "PT-E-INDEX-NO-SCHEDULE"
    proposed = bmk("schedule", "propose").data["rows"]
    assert [(r["symbol"], r["schedule"]) for r in proposed] == [
        ("AAA", "none"),
        ("BBB", "irregular"),
    ]
    bmk("schedule", "accept")
    bmk("build")
    levels = _valid(bmk("levels").json(), "bmk-levels-1.0.json")
    assert [r["price_level"] for r in levels["rows"]] == ["100", "105.0", "115.0"]
    assert levels["rows"][1]["total_return_level"] == "106.000"
    assert levels["return_types"] == {
        "total_return_level": "total_return",
        "price_level": "price_only",
    }
    held = _valid(bmk("--as-of", "2024-01-04", "holdings").json(), "bmk-holdings-1.0.json")
    assert {r["symbol"]: r["points"] for r in held["rows"]} == {"AAA": "60.0", "BBB": "55.0"}
    assert bmk("validate").data["problems"] == 0


def test_levels_are_refused_until_built_and_when_the_inputs_have_moved(bmk: Bmk) -> None:
    _define(bmk)
    bmk("schedule", "accept")
    assert bmk("levels", expect=5).json()["error"]["code"] == "PT-E-INDEX-NOT-BUILT"
    bmk("build")
    # A new session, refreshed: the build no longer describes the file.
    (bmk.root / "cal.csv").write_text(CALENDAR + "XNYS,2024-01-05\n", encoding="utf-8")
    (bmk.root / "px.csv").write_text(
        PRICES + "AAA,2024-01-05,12\nBBB,2024-01-05,22\n", encoding="utf-8"
    )
    bmk("refresh", "--through", "2024-01-05")
    stale = bmk("levels", expect=5).json()["error"]
    assert "changed since" in stale["message"]
    assert bmk("validate", expect=4).json()["error"]["code"] == "PT-E-INVARIANT-BROKEN"
    bmk("build")
    assert len(bmk("levels").data["rows"]) == 4


def test_a_changed_input_is_refused_unless_restated(bmk: Bmk) -> None:
    _built(bmk)
    (bmk.root / "px.csv").write_text(
        PRICES.replace("AAA,2024-01-03,11", "AAA,2024-01-03,11.5"), encoding="utf-8"
    )
    refused = bmk("refresh", "--through", "2024-01-04", expect=4).json()["error"]
    assert refused["code"] == "PT-E-INDEX-RESTATEMENT"
    assert refused["context"]["changes"] == [
        "close security_id=1;trade_date=2024-01-03: 11 -> 11.5"
    ]
    bmk("refresh", "--through", "2024-01-04", "--restate", "vendor correction")
    bmk("build")
    assert bmk("levels").data["rows"][1]["price_level"] == "107.50"


def test_a_dry_run_leaves_nothing(bmk: Bmk) -> None:
    bmk("--dry-run", "init", str(bmk.file), "--name", "x", "--description", "x",
        "--base-date", "2024-01-02")  # fmt: skip
    assert not bmk.file.exists()
    _built(bmk)
    noted = bmk("--dry-run", "methodology", "set", "--weighting", "equal", "--rebalance",
                "daily", "--effective-from", "2024-01-03").json()  # fmt: skip
    assert noted["data"]["dry_run"] is True
    assert "DRY RUN: nothing was written to the index." in noted["warnings"]
    assert len(bmk("methodology", "show").data["rows"]) == 1


def test_a_global_flag_works_after_the_subcommand(bmk: Bmk) -> None:
    _built(bmk)
    env_free = bmk("levels", "--format", "json").json()
    assert env_free["command"] == "levels"


def test_pt_refuses_an_index_and_bmk_refuses_a_portfolio(bmk: Bmk) -> None:
    """A .bmk read as a .port at version 1 would have been offered `pt migrate`."""
    _built(bmk)
    refused = bmk("--port", str(bmk.file), "holdings", expect=3, cli="portable_pt").json()
    assert "not a .port portfolio" in refused["error"]["message"]
    sample = ROOT / "examples" / "sample.port"
    copy = bmk.root / "s.port"
    copy.write_bytes(sample.read_bytes())
    refused = bmk("--bmk", str(copy), "show", expect=3).json()
    assert "not a .bmk file" in refused["error"]["message"]


def test_the_json_is_the_same_bytes_twice(bmk: Bmk) -> None:
    _built(bmk)

    def run() -> object:
        payload = json.loads(bmk("levels").stdout)
        payload.pop("generated_at", None)
        return payload

    assert run() == run()
