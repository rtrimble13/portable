"""Entry point for the `pt` CLI."""

from __future__ import annotations

from portable_pt.app import app


def main() -> None:
    """Run the Typer application. Exit codes are documented in README.md."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
