"""Every console script `pyproject.toml` declares resolves to a callable.

`[project.scripts]` is read by the installer and by nothing else, so a target
naming a module that does not exist is invisible to every other test: the
editable install succeeds, and the command fails with `ModuleNotFoundError`
the first time somebody runs it. `portable` did exactly that until its stub
existed. This test is what stops it recurring.
"""

from __future__ import annotations

import importlib
import tomllib
from importlib.metadata import EntryPoint
from typing import Any

import pytest

from portable_core.lint._common import repo_root

pytestmark = pytest.mark.unit


def _declared_scripts() -> dict[str, str]:
    with (repo_root() / "pyproject.toml").open("rb") as handle:
        return dict(tomllib.load(handle)["project"]["scripts"])


SCRIPTS = _declared_scripts()


def test_the_scripts_table_is_read() -> None:
    """Without this, an unreadable table would parametrise to no tests at all."""
    assert {"pt", "pert", "po", "risky", "portable"} <= set(SCRIPTS)


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_every_declared_console_script_resolves_to_a_callable(name: str) -> None:
    # EntryPoint parses `module:attr` exactly as the installed script will.
    entry = EntryPoint(name=name, value=SCRIPTS[name], group="console_scripts")
    assert entry.attr, f"{name} = {entry.value!r} names a module but no function"

    try:
        target: Any = importlib.import_module(entry.module)
    except ImportError as exc:
        pytest.fail(f"{name} = {entry.value!r}: {entry.module} does not import ({exc})")

    for part in entry.attr.split("."):
        assert hasattr(target, part), f"{name} = {entry.value!r}: {entry.module} has no {part}"
        target = getattr(target, part)

    assert callable(target), f"{name} = {entry.value!r} is not callable"


def test_portable_is_an_honest_stub(capsys: pytest.CaptureFixture[str]) -> None:
    """CLAUDE.md invariant 10: it says it is not built, links its issue, and fails.

    Nothing goes to stdout (invariant 8): there is no result, so a consumer
    reading stdout must see none rather than help text it might parse.
    """
    # Imported here, not at the top: if the module were missing, the test above
    # must still run and say which script is broken, not fail collection.
    from portable_core.cli import portable_cmd

    assert portable_cmd.main() == 1

    out, err = capsys.readouterr()
    assert out == ""
    assert "NOT YET IMPLEMENTED" in err
    assert portable_cmd.ISSUE_URL in err
    assert "portable config show" in err
