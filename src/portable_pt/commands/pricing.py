"""Pricing and valuation: price set/load/show, value, mark."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated

import typer

from portable_core.cli.context import CommandContext
from portable_core.decimals import from_text, money_context, quantize_money
from portable_core.domain.enums import ValuationBasis
from portable_core.domain.models import (
    Account,
    Price,
    SnapshotPrice,
    Transaction,
    ValuationSnapshot,
)
from portable_core.errors import DataUnavailableError, ValidationError
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.persistence.connection import transaction as db_transaction
from portable_core.persistence.repositories import Repositories
from portable_core.providers import as_eod_provider
from portable_core.services.replay import effective_ledger, over_dates
from portable_core.services.valuation import Holding, ValuationEngine
from portable_pt import state
from portable_pt.commands._shared import dispatch, maybe_dry_run, resolve_date

price_app = typer.Typer(help="Prices: set, load, show.", no_args_is_help=True)

ZERO = Decimal("0.00")


@price_app.command(name="set")
def set_price(
    symbol: Annotated[str, typer.Argument()],
    price: Annotated[str, typer.Option("--price")],
    date_text: Annotated[str | None, typer.Option("--date", "-d")] = None,
    source: Annotated[str, typer.Option("--source")] = "manual",
    level: Annotated[
        int,
        typer.Option(
            "--valuation-level",
            help=(
                "GIPS fair-value hierarchy, 1-5. A hand-set price with no documented "
                "basis is level 5 (subjective, unobservable) and is counted as such "
                "in reports (PORT-GIPS-A02, H05)."
            ),
        ),
    ] = 5,
    estimate: Annotated[
        bool,
        typer.Option(
            "--estimate",
            help="A preliminary value. Flagged, and triggers a "
            "rebuild when the final one arrives (PORT-GIPS-A09).",
        ),
    ] = False,
) -> None:
    """Set a price by hand.

    The default valuation level is **5**, not 1. That is deliberate: a price
    typed at a terminal with no documented basis is a subjective, unobservable
    input under the GIPS hierarchy, and reporting it as an observable exchange
    close would understate the portfolio's level-5 percentage. Pass
    `--valuation-level 1` when it genuinely is an exchange close you are
    entering by hand.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        on = resolve_date(date_text, ctx)
        instrument = repos.instruments.resolve(symbol, on=on)

        record = Price(
            instrument_id=instrument.instrument_id,
            price_date=on,
            price=from_text(price),
            source=source,
            as_of=datetime.now(UTC),
            valuation_level=level,
            valuation_basis=(ValuationBasis.ESTIMATE if estimate else ValuationBasis.MANUAL),
            is_estimate=estimate,
        )

        payload = {
            "symbol": instrument.symbol,
            "price_date": on.isoformat(),
            "price": record.price,
            "source": source,
            "valuation_level": level,
            "is_estimate": estimate,
        }
        if ctx.dry_run:
            return maybe_dry_run(CommandResult(command="price set", data=payload))

        with db_transaction(repos.con):
            price_id = repos.prices.add(record)

        return CommandResult(
            command="price set",
            data={**payload, "price_id": price_id},
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@price_app.command()
def load(
    symbols: Annotated[list[str] | None, typer.Argument(help="Default: all held.")] = None,
    start: Annotated[str | None, typer.Option("--from")] = None,
    end: Annotated[str | None, typer.Option("--to")] = None,
) -> None:
    """Load prices from the market data provider into the portfolio's cache.

    Every price is stored with its source, as-of timestamp, valuation level,
    and estimate flag, so a valuation built from it can be traced back to the
    tick that produced it (PORT-GIPS-J03).
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        provider = ctx.provider()
        prices_from = as_eod_provider(provider)

        first = resolve_date(start, ctx, what="--from") if start else ctx.as_of
        last = resolve_date(end, ctx, what="--to") if end else ctx.as_of
        wanted = symbols or [i.symbol for i in repos.instruments.all(active_only=True)]

        loaded: list[dict[str, object]] = []
        warnings: list[str] = []

        for symbol in wanted:
            instrument = repos.instruments.resolve(symbol, on=last)
            try:
                prices = prices_from.eod_prices(symbol, first, last)
            except Exception as exc:
                warnings.append(f"{symbol}: {exc}")
                continue

            if not ctx.dry_run:
                with db_transaction(repos.con):
                    for price in prices:
                        from dataclasses import replace

                        repos.prices.add(replace(price, instrument_id=instrument.instrument_id))
            loaded.append(
                {
                    "symbol": symbol,
                    "prices": len(prices),
                    "first": prices[0].price_date.isoformat(),
                    "last": prices[-1].price_date.isoformat(),
                    "source": prices[0].source,
                }
            )

        result = CommandResult(
            command="price load",
            table=Table(
                columns=(
                    Column("symbol", "Symbol"),
                    Column("prices", "Prices", ColumnKind.INTEGER),
                    Column("first", "From", ColumnKind.DATE),
                    Column("last", "To", ColumnKind.DATE),
                    Column("source", "Source"),
                ),
                rows=tuple(loaded),
                title=f"Loaded from {provider.name}",
            ),
            warnings=tuple(warnings),
            portfolio=ctx.portfolio_name(),
        )
        return maybe_dry_run(result) if ctx.dry_run else result

    dispatch(action)


@price_app.command()
def show(
    symbol: Annotated[str, typer.Argument()],
    start: Annotated[str | None, typer.Option("--from")] = None,
    end: Annotated[str | None, typer.Option("--to")] = None,
) -> None:
    """Show cached prices with their provenance."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        last = resolve_date(end, ctx, what="--to") if end else ctx.as_of
        first = resolve_date(start, ctx, what="--from") if start else last - timedelta(days=30)
        instrument = repos.instruments.resolve(symbol, on=last)
        prices = repos.prices.series(instrument.instrument_id, first, last)

        return CommandResult(
            command="price show",
            table=Table(
                columns=(
                    Column("price_date", "Date", ColumnKind.DATE),
                    Column("price", "Price", ColumnKind.PRICE),
                    Column("source", "Source"),
                    Column("as_of", "As Of"),
                    Column("valuation_level", "Level", ColumnKind.INTEGER),
                    Column("is_estimate", "Estimate", ColumnKind.BOOL),
                ),
                rows=tuple(
                    {
                        "price_date": p.price_date.isoformat(),
                        "price": p.price,
                        "source": p.source,
                        "as_of": p.as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "valuation_level": p.valuation_level,
                        "is_estimate": p.is_estimate,
                    }
                    for p in prices
                ),
                title=f"{instrument.symbol} prices",
                footnotes=(
                    "Level is the GIPS fair-value hierarchy: 1 is an observable "
                    "quoted price in an active market, 5 a subjective unobservable "
                    "input (PORT-GIPS-A02).",
                ),
            ),
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


# ── valuation ────────────────────────────────────────────────────────────────


def _holdings_for(repos: Repositories, account_id: int) -> list[Holding]:
    """Open legs in an account, as holdings to be priced.

    Takes no date on purpose. It reads derived state as it currently stands,
    and the caller is what makes that the right date: :func:`_snapshots` runs
    it inside :func:`over_dates`, which rewinds derived state to the date being
    valued when anything later is in the ledger (ADR 0021). Filtering here
    instead would mean re-deriving positions outside replay.
    """
    holdings: list[Holding] = []
    for position in repos.positions.all(account_id=account_id, open_only=True):
        for leg in position.legs:
            if leg.quantity == 0:
                continue
            instrument = repos.instruments.get(leg.instrument_id)
            if instrument is None:
                continue
            holdings.append(Holding(instrument, leg.quantity))
    return holdings


def _build_snapshot(
    ctx: CommandContext,
    repos: Repositories,
    account: Account,
    on: date,
    *,
    beginning: Decimal,
    transactions: list[Transaction],
) -> ValuationSnapshot:
    """Price an account's holdings and assemble its snapshot for one date.

    Reads positions and cash from derived state, so it must run where derived
    state is the state at the end of *on* -- see :func:`_snapshots`.
    *transactions* are the account's effective ledger rows dated *on*: the
    same rows, with the same reversals netted out, that the positions and
    cash were derived from. A flow taken from any other set is a flow the
    market value does not reflect (ADR 0021).

    Instruments that cannot be priced are collected rather than raised on, so
    that one missing price does not hide the rest of the report -- but the
    snapshot is marked incomplete, and an incomplete snapshot must not be used
    for a return.
    """
    engine = ValuationEngine(
        staleness_tolerance_days=int(ctx.config.get("staleness_tolerance_days", 5))
    )
    priced: list[SnapshotPrice] = []
    missing: list[str] = []

    for holding in _holdings_for(repos, account.account_id):
        price = repos.prices.newest_on_or_before(holding.instrument.instrument_id, on)
        try:
            priced.append(engine.price_holding(holding, price, on))
        except DataUnavailableError:
            missing.append(holding.instrument.symbol)

    cash, margin = repos.valuations.cash(account.account_id, currency=account.currency)
    return engine.build_snapshot(
        account,
        on,
        priced=priced,
        cash_balance=cash,
        margin_loan=margin,
        beginning_market_value=beginning,
        transactions=transactions,
        policy=repos.policies.in_force(on),
        incomplete_symbols=tuple(missing),
    )


def _snapshots(
    ctx: CommandContext,
    repos: Repositories,
    accounts: Sequence[Account],
    first: date,
    last: date,
) -> tuple[list[ValuationSnapshot], list[str]]:
    """Every account's snapshot for every day from *first* to *last*.

    Account-major, oldest first. Positions and cash on each day are the state
    at the end of that day, not today's state at that day's prices: when the
    ledger has anything dated after *first*, :func:`over_dates` replays it
    forward in a savepoint that is rolled back afterwards (ADR 0021).

    Beginning market value chains from one day's snapshot to the next in
    memory. Only the day before *first* comes from a stored snapshot, read
    before the walk rewinds derived state.
    """
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]

    rows_on: dict[tuple[int, date], list[Transaction]] = {}
    for txn in effective_ledger(repos.transactions.in_ledger_order()):
        if first <= txn.trade_date <= last:
            rows_on.setdefault((txn.account_id, txn.trade_date), []).append(txn)

    beginning: dict[int, Decimal] = {}
    for target in accounts:
        previous = repos.valuations.snapshot(target.account_id, first - timedelta(days=1))
        beginning[target.account_id] = (
            from_text(str(previous["ending_market_value"])) if previous is not None else ZERO
        )

    def visit(on: date) -> list[ValuationSnapshot]:
        built: list[ValuationSnapshot] = []
        for target in accounts:
            snapshot = _build_snapshot(
                ctx,
                repos,
                target,
                on,
                beginning=beginning[target.account_id],
                transactions=rows_on.get((target.account_id, on), []),
            )
            beginning[target.account_id] = snapshot.ending_market_value
            built.append(snapshot)
        return built

    walked = over_dates(repos, days, visit)
    by_account = [[day[index] for day in walked.results] for index in range(len(accounts))]
    return [snapshot for series in by_account for snapshot in series], list(walked.warnings)


def value(
    date_text: Annotated[str | None, typer.Option("--date", "-d")] = None,
    start: Annotated[str | None, typer.Option("--from")] = None,
    end: Annotated[str | None, typer.Option("--to")] = None,
    account: Annotated[str | None, typer.Option("--account", "-a")] = None,
) -> None:
    """Build and persist valuation snapshots.

    portable values **daily** where prices exist. That satisfies the GIPS
    monthly floor and month-end requirement (PORT-GIPS-A03), satisfies the
    recommendation to value on every external cash flow date, and removes the
    need for any within-period approximation (PORT-GIPS-B06).
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()

        if date_text and (start or end):
            raise ValidationError(
                "--date and --from/--to are mutually exclusive",
                remedy="Value one date, or a range.",
            )
        first = resolve_date(start or date_text, ctx, what="--from")
        last = resolve_date(end or date_text, ctx, what="--to")
        if last < first:
            raise ValidationError(
                f"--to {last.isoformat()} precedes --from {first.isoformat()}",
                remedy="Check the order of the dates.",
            )

        accounts = [repos.accounts.resolve(account)] if account else repos.accounts.all()
        names = {target.account_id: target.name for target in accounts}

        if ctx.dry_run:
            snapshots, warnings = _snapshots(ctx, repos, accounts, first, last)
        else:
            # One transaction: the walk rewinds derived state in a savepoint
            # inside it, and the snapshots it built are written after the
            # savepoint is rolled back -- so nothing can append to the ledger
            # between valuing a day and recording the value.
            with db_transaction(repos.con):
                snapshots, warnings = _snapshots(ctx, repos, accounts, first, last)
                for snapshot in snapshots:
                    repos.valuations.save_snapshot(snapshot)

        rows: list[dict[str, object]] = []
        for snapshot in snapshots:
            name = names[snapshot.account_id]
            on = snapshot.snapshot_date
            if not snapshot.is_complete:
                warnings.append(
                    f"{name} {on.isoformat()}: incomplete -- some positions "
                    "could not be priced. This snapshot must not be used for a "
                    "return."
                )
            rows.append(
                {
                    "account": name,
                    "date": on.isoformat(),
                    "securities": snapshot.securities_value,
                    "cash": snapshot.cash_balance,
                    "accrued": snapshot.accrued_income,
                    "ending": snapshot.ending_market_value,
                    "flow_account": snapshot.external_flow_account,
                    "flow_portfolio": snapshot.external_flow_portfolio,
                    "complete": snapshot.is_complete,
                }
            )

        result = CommandResult(
            command="value",
            table=Table(
                columns=(
                    Column("account", "Account"),
                    Column("date", "Date", ColumnKind.DATE),
                    Column("securities", "Securities", ColumnKind.MONEY),
                    Column("cash", "Cash", ColumnKind.MONEY),
                    Column("accrued", "Accrued", ColumnKind.MONEY),
                    Column("ending", "Ending MV", ColumnKind.MONEY),
                    Column("flow_account", "Flow (acct)", ColumnKind.MONEY),
                    Column("flow_portfolio", "Flow (pf)", ColumnKind.MONEY),
                    Column("complete", "Complete", ColumnKind.BOOL),
                ),
                rows=tuple(rows),
                title="Valuation snapshots",
                footnotes=(
                    "Ending market value is securities + cash - margin loan + accrued "
                    "income. Accrued income is part of value, not a memo "
                    "(PORT-GIPS-A06); market value is net of the margin loan "
                    "(PORT-GIPS-D04).",
                    "An inter-account transfer appears under Flow (acct) and not "
                    "under Flow (pf): it nets to zero at portfolio level "
                    "(PORT-GIPS-B02).",
                ),
            ),
            warnings=tuple(warnings),
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )
        return maybe_dry_run(result) if ctx.dry_run else result

    dispatch(action)


def mark(
    date_text: Annotated[str | None, typer.Option("--date", "-d")] = None,
) -> None:
    """Mark to market for a single date without persisting. The fast path."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        on = resolve_date(date_text, ctx)

        accounts = repos.accounts.all(include_closed=False)
        names = {target.account_id: target.name for target in accounts}
        snapshots, warnings = _snapshots(ctx, repos, accounts, on, on)

        rows: list[dict[str, object]] = []
        with money_context():
            total = ZERO
            for snapshot in snapshots:
                total += snapshot.ending_market_value
                rows.append(
                    {
                        "account": names[snapshot.account_id],
                        "ending": snapshot.ending_market_value,
                        "cash": snapshot.cash_balance,
                        "complete": snapshot.is_complete,
                    }
                )

        return CommandResult(
            command="mark",
            table=Table(
                columns=(
                    Column("account", "Account"),
                    Column("cash", "Cash", ColumnKind.MONEY),
                    Column("ending", "Market Value", ColumnKind.MONEY),
                    Column("complete", "Complete", ColumnKind.BOOL),
                ),
                rows=tuple(rows),
                title=f"Marked to market, {on.isoformat()}",
            ),
            data={"total_market_value": quantize_money(total), "as_of": on.isoformat()},
            warnings=tuple(warnings),
            as_of=on,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)
