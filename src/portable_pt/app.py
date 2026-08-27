"""The `pt` Typer application.

Command modules register themselves here. The full surface lands in Phase 7;
this module is the assembly point.
"""

from __future__ import annotations

import typer

from portable_core import __version__

app = typer.Typer(
    name="pt",
    help="pt -- portfolio and account definition, transactions, and history.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.command()
def version() -> None:
    """Print the portable version."""
    typer.echo(__version__)
