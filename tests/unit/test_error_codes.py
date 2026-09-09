"""The stable error-code registry, which nothing had been checking.

`errors/kinds.py` says of `ERROR_CODES`: "`pt introspect` publishes this and
`tests/unit/test_errors.py` asserts the values are unique -- a duplicated code
is a script somewhere branching on the wrong failure." That test file did not
exist, and the registry had drifted accordingly: `PT-E-WITHHOLDING-INVALID`,
`PT-E-DUPLICATE-REF` and `PT-E-MIGRATION-BLOCKED` were raised in production and
absent from the published list, so `pt introspect` under-reported the failures a
consumer had to handle.

The completeness test below is the one that would have caught it, and is the
reason there is now a file here rather than a promise of one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from portable_core.errors import kinds
from portable_core.errors.base import ExitCode, PortableError
from portable_core.lint._common import repo_root

pytestmark = pytest.mark.unit

_DECLARED = re.compile(r"^(E_[A-Z0-9_]+): Final", re.MULTILINE)


def _declared_names() -> set[str]:
    source = (repo_root() / "src" / "portable_core" / "errors" / "kinds.py").read_text(
        encoding="utf-8"
    )
    return set(_DECLARED.findall(source))


def test_every_declared_code_is_published() -> None:
    """The registry is the contract; a code missing from it is invisible.

    Read from the module source rather than from `dir()` so that a constant
    added to the file and forgotten in the tuple fails here, which is exactly
    how the three that drifted got out.
    """
    missing = sorted(
        name for name in _declared_names() if getattr(kinds, name) not in kinds.ERROR_CODES
    )
    assert not missing, f"declared but not in ERROR_CODES: {', '.join(missing)}"


def test_every_published_code_is_declared() -> None:
    published = set(kinds.ERROR_CODES)
    declared = {getattr(kinds, name) for name in _declared_names()}
    assert published <= declared


def test_codes_are_unique() -> None:
    """A duplicate is a script branching on the wrong failure."""
    seen: dict[str, int] = {}
    for code in kinds.ERROR_CODES:
        seen[code] = seen.get(code, 0) + 1
    assert [code for code, count in seen.items() if count > 1] == []


def test_codes_share_one_prefix() -> None:
    assert all(code.startswith("PT-E-") for code in kinds.ERROR_CODES)


def test_the_import_codes_are_published() -> None:
    """The ones this milestone added, named so a consumer can branch on them."""
    for code in (
        "PT-E-IMPORT-SOURCE",
        "PT-E-IMPORT-COLUMN",
        "PT-E-ACTIVITY-UNMAPPED",
        "PT-E-IMPORT-CAPABILITY",
    ):
        assert code in kinds.ERROR_CODES


def test_the_codes_that_had_drifted_are_published() -> None:
    """The regression this file exists for."""
    for code in (
        "PT-E-WITHHOLDING-INVALID",
        "PT-E-DUPLICATE-REF",
        "PT-E-MIGRATION-BLOCKED",
    ):
        assert code in kinds.ERROR_CODES


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (kinds.UsageError, ExitCode.USAGE),
        (kinds.PortfolioFileError, ExitCode.PORTFOLIO),
        (kinds.ValidationError, ExitCode.VALIDATION),
        (kinds.DataUnavailableError, ExitCode.DATA_UNAVAILABLE),
        (kinds.ReconciliationBreakError, ExitCode.RECONCILIATION),
        (kinds.GipsRefusalError, ExitCode.VALIDATION),
    ],
)
def test_each_class_carries_its_exit_code(
    error: type[PortableError], expected: ExitCode
) -> None:
    """The exit-code table in the README is true of the classes, not of memory."""
    assert error.exit_code == expected


def test_the_module_docstring_names_a_test_file_that_exists() -> None:
    """It named this one's predecessor for a year and nobody noticed.

    A comment pointing at a guard that does not exist is worse than no comment:
    it is a reason not to look.
    """
    source = (repo_root() / "src" / "portable_core" / "errors" / "kinds.py").read_text(
        encoding="utf-8"
    )
    named = re.findall(r"tests/unit/(test_[a-z_]+)\.py", source)
    assert named, "kinds.py no longer names its guard"
    for name in named:
        assert (Path(repo_root()) / "tests" / "unit" / f"{name}.py").exists(), name
