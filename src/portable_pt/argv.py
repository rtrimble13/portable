"""`pt`'s global flags, which work after the subcommand as well as before it.

The rule that moves them lives in :mod:`portable_core.cli.argv`, shared with
every CLI. What is `pt`'s own is the list: every option its root callback
declares (`portable_pt.app.main`) except the eager ``--version``. A test holds
the two together.
"""

from __future__ import annotations

from typing import Final

from portable_core.cli import argv as _argv

__all__ = ["GLOBAL_FLAGS", "GLOBAL_VALUE_FLAGS", "normalise_argv"]

#: Global flags that take no value. ``-vv`` and ``-vvv`` are ``-v`` repeated,
#: written as one argument.
GLOBAL_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "--offline",
        "--dry-run",
        "--yes",
        "-y",
        "--quiet",
        "-q",
        "--no-color",
        "--log-json",
        "-v",
        "-vv",
        "-vvv",
        "--verbose",
    }
)

#: Global flags that consume the following arg as their value.
GLOBAL_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "--port",
        "--format",
        "-f",
        "--as-of",
        "--source",
        "-S",
    }
)


def normalise_argv(argv: list[str]) -> list[str]:
    """``pt holdings --format json`` becomes ``pt --format json holdings``."""
    return _argv.normalise_argv(argv, flags=GLOBAL_FLAGS, value_flags=GLOBAL_VALUE_FLAGS)
