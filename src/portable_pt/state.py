"""Global flag state, shared between the Typer callback and each command.

Typer's callback runs before the subcommand and is where global options are
parsed; the subcommand needs them. A module-level holder is the pragmatic way
to bridge that, and it is confined to this one module so the coupling is
visible rather than spread through every command.
"""

from __future__ import annotations

from pathlib import Path

from portable_core.cli.context import CommandContext, GlobalOptions, build_context, make_options

__all__ = [
    "close_opened",
    "current_options",
    "forget_opened",
    "opened",
    "set_options",
    "with_portfolio",
]

_OPTIONS = GlobalOptions()

#: Every context :func:`with_portfolio` built for the command now running, so
#: that `dispatch` can report what its writes did beyond the command's own
#: result -- the valuation snapshots a ledger write removed (ADR 0024).
_OPENED: list[CommandContext] = []


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
    _OPTIONS = make_options(
        port=port,
        output_format=output_format,
        as_of=as_of,
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
    ctx = build_context(_OPTIONS, require_portfolio=True, check_schema=check_schema)
    _OPENED.append(ctx)
    return ctx


def opened() -> tuple[CommandContext, ...]:
    """The contexts opened since :func:`forget_opened`."""
    return tuple(_OPENED)


def forget_opened() -> None:
    """Start a command with nothing opened."""
    _OPENED.clear()


def close_opened() -> None:
    """Close every context opened for this command, and forget them."""
    while _OPENED:
        _OPENED.pop().close()
