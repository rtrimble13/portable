"""`scripts/release.py`: the bump, the CHANGELOG section, and the release notes. ADR 0049.

The bump counts from the version on main, so running it again on a branch
gives the same number. It turns CHANGELOG.md's [Unreleased] into the
release's section, which the gate requires and the release publishes. The
notes open with what the owner runs after `git pull`.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from types import ModuleType

import pytest

from portable_core.lint._common import repo_root

pytestmark = pytest.mark.unit


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "portable_release_script", repo_root() / "scripts" / "release.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first: a dataclass in it looks its module up by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


release = _load()
ON = date(2026, 11, 25)


@pytest.mark.parametrize(
    ("bump", "becomes"),
    [("patch", "0.2.1"), ("minor", "0.3.0"), ("major", "1.0.0"), ("0.4.0", "0.4.0"),
     ("v0.4.0", "0.4.0")],
)  # fmt: skip
def test_a_bump_counts_from_main(bump: str, becomes: str) -> None:
    assert release.bumped("0.2.0", bump) == becomes


@pytest.mark.parametrize("bump", ["0.2.0", "0.1.9", "0.2", "next"])
def test_a_bump_must_be_an_increase_of_three_numbers(bump: str) -> None:
    with pytest.raises(release.ReleaseError):
        release.bumped("0.2.0", bump)


def test_the_constant_is_rewritten_and_nothing_else() -> None:
    source = '"""Doc."""\n\n__version__ = "0.2.0"\n\nOTHER = "0.2.0"\n'
    assert release.with_version(source, "0.3.0") == (
        '"""Doc."""\n\n__version__ = "0.3.0"\n\nOTHER = "0.2.0"\n'
    )


CHANGELOG = """# Changelog

## [Unreleased]

### Fixed

- A fix.

## [0.2.0] - 2026-11-01

- Earlier.
"""


def test_unreleased_becomes_the_release_and_a_new_one_opens() -> None:
    out = release.with_section(CHANGELOG, "0.3.0", None, ON)
    assert out.startswith(
        "# Changelog\n\n## [Unreleased]\n\n## [0.3.0] - 2026-11-25\n\n### Fixed\n\n- A fix.\n"
    )
    assert release.section_of(out, "0.3.0") == "### Fixed\n\n- A fix."


def test_running_it_again_changes_nothing() -> None:
    once = release.with_section(CHANGELOG, "0.3.0", None, ON)
    assert release.with_section(once, "0.3.0", None, ON) == once


def test_a_branch_that_changes_its_bump_renames_its_section() -> None:
    """minor first, then major on the same branch: one section, renamed."""
    minor = release.with_section(CHANGELOG, "0.3.0", None, ON)
    major = release.with_section(minor, "1.0.0", "0.3.0", ON)
    assert "## [0.3.0]" not in major
    assert release.section_of(major, "1.0.0") == "### Fixed\n\n- A fix."


def test_with_no_unreleased_section_there_is_nothing_to_release() -> None:
    with pytest.raises(release.ReleaseError, match="Unreleased"):
        release.with_section(
            "# Changelog\n\n## [0.2.0] - 2026-11-01\n\n- x\n", "0.3.0", None, ON
        )


def _changes(**overrides: object) -> object:
    fields: dict[str, object] = {
        "previous": "v0.2.0", "reinstall": [], "migrations": [], "raised_schemas": [],
    }  # fmt: skip
    fields.update(overrides)
    return release.Changes(**fields)


def test_the_notes_open_with_what_to_run_after_the_pull() -> None:
    changelog = release.with_section(CHANGELOG, "0.3.0", None, ON)
    plain = release.notes("0.3.0", _changes(), changelog, "rtrimble13/portable")
    # Every release changes the number pip recorded and the extension was
    # compiled with, so every release reinstalls, even one of Python alone.
    assert plain.startswith(
        "## Upgrading\n\n```bash\ngit pull\n"
        "make install    # every release: pip and the native extension record the version\n"
        "pt --version    # 0.3.0\n```"
    )
    assert "## What changed\n\n### Fixed\n\n- A fix." in plain
    assert "blob/v0.3.0/CHANGELOG.md" in plain

    full = release.notes(
        "0.3.0",
        _changes(reinstall=["pyproject.toml"], migrations=["0013_policy_allocation"],
                 raised_schemas=["holdings-2"]),
        changelog,
        "rtrimble13/portable",
    )  # fmt: skip
    assert "make install    # every release, and pyproject.toml changed" in full
    assert full.count("make install") == 1
    assert "pt --port FILE migrate" in full
    assert "Schema migrations since v0.2.0: `0013_policy_allocation`." in full
    assert "`holdings-2.0`" in full


def test_the_first_release_says_to_migrate_files_an_earlier_checkout_wrote() -> None:
    changelog = release.with_section(CHANGELOG, "0.2.0", None, ON)
    first = release.notes(
        "0.2.0", _changes(previous=None),
        changelog, "rtrimble13/portable",
    )  # fmt: skip
    assert "make install    # this is the first release" in first
    assert "pt --port FILE migrate    # each .port file an earlier checkout wrote" in first


def test_a_changelog_too_long_for_a_release_is_linked_not_carried() -> None:
    long = CHANGELOG.replace("- A fix.", "- A fix.\n" + "x" * (release.NOTES_LIMIT + 1))
    changelog = release.with_section(long, "0.3.0", None, ON)
    notes = release.notes("0.3.0", _changes(), changelog, "rtrimble13/portable")
    assert len(notes) < 2_000
    assert "more than a GitHub release holds" in notes


def test_this_repository_s_changelog_gives_the_current_version_its_notes() -> None:
    """The section the release will publish exists, and is not empty, once
    the version has been released at least once under ADR 0049."""
    import portable_core

    text = (repo_root() / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## [{portable_core.__version__}]" not in text:
        pytest.skip("this version predates ADR 0049's changelog sections")
    assert release.section_of(text, portable_core.__version__)
