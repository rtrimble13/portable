"""The version gate: every version that reaches main is a release. ADR 0049.

`scripts/check_version.py` runs on every pull request (the `version` job in
CI), because merging one publishes `v<__version__>` when it has no release
yet (the `release` workflow). These tests pin:
- which paths are what ships, and that a change to them needs a new number;
- that the version never goes down;
- that an added migration needs a minor release unless it marks itself a patch;
- that a new major version of an output schema needs a minor (0.y.z) or a
  major (1.0 on) release;
- that a rising version brings its own CHANGELOG section, which becomes the
  release's text.

They mirror rtrimble13/fafnir's `test/ci/test_check_version.py`.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from types import ModuleType

import pytest

from portable_core.lint._common import repo_root

pytestmark = pytest.mark.unit

ROOT = repo_root()


def _load(name: str) -> ModuleType:
    """A stdlib-only script under scripts/, which CI runs before installing."""
    spec = importlib.util.spec_from_file_location(
        f"portable_script_{name}", ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load("check_version")

CHANGELOG = """# Changelog

## [Unreleased]

## [0.3.0] - 2026-11-25

### Fixed

- Something a reader acts on.

## [0.2.0] - 2026-11-01

- Earlier.
"""


def _evaluate(
    base: str,
    head: str,
    paths: list[str],
    migrations: dict[str, str] | None = None,
    schemas: list[str] | None = None,
    changelog: str = CHANGELOG,
) -> tuple[bool, str]:
    return gate.evaluate(base, head, paths, migrations or {}, schemas or [], changelog)  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    "path",
    [
        "src/portable_core/services/holdings.py",
        "src/portable_pt/commands/reporting.py",
        "src/portable_core/schema/0013_policy_allocation.sql",
        "schemas/holdings-2.0.json",
        "cpp/portable_native/probe.cpp",
        "pyproject.toml",
        "scripts/xlsx_to_csv.py",
    ],
)
def test_what_an_install_runs_ships(path: str) -> None:
    assert gate.ships(path)


@pytest.mark.parametrize(
    "path",
    [
        "docs/releasing.md",
        "README.md",
        "CHANGELOG.md",
        "CLAUDE.md",
        "tests/unit/test_version.py",
        ".github/workflows/release.yml",
        ".claude/skills/broker-import/SKILL.md",
        "Makefile",
        "examples/sample.port",
        "scripts/check_version.py",
        "scripts/release.py",
        "scripts/build_sample_portfolio.py",
        "scripts/bootstrap.ps1",
        "src/portable_core/__pycache__/x.cpython-313.pyc",
        "srcfoo/x.py",
    ],
)
def test_the_rest_does_not(path: str) -> None:
    assert not gate.ships(path)


def test_a_change_that_ships_must_raise_the_version() -> None:
    passed, message = _evaluate("0.3.0", "0.3.0", ["src/portable_pt/app.py"])
    assert not passed
    assert "keeps the version at 0.3.0" in message
    assert "scripts/release.py" in message


def test_a_raised_version_passes_and_says_what_merging_publishes() -> None:
    passed, message = _evaluate("0.2.0", "0.3.0", ["src/portable_pt/app.py"])
    assert passed, message
    assert "Merging it publishes v0.3.0" in message


def test_a_change_to_nothing_that_ships_keeps_the_version() -> None:
    passed, message = _evaluate("0.3.0", "0.3.0", ["docs/roadmap.md", "tests/unit/x.py"])
    assert passed, message


def test_versions_only_go_up() -> None:
    passed, message = _evaluate("0.3.0", "0.2.9", ["src/portable_pt/app.py"])
    assert not passed
    assert "only go up" in message


@pytest.mark.parametrize("bad", ["0.3", "v0.3.0", "0.3.0-rc.1", ""])
def test_the_version_is_three_numbers(bad: str) -> None:
    passed, message = _evaluate("0.2.0", bad, ["src/x.py"])
    assert not passed
    assert "not X.Y.Z" in message


def test_a_migration_is_not_a_patch() -> None:
    migration = {"0013_policy_allocation": "CREATE TABLE asset_class (id INTEGER);"}
    passed, message = _evaluate(
        "0.2.0", "0.2.1", ["src/portable_core/schema/0013_policy_allocation.sql"], migration,
        changelog=CHANGELOG.replace("0.3.0", "0.2.1"),
    )  # fmt: skip
    assert not passed
    assert "pt migrate" in message and "minor" in message
    passed, _ = _evaluate("0.2.0", "0.3.0", ["src/x.sql"], migration)
    assert passed


def test_a_migration_that_only_fixes_can_say_so() -> None:
    migration = {"0013_index": "-- release: patch\nCREATE INDEX ix ON lot (open_date);"}
    passed, message = _evaluate(
        "0.2.0", "0.2.1", ["src/portable_core/schema/0013_index.sql"], migration,
        changelog=CHANGELOG.replace("0.3.0", "0.2.1"),
    )  # fmt: skip
    assert passed, message


def test_a_new_major_output_schema_is_a_minor_release_before_one_point_oh() -> None:
    changelog = CHANGELOG.replace("0.3.0", "0.2.1")
    passed, message = _evaluate(
        "0.2.0", "0.2.1", ["schemas/holdings-2.0.json"], schemas=["holdings-2"],
        changelog=changelog,
    )  # fmt: skip
    assert not passed
    assert "holdings-2" in message and "minor" in message
    assert _evaluate("0.2.0", "0.3.0", ["schemas/holdings-2.0.json"], schemas=["holdings-2"])[0]


def test_a_new_major_output_schema_is_a_major_release_from_one_point_oh() -> None:
    changelog = CHANGELOG.replace("0.3.0", "1.1.0")
    passed, message = _evaluate(
        "1.0.0", "1.1.0", ["schemas/holdings-3.0.json"], schemas=["holdings-3"],
        changelog=changelog,
    )  # fmt: skip
    assert not passed
    assert "major release" in message


def test_a_new_major_is_one_beside_an_older_one_not_a_new_schema() -> None:
    base = ["schemas/holdings-1.0.json", "schemas/tax-1.0.json", "schemas/tax-2.0.json"]
    added = ["schemas/holdings-2.0.json", "schemas/lot-candidates-1.0.json"]
    assert gate.new_output_majors(base, added) == ["holdings-2"]


@pytest.mark.parametrize(
    ("changelog", "says"),
    [
        (CHANGELOG.replace("## [0.3.0] - 2026-11-25", "## [0.2.9] - 2026-11-25"), "[0.2.9]"),
        (CHANGELOG.replace(" - 2026-11-25", ""), "needs its date"),
        (CHANGELOG.replace("### Fixed\n\n- Something a reader acts on.\n", ""), "is empty"),
        (
            CHANGELOG.replace("## [Unreleased]\n\n", "").replace(
                "## [0.2.0]", "## [Unreleased]\n\n## [0.2.0]"
            ),
            "newest first",
        ),
    ],
    ids=["no-section", "no-date", "empty", "unreleased-not-first"],
)
def test_a_release_brings_its_own_changelog_section(changelog: str, says: str) -> None:
    passed, message = _evaluate("0.2.0", "0.3.0", ["src/x.py"], changelog=changelog)
    assert not passed
    assert says in message


def test_the_older_annotated_constant_is_read_as_a_base() -> None:
    """Commits before ADR 0049 wrote `__version__: Final[str] = "0.1.0"`."""
    text = 'from typing import Final\n\n__version__: Final[str] = "0.1.0"\n'
    assert gate.VERSION_LINE.search(text).group(1) == "0.1.0"


def test_the_gate_runs_on_this_repository() -> None:
    """Against its own commit it passes: nothing changed between them."""
    done = subprocess.run(
        [sys.executable, "scripts/check_version.py", "--base", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "nothing that ships changed" in done.stdout
