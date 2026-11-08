"""Global flag state for `pert`, shared between the Typer callback and each command.

`pert` reads and never writes, so this is smaller than `pt`'s: the options, the
context it opens, and a dispatch that renders and exits. What both CLIs need
lives in `portable_core.cli`; no CLI imports another (`CLAUDE.md`).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

import typer

from portable_core.cli.context import (
    CommandContext,
    GlobalOptions,
    build_context,
    make_options,
    resolve_date,
)
from portable_core.cli.runner import run_command
from portable_core.formatters import CommandResult

__all__ = ["dispatch", "resolve_date", "set_options", "with_portfolio"]

_OPTIONS = GlobalOptions()
_OPENED: list[CommandContext] = []


def set_options(
    *,
    port: Path | None = None,
    output_format: str = "table",
    as_of: str | None = None,
    verbose: int = 0,
    quiet: bool = False,
    no_color: bool = False,
) -> GlobalOptions:
    """Record the global flags parsed by the Typer callback."""
    global _OPTIONS
    _OPTIONS = make_options(
        port=port,
        output_format=output_format,
        as_of=as_of,
        verbose=verbose,
        quiet=quiet,
        no_color=no_color,
    )
    return _OPTIONS


def with_portfolio() -> CommandContext:
    """Open the portfolio named by the global flags, read-only in intent."""
    ctx = build_context(_OPTIONS, require_portfolio=True)
    _OPENED.append(ctx)
    return ctx


def dispatch(action: Callable[[], CommandResult]) -> None:
    """Run a command body, render it, close what it opened, and exit with its code."""
    try:
        code = run_command(
            action,
            output_format=_OPTIONS.output_format,
            no_color=_OPTIONS.no_color,
            verbose=_OPTIONS.verbose,
            quiet=_OPTIONS.quiet,
        )
    finally:
        while _OPENED:
            _OPENED.pop().close()
    sys.stdout.flush()
    raise typer.Exit(code)


def as_of_default() -> date | None:
    return _OPTIONS.as_of
