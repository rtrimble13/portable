"""Helpers shared across `pt` command modules.

Small, and deliberately so: anything with real logic belongs in a service, not
here (`docs/architecture.md` §9).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Annotated

import typer

from portable_core.cli.context import CommandContext, parse_date
from portable_core.cli.runner import dry_run_note, run_command
from portable_core.decimals import from_text
from portable_core.errors import UsageError
from portable_core.formatters import CommandResult
from portable_core.services.snapshots import removal_notices
from portable_pt import state

__all__ = ["RefOpt", "dispatch", "money_arg", "resolve_date"]

#: Every command that writes a ledger row takes one of these.
#:
#: An importer needs a stable handle on the source row to recognise a duplicate
#: on re-import, and a row recorded without one cannot be traced to the document
#: it came from (ADR 0012, `PORT-GIPS-J03`). Defined once here so the help text
#: cannot drift between the fourteen commands that offer it.
RefOpt = Annotated[
    str | None,
    typer.Option("--ref", help="External reference: a confirm id, or an importer's key."),
]


def dispatch(action: Callable[[], CommandResult]) -> None:
    """Run a command body, render it, and exit with the right code.

    Every `pt` command ends in a call to this. Centralising it is what makes
    the exit-code table in the README true of all of them rather than of the
    ones somebody remembered.
    """
    options = state.current_options()
    state.forget_opened()

    def reported() -> CommandResult:
        result = action()
        # ADR 0024. A ledger write removes the valuation snapshots it makes
        # stale, in whichever command wrote it; saying so here means no command
        # can remove valuation history without the user being told.
        notices = removal_notices(
            (
                removed
                for ctx in state.opened()
                if ctx.repos is not None
                for removed in ctx.repos.transactions.removed_snapshots
            ),
            dry_run=options.dry_run,
        )
        return replace(result, warnings=(*result.warnings, *notices)) if notices else result

    try:
        code = run_command(
            reported,
            output_format=options.output_format,
            no_color=options.no_color,
            verbose=options.verbose,
        )
    finally:
        # Every connection the command opened, closed whether it succeeded or
        # not. An open handle keeps the file locked on Windows.
        state.close_opened()
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
