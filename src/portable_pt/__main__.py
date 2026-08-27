"""Entry point for the `pt` CLI."""

from __future__ import annotations

import sys

from portable_pt.app import app
from portable_pt.argv import normalise_argv


def main() -> None:
    """Run the Typer application.

    ``argv`` is normalised first so that a global flag works after the
    subcommand as well as before it -- see :mod:`portable_pt.argv`. Exit codes
    are documented in README.md and in ``pt --help``.
    """
    app(args=normalise_argv(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
