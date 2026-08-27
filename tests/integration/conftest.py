"""Integration harness: run the real CLI against a real `.port` file.

These go through `python -m portable_pt` rather than calling functions, because
the things most likely to break -- argument parsing, exit codes, the JSON
envelope, the order commands must be run in -- only exist at that boundary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from portable_core.lint._common import repo_root

ROOT = repo_root()


class CliResult:
    """What one CLI invocation produced."""

    def __init__(self, completed: subprocess.CompletedProcess[str]) -> None:
        self.returncode = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr

    def json(self) -> dict[str, Any]:
        try:
            return json.loads(self.stdout)  # type: ignore[no-any-return]
        except json.JSONDecodeError as exc:  # pragma: no cover -- diagnostic
            raise AssertionError(
                f"stdout was not JSON (exit {self.returncode}):\n{self.stdout}\n"
                f"stderr:\n{self.stderr}"
            ) from exc

    @property
    def data(self) -> dict[str, Any]:
        return self.json()["data"]  # type: ignore[no-any-return]

    def ok(self) -> CliResult:
        assert self.returncode == 0, (
            f"exit {self.returncode}\nstdout: {self.stdout}\nstderr: {self.stderr}"
        )
        return self


CliRunner = Callable[..., CliResult]


@pytest.fixture
def run_pt(tmp_path: Path) -> CliRunner:
    """Invoke `pt`, JSON by default, against a temp working directory."""

    def run(*args: str, expect: int | None = 0, fmt: str = "json") -> CliResult:
        env = dict(os.environ)
        env |= {
            "PYTHONPATH": str(ROOT / "src"),
            "NO_COLOR": "1",
            # Point the home directory at the temp tree so the run never sees
            # the developer's real ~/.portablerc. USERPROFILE is the one that
            # matters on Windows -- HOME alone leaves Path.home() unable to
            # resolve there, which is a different failure from the one this is
            # isolating.
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
        }
        # Never inherit a portfolio path from the developer's environment.
        env.pop("PORTABLE_PORT", None)
        completed = subprocess.run(
            [sys.executable, "-m", "portable_pt", "--format", fmt, *args],
            capture_output=True,
            text=True,
            check=False,
            cwd=tmp_path,
            env=env,
        )
        result = CliResult(completed)
        if expect is not None:
            assert result.returncode == expect, (
                f"expected exit {expect}, got {result.returncode}\n"
                f"args: {args}\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result

    return run


@pytest.fixture
def portfolio(run_pt: CliRunner, tmp_path: Path) -> Path:
    """A funded, taxable-account portfolio ready to trade in."""
    path = tmp_path / "t.port"
    run_pt("init", str(path), "--name", "Test", "--inception", "2024-01-02")
    run_pt(
        "--port",
        str(path),
        "account",
        "add",
        "--name",
        "B",
        "--type",
        "taxable",
        "--opened",
        "2024-01-02",
        "--relief-method",
        "fifo",
    )
    run_pt(
        "--port",
        str(path),
        "account",
        "tax-rates",
        "set",
        "-a",
        "B",
        "--short",
        "0.37",
        "--long",
        "0.20",
        "--state",
        "0.05",
        "--niit",
        "0.038",
        "--effective-from",
        "2024-01-01",
    )
    run_pt(
        "--port",
        str(path),
        "cash",
        "deposit",
        "-a",
        "B",
        "--amount",
        "500000",
        "--date",
        "2024-01-02",
    )
    return path
