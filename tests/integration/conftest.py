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

    def run(
        *args: str,
        expect: int | None = 0,
        fmt: str = "json",
        extra_env: dict[str, str] | None = None,
    ) -> CliResult:
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
        env |= extra_env or {}
        completed = subprocess.run(
            [sys.executable, "-m", "portable_pt", "--format", fmt, *args],
            capture_output=True,
            # What `pt` writes is UTF-8 on every platform
            # (portable_core.cli.streams). Decoding it with the locale's
            # encoding instead, the default, would read it as cp1252 on
            # Windows and hand every test mojibake.
            encoding="utf-8",
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


#: What `duk` prints when the warehouse it was pointed at does not answer: the
#: shape of its own refusal, an ``Error:`` line on stderr and exit 1.
DUK_UNREACHABLE = (
    "Error: Failed to read ticker history: connection is bad: connection to server "
    'at "127.0.0.1", port 1 failed: Connection refused'
)


@pytest.fixture
def unreachable_duk(tmp_path: Path) -> tuple[str, Path]:
    """A `duk` on disk that answers every call as duk does when the warehouse is down.

    Returns ``(path, seen)``: name it with ``PORTABLE_DUK_PATH``; ``seen`` is a
    file each call appends the ``FAFNIR_DSN`` it was given to, so a test can
    check which warehouse portable sent it to. An executable script on POSIX, a
    ``.cmd`` shim on Windows -- CI runs both, and ``shutil.which`` must find it.
    """
    folder = tmp_path / "fake-duk"
    folder.mkdir()
    seen = folder / "seen-dsn.txt"
    body = (
        "import os, sys\n"
        f"with open({str(seen)!r}, 'a', encoding='utf-8') as fh:\n"
        "    fh.write((os.environ.get('FAFNIR_DSN') or '') + '\\n')\n"
        f"sys.stderr.write({DUK_UNREACHABLE!r} + '\\n')\n"
        "sys.exit(1)\n"
    )
    script = folder / "duk_impl.py"
    script.write_text(body, encoding="utf-8")
    if os.name == "nt":
        shim = folder / "duk.cmd"
        shim.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        shim = folder / "duk"
        shim.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
        shim.chmod(0o755)
    return str(shim), seen
