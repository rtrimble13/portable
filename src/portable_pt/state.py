"""Global flag state, shared between the Typer callback and each command.

Typer's callback runs before the subcommand and is where global options are
parsed; the subcommand needs them. A module-level holder is the pragmatic way
to bridge that, and it is confined to this one module so the coupling is
visible rather than spread through every command.
"""

from __future__ import annotations

from pathlib import Path

from portable_core.cli.context import CommandContext, GlobalOptions, build_context, parse_date
from portable_core.errors import UsageError
from portable_core.formatters import OutputFormat

__all__ = ["current_options", "set_options", "with_portfolio"]

_OPTIONS = GlobalOptions()


def set_options(
    *,
    port: Path | None = None,
    output_format: str = "table",
    as_of: str | None = None,
    source: str | None = None,
    offline: bool = False,
    dry_run: bool = False,
    yes: bool = False,
    verbose: int = 0,
    quiet: bool = False,
    no_color: bool = False,
) -> GlobalOptions:
    """Record the global flags parsed by the Typer callback."""
    global _OPTIONS
    try:
        fmt = OutputFormat(output_format)
    except ValueError as exc:
        raise UsageError(
            f"unknown output format {output_format!r}",
            remedy="Choose one of: table, json, markdown, csv.",
            value=output_format,
        ) from exc

    _OPTIONS = GlobalOptions(
        port=port,
        output_format=fmt,
        as_of=parse_date(as_of, what="--as-of"),
        source=source,
        offline=offline,
        dry_run=dry_run,
        yes=yes,
        verbose=verbose,
        quiet=quiet,
        no_color=no_color,
    )
    return _OPTIONS


def current_options() -> GlobalOptions:
    return _OPTIONS


def with_portfolio(*, check_schema: bool = True) -> CommandContext:
    """Open the portfolio named by the global flags."""
    return build_context(_OPTIONS, require_portfolio=True, check_schema=check_schema)
