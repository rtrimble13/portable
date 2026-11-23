"""Integration harness: run the real CLI against a real `.port` file.

These go through `python -m portable_pt` rather than calling functions, because
the things most likely to break -- argument parsing, exit codes, the JSON
envelope, the order commands must be run in -- only exist at that boundary.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
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
    return make_runner(tmp_path)


@pytest.fixture
def run_pert(tmp_path: Path) -> CliRunner:
    """Invoke `pert`, JSON by default, isolated and decoded as `run_pt` is."""

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
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
        }
        env.pop("PORTABLE_PORT", None)
        env |= extra_env or {}
        completed = subprocess.run(
            [sys.executable, "-m", "portable_pert", "--format", fmt, *args],
            capture_output=True,
            # `pert` writes UTF-8 on every platform (portable_core.cli.streams).
            encoding="utf-8",
            check=False,
            cwd=tmp_path,
            env=env,
        )
        result = CliResult(completed)
        if expect is not None:
            assert result.returncode == expect, (
                f"exit {result.returncode}, expected {expect}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result

    return run


def make_runner(tmp_path: Path) -> CliRunner:
    """A `pt` runner whose working and home directory is *tmp_path*.

    `run_pt` gives each test one rooted in its own temp directory; a template
    (below) is built with one rooted in a directory of its own.
    """

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


# ── portfolios built once, copied into each test ─────────────────────────────
#
# Most of this suite's time went on fixtures rebuilding the same portfolio
# through the CLI before every test: 518 of its 756 seconds of integration
# tests, at four calls to `pt` for `portfolio` alone and up to a dozen for the
# fixtures stacked on it. Such a fixture now builds its portfolio once per
# module (per session, for `portfolio`) in a directory of its own, a
# *template*, and each test gets a copy of the file.
#
# `pt` is deterministic (CLAUDE.md invariant 6), so a copy holds what building
# in place would write, but for the stamps read from the wall clock, which
# nothing orders, replays or values by (ADR 0035). Each template is checked
# before any test may copy it, for the two ways a copy could differ:
#
# * a SQLite sidecar beside it, holding writes the file alone does not;
# * a stored value naming the directory it was built in, which a copy would
#   carry into another test's.
#
# A fixture stacked on `portfolio` used to write into its file. Its copy now
# replaces that file instead, so it could silently drop what another fixture
# wrote there first; `stack_template` refuses unless the file is still the
# untouched base.
#
# tests/integration/test_templates.py holds a copy to an in-place build, and
# holds each check to the shapes it refuses.

#: The files SQLite keeps beside a database while a connection is open, or
#: after one was not closed. In WAL mode (`persistence.connection`) the last
#: clean close checkpoints the log into the file and deletes them.
SIDECARS: tuple[str, ...] = ("-wal", "-shm", "-journal")

Builder = Callable[[CliRunner, Path], object]

#: Each stacked template's base, the template it started from. Per process,
#: so per xdist worker, like the templates themselves.
_BASES: dict[Path, Path] = {}


def build_template(
    factory: pytest.TempPathFactory,
    build: Builder,
    *,
    name: str = "t.port",
    start_from: Path | None = None,
) -> Path:
    """Build a portfolio once, in a directory of its own, for tests to copy.

    *build* makes the same calls to `pt` a per-test fixture would, through a
    runner rooted in that directory, given the path of the `.port` file to
    make. With *start_from*, another template, that file is a copy of it.
    """
    root = factory.mktemp(f"template-{build.__name__.strip('_')}")
    path = root / (start_from.name if start_from is not None else name)
    if start_from is not None:
        copy_template(start_from, path)
        _BASES[path] = start_from
    build(make_runner(root), path)
    check_template(path)
    return path


def copy_template(template: Path, to: Path) -> Path:
    """Copy *template* to *to*, replacing what is there, and return *to*."""
    shutil.copyfile(template, to)
    return to


def stack_template(template: Path, portfolio: Path) -> Path:
    """Copy a stacked *template* over *portfolio*, which must still be its base.

    Built in place, two fixtures stacked on `portfolio` both wrote into one
    file. A copy of one template cannot hold another's writes, so a test that
    asks for two, or a fixture that writes to `portfolio` before one, is refused
    rather than quietly handed a single fixture's state.
    """
    base = _BASES[template]
    assert portfolio.read_bytes() == base.read_bytes(), (
        f"{portfolio} is no longer a copy of {base.parent.name}/{base.name}: another "
        f"fixture wrote to it, and a copy of {template.parent.name} would discard that. "
        "Build one template holding both instead."
    )
    return copy_template(template, portfolio)


def check_template(path: Path) -> None:
    """Refuse a template whose copies would not be what building in place makes."""
    left = [path.name + s for s in SIDECARS if path.with_name(path.name + s).exists()]
    assert not left, (
        f"{', '.join(left)} left beside the template: a copy of {path.name} alone "
        "would lose the writes it holds"
    )
    # The directory is named by its unique basename, which catches it however
    # a path was spelled: native, POSIX, resolved or relative.
    named = _values_containing(path, path.parent.name)
    assert not named, (
        f"the template names the directory it was built in, {path.parent}, so a "
        "copy would point another test at this one's files: " + "; ".join(named)
    )


def _values_containing(path: Path, needle: str) -> list[str]:
    """``table.column`` for every stored value in *path* that contains *needle*."""
    # immutable=1: read without locks or a -shm file, so the check itself
    # leaves nothing beside the template.
    con = sqlite3.connect(f"{path.as_uri()}?immutable=1", uri=True)
    try:
        tables = [
            str(row[0])
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        found: list[str] = []
        for table in tables:
            for column in [str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")')]:
                hit = con.execute(
                    f'SELECT 1 FROM "{table}" WHERE instr(CAST("{column}" AS TEXT), ?) > 0 '
                    "LIMIT 1",
                    (needle,),
                ).fetchone()
                if hit is not None:
                    found.append(f"{table}.{column}")
        return found
    finally:
        con.close()


def build_portfolio(run_pt: CliRunner, path: Path) -> None:
    """A funded, taxable-account portfolio ready to trade in."""
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


@pytest.fixture(scope="session")
def portfolio_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """`build_portfolio`'s portfolio, built once per session (per xdist worker)."""
    return build_template(tmp_path_factory, build_portfolio)


@pytest.fixture
def portfolio(portfolio_template: Path, tmp_path: Path) -> Path:
    """A funded, taxable-account portfolio ready to trade in.

    A copy of `portfolio_template`. A fixture stacked on this one copies its own
    template over this path (`stack_template`), so a test that asks for both
    sees one file, as it did when the stacked fixture wrote into this one.
    """
    return copy_template(portfolio_template, tmp_path / "t.port")


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
