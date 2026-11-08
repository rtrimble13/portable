"""The `pert` Typer application.

`pert` is thin: each command parses its arguments, calls one service in
`portable_core`, and hands the result to a formatter. It reads a portfolio and
never writes one. What it computes is governed by `docs/gips-standard.md`, and
every return it prints carries the disclaimer of §9.3 -- in `--format json` as
an envelope field.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from portable_core import __version__
from portable_core.cli.runner import configure_logging
from portable_pert import state
from portable_pert.commands import twr

app = typer.Typer(
    name="pert",
    help=(
        "pert -- performance measurement, from the valuation snapshots `pt value` "
        "stores.\n\n"
        "Time-weighted returns lead (PORT-GIPS-B01). Built so far: `pert twr`. "
        "Money-weighted returns, multi-period reporting, benchmarks, risk and "
        "attribution are planned for milestone v0.2 (docs/roadmap.md).\n\n"
        "Exit codes: 0 ok, 1 generic, 2 usage, 3 portfolio/file, 4 validation, "
        "5 data unavailable, 6 reconciliation break."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    rich_markup_mode=None,
)


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit(0)


@app.callback()
def main(
    port: Annotated[
        Path | None,
        typer.Option("--port", envvar="PORTABLE_PORT", help="The .port file."),
    ] = None,
    output_format: Annotated[
        str, typer.Option("--format", "-f", help="table | json | markdown | csv")
    ] = "table",
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help="Report as known on this date. Defaults to today, explicitly.",
        ),
    ] = None,
    verbose: Annotated[int, typer.Option("--verbose", "-v", count=True)] = 0,
    quiet: Annotated[bool, typer.Option("--quiet", "-q")] = False,
    no_color: Annotated[bool, typer.Option("--no-color")] = False,
    log_json: Annotated[
        bool, typer.Option("--log-json", help="One JSON object per log line, on stderr.")
    ] = False,
    version: Annotated[  # noqa: ARG001 -- Typer's eager-callback idiom
        bool, typer.Option("--version", callback=_version, is_eager=True)
    ] = False,
) -> None:
    """Global options, resolved once for whichever command follows."""
    configure_logging(verbose=verbose, quiet=quiet, log_json=log_json)
    state.set_options(
        port=port,
        output_format=output_format,
        as_of=as_of,
        verbose=verbose,
        quiet=quiet,
        no_color=no_color,
    )


app.command("twr")(twr.twr)
