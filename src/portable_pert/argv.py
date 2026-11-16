"""`pert`'s global flags, which work after the subcommand as well as before it.

``pert twr --as-of 2025-06-30`` is what a person types, and until this was
refused with "No such option" (issue 63). The rule that moves the flags lives
in :mod:`portable_core.cli.argv`, shared with `pt`. What is `pert`'s own is the
list: every option its root callback declares (`portable_pert.app.main`)
except the eager ``--version``. A test holds the two together.
"""

from __future__ import annotations

from typing import Final

from portable_core.cli import argv as _argv

__all__ = ["GLOBAL_FLAGS", "GLOBAL_VALUE_FLAGS", "normalise_argv"]

#: Global flags that take no value. ``-vv`` and ``-vvv`` are ``-v`` repeated,
#: written as one argument.
GLOBAL_FLAGS: Final[frozenset[str]] = frozenset(
    {"--quiet", "-q", "--no-color", "--log-json", "-v", "-vv", "-vvv", "--verbose"}
)

#: Global flags that consume the following arg as their value.
GLOBAL_VALUE_FLAGS: Final[frozenset[str]] = frozenset({"--port", "--format", "-f", "--as-of"})


def normalise_argv(argv: list[str]) -> list[str]:
    """``pert twr --as-of 2025-06-30`` becomes ``pert --as-of 2025-06-30 twr``."""
    return _argv.normalise_argv(argv, flags=GLOBAL_FLAGS, value_flags=GLOBAL_VALUE_FLAGS)
