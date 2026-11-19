"""Every global flag works after the subcommand as well as before it, in every CLI.

`CLAUDE.md` says every command supports the global flags, and click parses a
group's options only before the subcommand. `pt` normalised its `argv` and
`pert` did not, so `pert twr --as-of 2025-06-30` was refused with "No such
option" (issue 63). The rule now lives in `portable_core.cli.argv`, and each
CLI's entry point applies it with its own list of flags.

Three things are held here:

- each CLI's list is exactly the options its root callback declares, so a new
  global flag cannot be left out of it;
- for each flag, both positions give the same output, through the same
  composition the entry point makes;
- the entry point itself normalises, run as a person runs it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import typer
from typer.main import get_command
from typer.testing import CliRunner

import portable_core.cli.portable_cmd
import portable_pert
import portable_pert.app
import portable_pert.argv
import portable_pt.app
import portable_pt.argv
from tests.integration.conftest import ROOT

pytestmark = pytest.mark.integration

SAMPLE = ROOT / "examples" / "sample.port"

#: (CLI, its app, its argv module, the command each flag is tried on).
CLIS = {
    "pt": (portable_pt.app.app, portable_pt.argv, ["info"]),
    "pert": (portable_pert.app.app, portable_pert.argv, ["twr", "--from", "2021-01-04"]),
    "portable": (
        portable_core.cli.portable_cmd.app,
        portable_core.cli.portable_cmd,
        ["config", "show"],
    ),
}

#: A value each value-taking global flag accepts on the sample.
VALUES = {
    "--format": "json",
    "-f": "json",
    "--as-of": "2021-01-31",
    "--source": "file",
    "-S": "file",
}


def _comparable(stdout: str) -> object:
    """*stdout*, less the envelope's `generated_at`.

    That is the one wall-clock value `portable` emits, and it is excluded from
    the content hash for the same reason (`docs/output-formats.md`).
    """
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if isinstance(parsed, dict):
        parsed.pop("generated_at", None)
    return parsed


def _declared(app: typer.Typer) -> tuple[set[str], set[str]]:
    """The root callback's options: (taking no value, taking one). Eager ones excluded."""
    flags: set[str] = set()
    value_flags: set[str] = set()
    for param in get_command(app).params:
        # An option, by what click calls it: Typer vendors its own click.
        if param.param_type_name != "option" or param.is_eager:
            continue
        names = {*param.opts, *param.secondary_opts}
        if getattr(param, "is_flag", False) or getattr(param, "count", False):
            flags |= names
            if getattr(param, "count", False):
                # `-vv` is `-v` twice, written as one argument.
                flags |= {f"-{short[1:] * n}" for short in param.opts if len(short) == 2
                          for n in (2, 3)}  # fmt: skip
        else:
            value_flags |= names
    return flags, value_flags


@pytest.mark.parametrize("cli", sorted(CLIS))
def test_each_cli_moves_exactly_the_flags_its_root_declares(cli: str) -> None:
    app, argv, _command = CLIS[cli]
    flags, value_flags = _declared(app)
    assert set(argv.GLOBAL_FLAGS) == flags
    assert set(argv.GLOBAL_VALUE_FLAGS) == value_flags


def _cases() -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for cli, (app, _argv, _command) in sorted(CLIS.items()):
        flags, value_flags = _declared(app)
        cases += [(cli, flag) for flag in sorted(flags | value_flags) if flag != "--port"]
    return cases


@pytest.mark.parametrize(("cli", "flag"), _cases())
def test_a_global_flag_means_the_same_in_either_position(
    cli: str, flag: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, argv, command = CLIS[cli]
    sample = tmp_path / "sample.port"
    shutil.copyfile(SAMPLE, sample)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("PORTABLE_PORT", raising=False)
    given = [flag, VALUES[flag]] if flag in VALUES else [flag]
    # --as-of is always given, so that pert's period ends where the sample is
    # valued; when it is the flag under test, it is the one that moves.
    as_of = [] if flag == "--as-of" else ["--as-of", "2021-01-31"]

    runner = CliRunner()

    def invoke(args: list[str]) -> tuple[int, object]:
        result = runner.invoke(app, argv.normalise_argv(args), catch_exceptions=False)
        return result.exit_code, _comparable(result.stdout)

    before = invoke(["--port", str(sample), *as_of, *given, *command])
    after = invoke(["--port", str(sample), *as_of, *command, *given])
    assert before[0] == 0, before[1]
    assert after == before


def _run_module(
    module: ModuleType, tmp_path: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env |= {
        "PYTHONPATH": str(ROOT / "src"),
        "NO_COLOR": "1",
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
    }
    env.pop("PORTABLE_PORT", None)
    return subprocess.run(
        [sys.executable, "-m", module.__name__, *args],
        capture_output=True,
        encoding="utf-8",
        check=False,
        cwd=tmp_path,
        env=env,
    )


def test_pert_accepts_as_of_after_the_subcommand(tmp_path: Path) -> None:
    """Issue 63, as reported: `pert twr --as-of 2025-06-30` on the sample."""
    sample = tmp_path / "sample.port"
    shutil.copyfile(SAMPLE, sample)
    after = _run_module(portable_pert, tmp_path, "--port", str(sample), "twr",
                        "--as-of", "2025-06-30", "--format", "json")  # fmt: skip
    before = _run_module(portable_pert, tmp_path, "--port", str(sample), "--as-of",
                         "2025-06-30", "--format", "json", "twr")  # fmt: skip
    assert after.returncode == 0, after.stderr
    assert _comparable(after.stdout) == _comparable(before.stdout)


def test_portable_accepts_format_after_the_subcommand(tmp_path: Path) -> None:
    """`portable config show --format json`, run as a person runs it."""
    after = _run_module(portable_core.cli.portable_cmd, tmp_path, "config", "show",
                        "--format", "json", "--as-of", "2026-01-02")  # fmt: skip
    before = _run_module(portable_core.cli.portable_cmd, tmp_path, "--format", "json",
                         "--as-of", "2026-01-02", "config", "show")  # fmt: skip
    assert after.returncode == 0, after.stderr
    assert _comparable(after.stdout) == _comparable(before.stdout)
