"""Enforce the layering and placement rules from docs/architecture.md §1.

These rules are what keep `portable_core` reusable and each CLI standalone.
They are easy to state and easy to violate by accident -- one convenient
import and `domain/` depends on `persistence/` forever. So they are tested
rather than documented and hoped for.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from portable_core.lint._common import repo_root

pytestmark = pytest.mark.unit

ROOT = repo_root()
SRC = ROOT / "src"

#: For each package, what it may import from within the repo. See the table in
#: docs/architecture.md §1 -- if you change one, change both.
ALLOWED: dict[str, frozenset[str]] = {
    # `portable_core.decimals` is a stdlib-only leaf: the Decimal boundary
    # itself (ADR 0005). Domain objects are made OF Decimals, so depending on
    # the module that defines how a Decimal is represented is not a layer
    # violation -- it is the layer below.
    "portable_core.domain": frozenset(
        {"portable_core.domain", "portable_core.errors", "portable_core.decimals"}
    ),
    "portable_core.errors": frozenset({"portable_core.errors"}),
    # `portable_core` itself is the root package: a version string and a
    # schema-version constant, with no dependencies of its own. Importing it
    # does not create a cycle or a layer violation.
    "portable_core.schema": frozenset(
        {"portable_core.schema", "portable_core.errors", "portable_core"}
    ),
    "portable_core.persistence": frozenset(
        {
            "portable_core.persistence",
            "portable_core.domain",
            "portable_core.schema",
            "portable_core.errors",
            "portable_core.decimals",
        }
    ),
    "portable_core.providers": frozenset(
        {
            "portable_core.providers",
            "portable_core.domain",
            "portable_core.config",
            "portable_core.errors",
            "portable_core.decimals",
        }
    ),
    "portable_core.services": frozenset(
        {
            "portable_core.services",
            "portable_core.domain",
            "portable_core.persistence",
            "portable_core.providers",
            "portable_core.errors",
            "portable_core.decimals",
            "portable_core.disclaimer",
            "portable_core.native",
        }
    ),
    "portable_core.formatters": frozenset(
        {
            "portable_core.formatters",
            "portable_core.domain",
            "portable_core.errors",
            "portable_core.decimals",
            "portable_core.disclaimer",
            "portable_core",
        }
    ),
    "portable_core.config": frozenset({"portable_core.config", "portable_core.errors"}),
}

CLI_PACKAGES: frozenset[str] = frozenset(
    {"portable_pt", "portable_pert", "portable_po", "portable_risky"}
)


def _python_files(package_dir: Path) -> Iterator[Path]:
    yield from sorted(package_dir.rglob("*.py"))


def _imported_modules(path: Path) -> Iterator[tuple[str, int]]:
    """Yield ``(module, lineno)`` for every import in *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module, node.lineno


def _first_party(module: str) -> bool:
    return module.split(".")[0] in {"portable_core", *CLI_PACKAGES}


@pytest.mark.parametrize("package", sorted(ALLOWED))
def test_package_only_imports_what_its_layer_permits(package: str) -> None:
    """Arrows point inward and down. docs/architecture.md §1."""
    package_dir = SRC / Path(*package.split("."))
    if not package_dir.is_dir():
        pytest.skip(f"{package} does not exist yet")

    allowed = ALLOWED[package]
    violations: list[str] = []

    for path in _python_files(package_dir):
        for module, lineno in _imported_modules(path):
            if not _first_party(module):
                continue
            if any(module == ok or module.startswith(ok + ".") for ok in allowed):
                continue
            rel = path.relative_to(ROOT).as_posix()
            violations.append(f"{rel}:{lineno}: {package} may not import {module}")

    assert not violations, "\n".join(violations)


def test_no_cli_imports_another_cli() -> None:
    """A CLI that reaches into another CLI is no longer standalone.

    If two CLIs need the same logic, it belongs in `portable_core`. This is
    not a style preference -- it is what lets `pert` ship without `po`.
    """
    violations: list[str] = []
    for package in sorted(CLI_PACKAGES):
        package_dir = SRC / package
        if not package_dir.is_dir():
            continue
        others = CLI_PACKAGES - {package}
        for path in _python_files(package_dir):
            for module, lineno in _imported_modules(path):
                if module.split(".")[0] in others:
                    rel = path.relative_to(ROOT).as_posix()
                    violations.append(f"{rel}:{lineno}: {package} imports {module}")

    assert not violations, "\n".join(violations)


def test_sql_appears_only_in_persistence_and_schema() -> None:
    """ADR 0002. A query anywhere else means the logic is in the wrong file.

    Scoped to the `.port` file's own SQL. `providers/fafnir.py` queries a
    FOREIGN database, which CLAUDE.md's other placement rule assigns to it
    explicitly -- "anything touching fafnir's schema or `duk` lives in
    providers/fafnir.py and nowhere else". Both rules are right; the boundary
    between them is which database is being talked to.

    `test_fafnir_is_confined_to_its_adapter` is what keeps that exemption
    honest: the adapter may know fafnir's tables, and nothing else may.
    """
    import re

    # Deliberately narrow: match SQL verbs at the head of a string, which is
    # how a query is written, rather than the bare words, which appear in
    # prose and in enum members.
    sql = re.compile(
        r"""["'](\s*)(SELECT\s|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|"""
        r"""CREATE\s+(TABLE|INDEX|TRIGGER|VIEW)|DROP\s+(TABLE|INDEX|TRIGGER|VIEW)|ALTER\s+TABLE)""",
        re.IGNORECASE,
    )
    allowed_dirs = {"persistence", "schema"}
    foreign_database_adapters = {SRC / "portable_core" / "providers" / "fafnir.py"}
    violations: list[str] = []

    for path in sorted(SRC.rglob("*.py")):
        parts = set(path.relative_to(SRC).parts)
        if parts & allowed_dirs or path in foreign_database_adapters:
            continue
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), start=1):
            if sql.search(line):
                rel = path.relative_to(ROOT).as_posix()
                violations.append(f"{rel}:{i}: SQL outside persistence/")

    assert not violations, "\n".join(violations)


def test_fafnir_is_confined_to_its_adapter() -> None:
    """ADR 0006 and CLAUDE.md: `portable` must never couple to fafnir internals.

    The adapter is allowed to know that `core.daily_price` exists. Nothing
    else is, so that a fafnir migration is a one-file change here.
    """
    import re

    fafnir_shaped = re.compile(
        r"\b(core|mart|ref|ops|landing)\.(security|daily_price|corporate_action|"
        r"symbol_xref|adjustment_factor|v_daily_price_adjusted|security_latest|"
        r"trading_calendar|exchange|ingestion_run)\b"
        r"|\bFAFNIR_DSN\b|\bdukrc\b|\bfafnirrc\b",
    )
    adapter = SRC / "portable_core" / "providers" / "fafnir.py"
    violations: list[str] = []

    for path in sorted(SRC.rglob("*.py")):
        if path == adapter:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if fafnir_shaped.search(line):
                violations.append(
                    f"{path.relative_to(ROOT).as_posix()}:{i}: fafnir internals outside "
                    "providers/fafnir.py (ADR 0006)"
                )

    assert not violations, "\n".join(violations)


def test_every_source_package_is_importable() -> None:
    """A package that does not import is a package CI is not really testing."""
    import importlib

    for package in ["portable_core", *sorted(CLI_PACKAGES)]:
        importlib.import_module(package)
