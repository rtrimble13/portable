"""Entry point for the `bmk` CLI."""

from __future__ import annotations

import sys

from portable_bmk.app import app
from portable_bmk.argv import normalise_argv
from portable_core.cli.streams import configure_streams


def main() -> None:
    """Run the Typer application, its streams UTF-8 and its global flags anywhere.

    As for `pt` and `pert`: the bytes written do not depend on the console's
    code page (:mod:`portable_core.cli.streams`), and a global flag works after
    the subcommand as well as before it (:mod:`portable_core.cli.argv`).
    """
    configure_streams()
    app(args=normalise_argv(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
