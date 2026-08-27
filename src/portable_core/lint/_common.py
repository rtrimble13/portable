"""Shared plumbing for the project lint rules."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

#: Directories never scanned, whatever the rule.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "build",
        "dist",
        "htmlcov",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".eggs",
        "node_modules",
    }
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One rule violation, at a point in a file."""

    path: Path
    line: int
    column: int
    code: str
    message: str

    def render(self, root: Path) -> str:
        """Render as ``path:line:col: CODE message`` -- clickable in an editor."""
        try:
            shown = self.path.relative_to(root)
        except ValueError:  # pragma: no cover -- defensive
            shown = self.path
        return f"{shown.as_posix()}:{self.line}:{self.column}: {self.code} {self.message}"


def repo_root(start: Path | None = None) -> Path:
    """Locate the repository root.

    Prefers ``git rev-parse``, falls back to walking up for ``pyproject.toml``
    so the rules still work in an exported tree with no ``.git``.
    """
    here = (start or Path(__file__)).resolve()
    base = here if here.is_dir() else here.parent
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],  # noqa: S607
            cwd=base,
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        return Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        for parent in [base, *base.parents]:
            if (parent / "pyproject.toml").is_file():
                return parent
        return base


def iter_repo_files(root: Path, suffixes: frozenset[str] | None = None) -> Iterator[Path]:
    """Yield every scannable file under *root*, deterministically ordered.

    Determinism matters: ``CLAUDE.md`` invariant 6 applies to tool output too,
    and a lint report whose findings reorder between runs is a diff nobody can
    read.
    """
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if suffixes is not None and path.suffix.lower() not in suffixes:
            continue
        yield path


def read_text(path: Path) -> str | None:
    """Read *path* as UTF-8, returning ``None`` for anything binary."""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
