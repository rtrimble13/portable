"""Helpers shared across `pt` command modules.

Small, and deliberately so: anything with real logic belongs in a service, not
here (`docs/architecture.md` §9).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import typer

from portable_core.cli.context import CommandContext, parse_date
from portable_core.cli.runner import dry_run_note, run_command
from portable_core.decimals import from_text
from portable_core.errors import UsageError
from portable_core.formatters import CommandResult
from portable_pt import state

__all__ = ["dispatch", "money_arg", "resolve_date"]


def dispatch(action: Callable[[], CommandResult]) -> None:
    """Run a command body, render it, and exit with the right code.

    Every `pt` command ends in a call to this. Centralising it is what makes
    the exit-code table in the README true of all of them rather than of the
    ones somebody remembered.
    """
    options = state.current_options()
    code = run_command(
        action,
        output_format=options.output_format,
        no_color=options.no_color,
        verbose=options.verbose,
    )
    raise typer.Exit(code)


def resolve_date(value: str | None, ctx: CommandContext, *, what: str = "--date") -> date:
    """A date from the command line, defaulting to the context's ``as_of``.

    Never to ``today`` directly: ``as_of`` was already defaulted explicitly at
    context-build time, and reaching for the clock again here would reintroduce
    exactly the hidden dependency `CLAUDE.md` invariant 6 forbids.
    """
    parsed = parse_date(value, what=what)
    return parsed if parsed is not None else ctx.as_of


def money_arg(value: str | None, *, what: str) -> Decimal:
    """A required money argument from the command line."""
    if value is None:
        raise UsageError(f"{what} is required", remedy=f"Pass {what} VALUE.")
    try:
        return from_text(str(value).replace(",", "").replace("$", "").strip())
    except ValueError as exc:
        raise UsageError(
            f"{what} must be a decimal number, got {value!r}", value=value
        ) from exc


def maybe_dry_run(result: CommandResult) -> CommandResult:
    return dry_run_note(result) if state.current_options().dry_run else result
