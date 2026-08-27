"""The `pt` Typer application.

`pt` is thin. Every command here parses arguments, calls one service, hands the
result to a formatter, and chooses an exit code -- the logic lives in
`portable_core` (`docs/architecture.md` §2).

The surface is `pt <noun> <verb>`, with the four trade verbs promoted to the
top level because `pt buy AAPL --qty 100` is what somebody types twenty times a
week and `pt trade buy` would be four extra characters every time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from portable_core import __version__
from portable_core.cli.runner import configure_logging
from portable_core.domain.enums import TransactionType
from portable_pt import state
from portable_pt.commands import (
    account,
    cash,
    instrument,
    introspect,
    portfolio,
    positions,
    pricing,
    query,
    reporting,
    trade,
)

app = typer.Typer(
    name="pt",
    help=(
        "pt -- portfolio and account definition, transactions, and history.\n\n"
        "The ledger is append-only: a mistake is corrected with a reversing entry "
        "plus a new entry, never by editing history. Everything else is derived and "
        "can be rebuilt with `pt rebuild`.\n\n"
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
            help="Report state as known on this date. Defaults to today, explicitly.",
        ),
    ] = None,
    source: Annotated[
        str | None, typer.Option("--source", "-S", help="file | fafnir | null")
    ] = None,
    offline: Annotated[
        bool, typer.Option("--offline", help="Use only prices already cached.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the effects; write nothing.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Confirm everything.")] = False,
    verbose: Annotated[int, typer.Option("--verbose", "-v", count=True)] = 0,
    quiet: Annotated[bool, typer.Option("--quiet", "-q")] = False,
    no_color: Annotated[bool, typer.Option("--no-color")] = False,
    log_json: Annotated[
        bool, typer.Option("--log-json", help="One JSON object per log line, on stderr.")
    ] = False,
    # `version` is never read: Typer's idiom is that the eager callback runs
    # and exits before anything else. The parameter exists so the option does.
    version: Annotated[  # noqa: ARG001
        bool, typer.Option("--version", callback=_version, is_eager=True)
    ] = False,
) -> None:
    """Global options, resolved once for whichever command follows."""
    configure_logging(verbose=verbose, quiet=quiet, log_json=log_json)
    state.set_options(
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


# ── portfolio ────────────────────────────────────────────────────────────────

app.command("init")(portfolio.init)
app.command("info")(portfolio.info)
app.command("migrate")(portfolio.migrate)
app.command("validate")(portfolio.validate)
app.command("rebuild")(portfolio.rebuild)
app.command("export")(portfolio.export_portfolio)
app.command("import")(portfolio.import_portfolio)
app.command("backup")(portfolio.backup)

# ── nouns ────────────────────────────────────────────────────────────────────

app.add_typer(account.app, name="account")
app.add_typer(instrument.app, name="instrument")
app.add_typer(cash.app, name="cash")
app.add_typer(cash.income_app, name="income")
app.add_typer(trade.app, name="trade")
app.add_typer(pricing.price_app, name="price")
app.add_typer(positions.position_app, name="position")
app.add_typer(positions.lot_app, name="lot")
app.add_typer(reporting.policy_app, name="policy")

# ── trading verbs, promoted to the top level ─────────────────────────────────
# `pt buy AAPL --qty 100` is the most-typed command in the tool. `pt trade buy`
# would be correct and slightly worse, twenty times a week.

app.command("buy")(
    trade.make_trade_command(TransactionType.BUY, "Buy, opening or adding to a position.")
)
app.command("sell")(
    trade.make_trade_command(
        TransactionType.SELL,
        "Sell, consuming lots under the account's relief method or --lots.",
    )
)
app.command("short")(
    trade.make_trade_command(
        TransactionType.SELL_SHORT,
        "Sell short. Short sales are always short-term, however long they are held.",
    )
)
app.command("cover")(
    trade.make_trade_command(TransactionType.BUY_TO_COVER, "Buy to cover a short position.")
)

# ── valuation and reporting ──────────────────────────────────────────────────

app.command("value")(pricing.value)
app.command("mark")(pricing.mark)
app.command("holdings")(reporting.holdings)
app.command("pnl")(reporting.pnl)
app.command("tax")(reporting.tax)
app.command("activity")(reporting.activity)
app.command("cash-flows")(reporting.cash_flows)
app.command("reconcile")(reporting.reconcile)
app.command("query")(query.query)
app.command("introspect")(introspect.introspect)
