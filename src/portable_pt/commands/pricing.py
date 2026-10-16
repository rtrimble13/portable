"""Pricing and valuation: price set/load/show, value, mark."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
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
    ValuationSnapshot,
)
from portable_core.errors import DataUnavailableError, ValidationError
from portable_core.errors.kinds import E_PRICE_MISSING
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.persistence.repositories import Repositories
from portable_core.providers import as_eod_provider
from portable_core.services import price_history
from portable_core.services import snapshots as snapshots_service
from portable_pt import state
from portable_pt.commands._shared import dispatch, resolve_date

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
            help="A preliminary value. Flagged; when the final one replaces it, "
            "`pt validate` names the snapshots that used it (PORT-GIPS-A09).",
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
        if not instrument.has_market_price:
            raise ValidationError(
                f"{instrument.symbol} is marked as having no market price, and is valued "
                "at zero (ADR 0028)",
                remedy=(
                    f"If something now quotes it, `pt instrument market-price "
                    f"{instrument.symbol} --quoted` first."
                ),
                symbol=instrument.symbol,
            )

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
        with ctx.write():
            price_id = repos.prices.add(record)

        return CommandResult(
            command="price set",
            data={**payload, "price_id": price_id},
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@price_app.command()
def load(
    symbols: Annotated[
        list[str] | None,
        typer.Argument(
            help=(
                "Default: every instrument held at some point in the range, over the "
                "days it was held."
            )
        ),
    ] = None,
    start: Annotated[str | None, typer.Option("--from")] = None,
    end: Annotated[str | None, typer.Option("--to")] = None,
) -> None:
    """Load prices from the market data provider into the portfolio's cache.

    With no symbols, each instrument any account held in the range is loaded
    over the days it was held -- read from the ledger, so a position sold
    years ago is priced for the years it was owned (ADR 0020 R2). Each held
    stretch is preceded by a few days, the staleness tolerance, so a stretch
    that begins on a non-trading day can be valued from the previous close.
    Each day is asked for under the ticker the instrument carried that day.

    Every price is stored with its source, as-of timestamp, valuation level,
    and estimate flag, so a valuation built from it can be traced back to the
    tick that produced it (PORT-GIPS-J03). A symbol the provider has no data
    for is a warning; a provider that cannot be reached is a failure, exit 5.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        provider = ctx.provider()
        prices_from = as_eod_provider(provider)

        first = resolve_date(start, ctx, what="--from") if start else ctx.as_of
        last = resolve_date(end, ctx, what="--to") if end else ctx.as_of
        if last < first:
            raise ValidationError(
                f"--to {last.isoformat()} precedes --from {first.isoformat()}",
                remedy="Check the order of the dates.",
            )

        warnings: list[str] = []
        if symbols:
            requests = price_history.requests_for(
                repos, [repos.instruments.resolve(s, on=last) for s in symbols], first, last
            )
        else:
            requests, replay_warnings = price_history.requests_while_held(
                repos,
                first,
                last,
                lead_in_days=int(ctx.config.get("staleness_tolerance_days", 5)),
            )
            warnings.extend(replay_warnings)

        # ADR 0028: nothing quotes these, so nothing is asked for them.
        unquoted = sorted({r.symbol for r in requests if not r.instrument.has_market_price})
        if unquoted:
            requests = [r for r in requests if r.instrument.has_market_price]
            warnings.append(
                f"not loaded, marked as having no market price: {', '.join(unquoted)}"
            )

        loaded: list[dict[str, object]] = []
        missing: list[str] = []
        lead_ins_missing: list[str] = []
        for request in requests:
            try:
                prices = prices_from.eod_prices(request.symbol, request.start, request.end)
            except DataUnavailableError as exc:
                # "No data for this symbol here" is an answer about one symbol
                # and the run goes on. Anything else -- the provider is down,
                # or cannot do this -- is an answer about every symbol, and is
                # raised rather than repeated as a warning per row.
                if exc.code != E_PRICE_MISSING:
                    raise
                span = f"{request.start.isoformat()}..{request.end.isoformat()}"
                if request.lead_in:
                    lead_ins_missing.append(f"{request.symbol} {span}")
                else:
                    missing.append(f"{request.symbol} {span}: {exc.message}")
                continue

            with ctx.write():
                for price in prices:
                    repos.prices.add(
                        replace(price, instrument_id=request.instrument.instrument_id)
                    )
            loaded.append(
                {
                    "symbol": request.symbol,
                    "prices": len(prices),
                    "first": prices[0].price_date.isoformat(),
                    "last": prices[-1].price_date.isoformat(),
                    "source": prices[0].source,
                }
            )

        wanted = [r for r in requests if not r.lead_in]
        if wanted and len(missing) == len(wanted):
            raise DataUnavailableError(
                f"{provider.name} had no prices for anything asked: "
                f"{len(missing)} request(s), none loaded",
                code=E_PRICE_MISSING,
                remedy=(
                    "Check the symbols and the range, or supply the prices with "
                    "`--source file`."
                ),
                provider=provider.name,
                problems=missing,
            )
        warnings.extend(missing)
        if lead_ins_missing:
            warnings.append(
                f"{len(lead_ins_missing)} held stretch(es) had no price in the days "
                "before they began, so a first day that is not a trading day cannot be "
                "valued from a previous close: " + "; ".join(lead_ins_missing)
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
            data={
                "from": first.isoformat(),
                "to": last.isoformat(),
                "requests": len(wanted),
                "loaded": sum(1 for r in requests if not r.lead_in) - len(missing),
            },
            warnings=tuple(warnings),
            portfolio=ctx.portfolio_name(),
        )
        return result

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


def _snapshots(
    ctx: CommandContext,
    repos: Repositories,
    accounts: Sequence[Account],
    first: date,
    last: date,
) -> tuple[list[ValuationSnapshot], list[str]]:
    """Every account's snapshot for every day from *first* to *last* (ADR 0021, 0024)."""
    return snapshots_service.value_range(
        repos,
        accounts,
        first,
        last,
        staleness_tolerance_days=int(ctx.config.get("staleness_tolerance_days", 5)),
    )


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

        # One transaction: the walk rewinds derived state in a savepoint inside
        # it, and the snapshots it built are written after the savepoint is
        # rolled back -- so nothing can append to the ledger between valuing a
        # day and recording the value.
        with ctx.write():
            snapshots, warnings = _snapshots(ctx, repos, accounts, first, last)
            warnings.extend(snapshots_service.store(repos, snapshots))

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
        return result

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
