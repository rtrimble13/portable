"""Entry point for the `pert` CLI."""

from __future__ import annotations

from portable_core.cli.streams import configure_streams
from portable_pert.app import app


def main() -> None:
    """Run the Typer application.

    The streams are set to UTF-8 before anything is written, help included, so
    the bytes `pert` writes do not depend on the console's code page, as for
    `pt` -- see :mod:`portable_core.cli.streams`. A report's disclaimer
    carries characters outside ASCII. Exit codes are documented in
    ``pert --help``.
    """
    configure_streams()
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
