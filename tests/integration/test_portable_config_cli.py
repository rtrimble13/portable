"""`portable config show`: what takes effect, where it came from, and what does not.

Issue 38 and `docs/architecture.md` §6. Run as a person runs it: a real home
directory holding `~/.portablerc`, a `portable.toml` in the working directory,
and `PORTABLE_*` variables, so every layer is the real one.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import ROOT

pytestmark = pytest.mark.integration


def _portable(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> Any:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("PORTABLE_")
    }
    environment |= {
        "PYTHONPATH": str(ROOT / "src"),
        "NO_COLOR": "1",
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
    }
    environment |= env or {}
    return subprocess.run(
        [sys.executable, "-m", "portable_core.cli.portable_cmd", *args],
        capture_output=True,
        encoding="utf-8",
        check=False,
        cwd=tmp_path,
        env=environment,
    )


def _show(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    completed = _portable(tmp_path, "--format", "json", *args, "config", "show", env=env)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)  # type: ignore[no-any-return]


def test_each_setting_says_where_it_came_from(tmp_path: Path) -> None:
    (tmp_path / ".portablerc").write_text('duk_path = "/opt/duk"\nsource = "file"\n')
    (tmp_path / "portable.toml").write_text('price_file = "prices.csv"\n')
    envelope = _show(tmp_path, "--port", "book.port", env={"PORTABLE_SOURCE": "fafnir"})

    rows = {row["key"]: row for row in envelope["data"]["rows"]}
    assert (rows["port"]["value"], rows["port"]["source"]) == ("book.port", "flag")
    assert (rows["source"]["value"], rows["source"]["origin"]) == ("fafnir", "PORTABLE_SOURCE")
    assert (rows["price_file"]["source"], rows["price_file"]["origin"]) == (
        "project",
        "portable.toml",
    )
    assert rows["duk_path"]["origin"] == str(tmp_path / ".portablerc")
    assert rows["staleness_tolerance_days"] == {
        "key": "staleness_tolerance_days",
        "value": 5,
        "source": "default",
        "origin": None,
    }
    assert envelope["data"]["files"]["project"] == {"path": "portable.toml", "found": True}
    assert envelope["warnings"] == []


def test_portable_port_is_reported_as_the_environment(tmp_path: Path) -> None:
    """`pt` reads PORTABLE_PORT through its --port flag; this must not say "flag"."""
    rows = _show(tmp_path, env={"PORTABLE_PORT": "book.port"})["data"]["rows"]
    port = next(row for row in rows if row["key"] == "port")
    assert (port["value"], port["source"], port["origin"]) == (
        "book.port",
        "env",
        "PORTABLE_PORT",
    )


def test_a_secret_is_redacted(tmp_path: Path) -> None:
    completed = _portable(
        tmp_path, "--format", "json", "config", "show",
        env={"PORTABLE_FAFNIR_DSN": "host=db password=hunter2"},
    )  # fmt: skip
    assert "hunter2" not in completed.stdout + completed.stderr
    rows = {row["key"]: row for row in json.loads(completed.stdout)["data"]["rows"]}
    assert rows["fafnir_dsn"]["value"] == "<redacted>"


def test_a_setting_with_no_effect_is_a_warning_not_a_row(tmp_path: Path) -> None:
    """`format` in a file is read by no command; `colour` by nothing at all."""
    (tmp_path / ".portablerc").write_text('format = "json"\ncolour = "no"\n')
    envelope = _show(tmp_path, env={"PORTABLE_YES": "1", "PORTABLE_BUILD_NATIVE": "OFF"})

    keys = {row["key"] for row in envelope["data"]["rows"]}
    assert not keys & {"format", "colour", "yes", "build_native"}
    rc = tmp_path / ".portablerc"
    assert envelope["warnings"] == [
        f"`colour` in {rc} has no effect: no command reads a setting named `colour`.",
        f"`format` in {rc} has no effect: it is set only by --format, on the command line.",
        "`PORTABLE_YES` has no effect: it is set only by --yes, on the command line.",
    ]


def test_the_table_writes_its_warnings_to_stderr(tmp_path: Path) -> None:
    (tmp_path / "portable.toml").write_text("offline = true\n")
    completed = _portable(tmp_path, "config", "show")
    assert completed.returncode == 0
    assert "Effective configuration" in completed.stdout
    assert "offline" not in completed.stdout.split("flag_only")[0]
    assert "`offline` in portable.toml has no effect" in completed.stderr


def test_a_malformed_file_is_a_usage_error(tmp_path: Path) -> None:
    (tmp_path / "portable.toml").write_text("this is = = not toml\n")
    completed = _portable(tmp_path, "--format", "json", "config", "show")
    assert completed.returncode == 2
    assert json.loads(completed.stdout)["error"]["code"] == "PT-E-USAGE"


def test_a_bad_global_flag_is_rendered_not_a_traceback(tmp_path: Path) -> None:
    completed = _portable(tmp_path, "--format", "bogus", "config", "show")
    assert completed.returncode == 2
    assert "Traceback" not in completed.stderr
    assert "unknown output format 'bogus'" in completed.stderr
