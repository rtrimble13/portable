"""`bmk`'s global flags, which work after the subcommand as well as before it.

Every option the root callback declares (`portable_bmk.app.main`) except the
eager ``--version``. A test holds the two together, as for `pt` and `pert`.
"""

from __future__ import annotations

from typing import Final

from portable_core.cli import argv as _argv

__all__ = ["GLOBAL_FLAGS", "GLOBAL_VALUE_FLAGS", "normalise_argv"]

GLOBAL_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "--quiet", "-q", "--no-color", "--log-json", "-v", "-vv", "-vvv", "--verbose",
        "--offline", "--dry-run", "--yes", "-y",
    }
)  # fmt: skip

GLOBAL_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"--bmk", "--format", "-f", "--as-of", "--source"}
)


def normalise_argv(argv: list[str]) -> list[str]:
    """``bmk levels --format json`` becomes ``bmk --format json levels``."""
    return _argv.normalise_argv(argv, flags=GLOBAL_FLAGS, value_flags=GLOBAL_VALUE_FLAGS)
