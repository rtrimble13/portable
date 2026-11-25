"""One repository, one version. ADR 0049.

Five places carried a version number before: `pyproject.toml`,
`portable_core`, `portable_pt`, `portable_pert` and the C++ extension (in
three files of its own), each a literal `0.1.0` that agreed by coincidence.
Every merge that ships is now a release, so the number moves often, and a copy
left behind would be a release that misreports itself.

`src/portable_core/__init__.py` is now the only place it is written. The CLI
packages re-export it, `pyproject.toml` reads it through scikit-build-core's
regex provider, and CMake reads it from the same file. Each of those links is
asserted here, so an edit that brings a literal back fails rather than drifts.
This mirrors rtrimble13/fafnir's `test/fafnir/test_version.py`.
"""

from __future__ import annotations

import ast
import importlib.metadata
import re
import subprocess
import sys

import pytest

import portable_core
import portable_pert
import portable_po
import portable_pt
import portable_risky
from portable_core.lint._common import repo_root

pytestmark = pytest.mark.unit

ROOT = repo_root()
VERSION_FILE = ROOT / "src" / "portable_core" / "__init__.py"

#: Releases are three numbers. The `v` belongs to the git tag, not to the
#: version: PEP 440 metadata carries no prefix.
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

#: Every console script in `[project.scripts]`, each asked by the release.
CLIS = ("pt", "pert", "po", "risky", "portable")


def test_the_version_is_three_numbers() -> None:
    assert SEMVER.match(portable_core.__version__), (
        f"{portable_core.__version__!r} is not X.Y.Z. scripts/release.py sets it; "
        "something has edited the constant by hand."
    )


def test_every_package_reports_the_same_version() -> None:
    """The re-exports are the mechanism; this is what they exist for."""
    for package in (portable_pt, portable_pert, portable_po, portable_risky):
        assert package.__version__ == portable_core.__version__, package.__name__


@pytest.mark.parametrize("cli", CLIS)
def test_every_cli_reports_the_version(cli: str) -> None:
    """What the release workflow asks each command, asked here first. The
    stubs answer too: it is the one question `po` and `risky` can."""
    entry = {
        "pt": "portable_pt",
        "pert": "portable_pert",
        "po": "portable_po",
        "risky": "portable_risky",
        "portable": "portable_core.cli.portable_cmd",
    }[cli]
    completed = subprocess.run(
        [sys.executable, "-m", entry, "--version"],
        capture_output=True,
        encoding="utf-8",
        check=False,
        cwd=ROOT,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == portable_core.__version__


@pytest.mark.needs_fresh_install
def test_the_distribution_metadata_matches_the_source() -> None:
    """A fresh install picks the version up from the source constant.

    The link `dynamic = ["version"]` provides, and the only test that proves
    it: everything else here reads the source tree, which would look the same
    if the wiring were broken. It reads what *pip* recorded, so it fails
    between an edit of the constant and the next `make install`; seeing it
    fail during ordinary work means the editable install is behind the tree.
    `scripts/release.py` deselects it for that reason, and the release
    workflow installs before it runs it.
    """
    assert importlib.metadata.version("portable") == portable_core.__version__


def test_the_number_is_written_in_exactly_one_place() -> None:
    """A second literal anywhere in src/ is drift waiting to happen.

    Matched on the assignment, not the number, so it holds after a release
    moves it.
    """
    offenders = [
        f"{path.relative_to(ROOT).as_posix()}:{n}"
        for path in sorted((ROOT / "src").rglob("*.py"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.match(r"""^__version__\s*(:[^=]*)?=\s*["']""", line)
    ]
    assert offenders == [f"src/portable_core/__init__.py:{_line_of_assignment()}"]


def _line_of_assignment() -> int:
    for n, line in enumerate(VERSION_FILE.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("__version__ ="):
            return n
    raise AssertionError(f"no __version__ assignment in {VERSION_FILE}")


def test_the_constant_is_a_plain_literal_a_build_can_read() -> None:
    """pyproject.toml and CMake each read the line with a pattern, without
    importing the package: an annotation, an f-string or a call would make
    them miss it, or make a build run the code it builds."""
    tree = ast.parse(VERSION_FILE.read_text(encoding="utf-8"))
    assigned = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)
    ]
    assert len(assigned) == 1, "expected exactly one plain `__version__ = ...`"
    assert isinstance(assigned[0], ast.Constant)
    assert isinstance(assigned[0].value, str)


def test_pyproject_takes_its_version_from_the_constant() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in text
    assert re.search(r"^version\s*=", text, re.MULTILINE) is None, "a static version"
    assert 'provider = "scikit_build_core.metadata.regex"' in text
    assert 'input = "src/portable_core/__init__.py"' in text


def test_the_extension_reads_the_constant_and_writes_no_number_of_its_own() -> None:
    """CMake reads the version from the source file, and the C++ uses the
    definition it makes. A literal version in cpp/ would be a sixth copy."""
    cmake = (ROOT / "cpp" / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "src/portable_core/__init__.py" in cmake
    assert "VERSION ${PORTABLE_VERSION}" in cmake
    literals = [
        f"{path.relative_to(ROOT).as_posix()}: {line.strip()}"
        for path in sorted((ROOT / "cpp").rglob("*"))
        if path.suffix in {".cpp", ".hpp", ".txt"}
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.search(r'"\d+\.\d+\.\d+"', line) or re.search(r"VERSION \d+\.\d+\.\d+$", line)
    ]
    assert literals == [], literals


@pytest.mark.needs_fresh_install
def test_the_extension_reports_the_package_version() -> None:
    """Built or not, `build_info()` names this version: the reference reads
    the constant, and the extension the definition CMake made from it.

    A built extension carries the number it was compiled with, so like pip's
    record it lags an edit of the constant until the next `make install`.
    That is why `make bump`, which has just made that edit, deselects it.
    """
    from portable_core import native

    assert native.build_info().module_version == portable_core.__version__


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")
def test_any_tag_on_this_commit_matches_the_version() -> None:
    """A tag names a commit; the version is what that commit says it is.

    Asserts only when HEAD carries a release tag, so ordinary work is
    unaffected. On a release commit this is the release workflow's check,
    brought forward to where a developer sees it.
    """
    tags = subprocess.run(
        ["git", "tag", "--points-at", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        check=False,
    ).stdout.split()
    version_tags = [t for t in tags if re.match(r"^v\d+\.\d+\.\d+$", t)]
    if not version_tags:
        pytest.skip("HEAD is not a release commit")
    assert version_tags == [f"v{portable_core.__version__}"]
