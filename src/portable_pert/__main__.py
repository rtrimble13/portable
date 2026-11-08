"""Entry point for the `pert` CLI."""

from __future__ import annotations

from portable_pert.app import app


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
