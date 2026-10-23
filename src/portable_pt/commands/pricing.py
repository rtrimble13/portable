"""Pricing and valuation: price set/load/show, value, mark."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import groupby
from pathlib import Path
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
from portable_core.errors import (
    DataUnavailableError,
    ReconciliationBreakError,
    ValidationError,
)
from portable_core.errors.kinds import E_PRICE_CONFLICT, E_PRICE_MISSING, E_RECONCILE_BREAK
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.persistence.repositories import Repositories
from portable_core.providers import (
    FileProvider,
    as_eod_provider,
    as_trading_calendar_provider,
)
from portable_core.services import price_acceptance, price_gaps, price_history
from portable_core.services import snapshots as snapshots_service
from portable_pt import state
from portable_pt.commands._shared import dispatch, price_line_row, resolve_date

price_app = typer.Typer(help="Prices: set, load, show, check, accept.", no_args_is_help=True)

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
    price_file: Annotated[
        Path | None,
        typer.Option(
            "--file",
            help=(
                "Load from this CSV or JSON price file instead of the market data "
                "provider: the instruments it names, over the days they were held. "
                "--from/--to default to the first and last date in the file."
            ),
        ),
    ] = None,
    on_conflict: Annotated[
        price_history.OnConflict,
        typer.Option(
            "--on-conflict",
            help=(
                "When another source already has a different price for the same day: "
                "refuse (store nothing for that instrument and fail), keep the stored "
                "price, or replace it with this one."
            ),
        ),
    ] = price_history.OnConflict.REFUSE,
) -> None:
    """Load prices from the market data provider, or a file, into the portfolio's cache.

    With no symbols, each instrument any account held in the range is loaded
    over the days it was held -- read from the ledger, so a position sold
    years ago is priced for the years it was owned (ADR 0020 R2). Each held
    stretch is preceded by a few days, the staleness tolerance, so a stretch
    that begins on a non-trading day can be valued from the previous close.
    Each day is asked for under the ticker the instrument carried that day.

    ``--file`` fills what the provider does not hold, from a price file in the
    documented schema (docs/market-data.md). A price that disagrees with one
    another source already gave for the same day is refused unless
    ``--on-conflict`` says which wins: otherwise the valuation would use
    whichever command ran last (ADR 0029).

    Every price is stored with its source, as-of timestamp, valuation level,
    and estimate flag, so a valuation built from it can be traced back to the
    tick that produced it (PORT-GIPS-J03). A symbol the provider has no data
    for is a warning; a provider that cannot be reached is a failure, exit 5.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        from_file = FileProvider(price_file=price_file) if price_file is not None else None
        provider = from_file if from_file is not None else ctx.provider()
        prices_from = as_eod_provider(provider)
        in_file = from_file.price_symbols() if from_file is not None else {}
        if from_file is not None and not in_file:
            raise ValidationError(
                f"{price_file} holds no prices",
                remedy="See docs/market-data.md for the price file schema.",
                path=str(price_file),
            )

        if start:
            first = resolve_date(start, ctx, what="--from")
        elif in_file:
            first = min(f for _, f, _ in in_file.values())
        else:
            first = ctx.as_of
        if end:
            last = resolve_date(end, ctx, what="--to")
        elif in_file:
            last = max(t for _, _, t in in_file.values())
        else:
            last = ctx.as_of
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
            if from_file is not None:
                # A file fills gaps: an instrument it does not mention is not
                # missing from it.
                requests = [r for r in requests if r.symbol.upper() in in_file]

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
        refused: list[price_history.Conflict] = []
        settled: list[price_history.Conflict] = []
        stored_from_file: dict[str, int] = {}
        kept_rows = 0
        # One instrument at a time: its requests are fetched, checked against
        # what other sources already said, and only then written, so a refusal
        # leaves nothing of that instrument stored.
        for _, group in groupby(requests, key=lambda r: r.instrument.instrument_id):
            answers: list[tuple[price_history.PriceRequest, list[Price]]] = []
            for request in group:
                try:
                    answers.append(
                        (
                            request,
                            prices_from.eod_prices(request.symbol, request.start, request.end),
                        )
                    )
                except DataUnavailableError as exc:
                    # "No data for this symbol here" is an answer about one
                    # symbol and the run goes on. Anything else -- the provider
                    # is down, or cannot do this -- is an answer about every
                    # symbol, and is raised rather than repeated per row.
                    if exc.code != E_PRICE_MISSING:
                        raise
                    span = f"{request.start.isoformat()}..{request.end.isoformat()}"
                    if request.lead_in:
                        lead_ins_missing.append(f"{request.symbol} {span}")
                    else:
                        missing.append(f"{request.symbol} {span}: {exc.message}")

            clashes = [
                clash
                for request, prices in answers
                for clash in price_history.conflicts(
                    repos, request.symbol, request.instrument.instrument_id, prices
                )
            ]
            if clashes and on_conflict is price_history.OnConflict.REFUSE:
                refused.extend(clashes)
                continue
            settled.extend(clashes)
            kept = (
                {c.offered.price_date for c in clashes}
                if on_conflict is price_history.OnConflict.KEEP
                else set()
            )
            kept_rows += len(kept)

            for request, fetched in answers:
                prices = [p for p in fetched if p.price_date not in kept]
                if not prices:
                    continue
                with ctx.write():
                    for price in prices:
                        repos.prices.add(
                            replace(price, instrument_id=request.instrument.instrument_id)
                        )
                key = request.symbol.upper()
                stored_from_file[key] = stored_from_file.get(key, 0) + len(prices)
                loaded.append(
                    {
                        "symbol": request.symbol,
                        "prices": len(prices),
                        "first": prices[0].price_date.isoformat(),
                        "last": prices[-1].price_date.isoformat(),
                        "source": prices[0].source,
                    }
                )

        if refused:
            instruments = sorted({c.symbol for c in refused})
            raise ValidationError(
                f"{len(refused)} price(s) for {', '.join(instruments)} differ from the "
                "price another source already gave for the same day; nothing was stored "
                f"for {'that instrument' if len(instruments) == 1 else 'those instruments'}"
                + (f"; {len(loaded)} request(s) for others were loaded" if loaded else ""),
                code=E_PRICE_CONFLICT,
                remedy=(
                    "Decide which is right. Correct the file if it is wrong; otherwise "
                    "pass --on-conflict keep to leave the stored prices and load the "
                    "rest, or --on-conflict replace to value from these instead."
                ),
                conflicts=[c.describe() for c in refused],
                loaded=len(loaded),
            )

        wanted = [r for r in requests if not r.lead_in]
        if wanted and len(missing) == len(wanted):
            raise DataUnavailableError(
                f"{provider.name} had no prices for anything asked: "
                f"{len(missing)} request(s), none loaded",
                code=E_PRICE_MISSING,
                remedy=(
                    "Check the symbols and the range, or supply the prices with "
                    "`pt price load --file PATH`."
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
        if settled:
            verb = "kept" if on_conflict is price_history.OnConflict.KEEP else "replaced"
            warnings.append(
                f"{len(settled)} stored price(s) from another source {verb}: "
                + "; ".join(c.describe() for c in settled)
            )
        if from_file is not None:
            asked = {r.symbol.upper() for r in requests}
            unasked = sorted(set(in_file) - asked)
            if unasked:
                why = (
                    "not asked for"
                    if symbols
                    else f"not held in {first.isoformat()}..{last.isoformat()} under "
                    "that ticker"
                )
                warnings.append(f"in {price_file} but not loaded, {why}: {', '.join(unasked)}")
            outside = (
                sum(rows for symbol, (rows, _, _) in in_file.items() if symbol in asked)
                - sum(stored_from_file.values())
                - kept_rows
            )
            if outside > 0:
                warnings.append(
                    f"{outside} row(s) of {price_file} were not loaded: they fall outside "
                    + (
                        "the range asked for"
                        if symbols
                        else "the days their instrument was held"
                    )
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


@price_app.command()
def gaps(
    start: Annotated[
        str | None, typer.Option("--from", help="Default: the ledger's first date.")
    ] = None,
    end: Annotated[str | None, typer.Option("--to", help="Default: --as-of.")] = None,
) -> None:
    """List the trading days a held instrument has no price for (ADR 0020 R3, R9).

    Every day an account held an instrument, read from the ledger, is checked
    against its venue's trading calendar: a holiday is not a gap, and a
    session with no price from any source is. Valuation tolerates a price a
    few days old; this report does not, so it finds what that tolerance hides,
    once, across the whole history. An instrument marked as having no market
    price is listed, not checked (ADR 0028). Days the calendar does not reach
    are listed as unchecked, never counted either way.

    A report: it exits 0 whatever it finds. Fill a gap with `pt price load
    --from D --to D`, or from a file with `pt price load --file PATH`.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        calendars = as_trading_calendar_provider(ctx.provider())
        last = resolve_date(end, ctx, what="--to") if end else ctx.as_of
        if start:
            first = resolve_date(start, ctx, what="--from")
        else:
            opened = [
                found
                for account in repos.accounts.all()
                if (found := repos.transactions.first_trade_date(account.account_id))
            ]
            if not opened:
                raise ValidationError(
                    "the ledger is empty, so nothing was held",
                    remedy="Record or import transactions first.",
                )
            first = min(opened)
        if last < first:
            raise ValidationError(
                f"--to {last.isoformat()} precedes --from {first.isoformat()}",
                remedy="Check the order of the dates.",
            )

        coverage = price_gaps.report(repos, calendars, first, last)
        warnings = list(coverage.warnings)
        if coverage.gaps:
            warnings.append(
                f"{coverage.unpriced} of {coverage.sessions} held session(s) have no "
                f"price, in {len({g.symbol for g in coverage.gaps})} instrument(s). "
                "`pt price load --from D --to D` fetches them; `pt price load --file "
                "PATH` fills what the provider does not hold."
            )
        warnings.extend(u.describe() for u in coverage.uncovered)
        warnings.extend(
            f"judged by {calendar}'s calendar, for want of their own: instruments on "
            + ", ".join(sorted(venue or "no venue on record" for venue in venues))
            for calendar, venues in _fallbacks(coverage.calendars).items()
        )
        if coverage.unquoted:
            warnings.append(
                "not checked, marked as having no market price: " + ", ".join(coverage.unquoted)
            )

        rows = tuple(
            {
                "symbol": gap.symbol,
                "calendar": gap.calendar,
                "from": gap.start.isoformat(),
                "to": gap.end.isoformat(),
                "sessions": gap.sessions,
            }
            for gap in coverage.gaps
        )
        return CommandResult(
            command="price gaps",
            table=Table(
                columns=(
                    Column("symbol", "Symbol"),
                    Column("calendar", "Calendar"),
                    Column("from", "From", ColumnKind.DATE),
                    Column("to", "To", ColumnKind.DATE),
                    Column("sessions", "Sessions", ColumnKind.INTEGER),
                ),
                rows=rows,
                title=f"Held sessions with no price, {first.isoformat()} to {last.isoformat()}",
                footnotes=(
                    f"{coverage.priced} of {coverage.sessions} held sessions are priced, "
                    f"across {coverage.instruments} instrument(s).",
                    "Sessions are the venue's trading days. A price from any source counts.",
                ),
            ),
            data={
                "from": first.isoformat(),
                "to": last.isoformat(),
                "instruments": coverage.instruments,
                "sessions": coverage.sessions,
                "priced": coverage.priced,
                "unpriced": coverage.unpriced,
                "uncovered": [
                    {
                        "symbol": u.symbol,
                        "calendar": u.calendar,
                        "from": u.start.isoformat(),
                        "to": u.end.isoformat(),
                    }
                    for u in coverage.uncovered
                ],
                "unquoted": list(coverage.unquoted),
                "calendars": [
                    {"venue": venue, "calendar": calendar}
                    for venue, calendar in coverage.calendars
                ],
            },
            warnings=tuple(warnings),
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


def _fallbacks(calendars: Sequence[tuple[str | None, str]]) -> dict[str, list[str | None]]:
    """The venues judged by another venue's calendar, by the calendar used."""
    used: dict[str, list[str | None]] = {}
    for venue, calendar in calendars:
        if venue is None or venue.strip().upper() != calendar:
            used.setdefault(calendar, []).append(venue)
    return used


# ── valuation ────────────────────────────────────────────────────────────────


@price_app.command()
def check(
    account: Annotated[str | None, typer.Option("--account", "-a")] = None,
) -> None:
    """Check every recorded statement line against the prices held now (ADR 0031).

    A line ties when portable's price for the statement's date agrees with
    the statement's (market value over quantity) to the cent a unit. One
    that does not, and has no acceptance for these exact figures, is a break;
    one with no usable price is unpriced. Either blocks the account's returns
    from that date, and exits 6. Lines are recorded with `pt reconcile
    --against STATEMENT.csv --as-of DATE --record`.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        target = repos.accounts.resolve(account) if account else None
        lines = price_acceptance.check_recorded(
            repos,
            staleness_days=int(ctx.config.get("staleness_tolerance_days", 5)),
            account_id=target.account_id if target is not None else None,
        )
        if not lines:
            raise ValidationError(
                "no statement lines are recorded"
                + (f" for {target.name}" if target is not None else ""),
                remedy=(
                    "Record a statement with `pt reconcile --against STATEMENT.csv "
                    "--as-of YYYY-MM-DD --record`. An empty check is not a passing one."
                ),
            )
        blocked = price_acceptance.blocked_from(lines)
        counts = {str(status): 0 for status in price_acceptance.Status}
        for line in lines:
            counts[str(line.status)] += 1

        result = CommandResult(
            command="price check",
            table=Table(
                columns=(
                    Column("account", "Account"),
                    Column("statement_date", "Statement", ColumnKind.DATE),
                    Column("symbol", "Symbol"),
                    Column("quantity", "Quantity", ColumnKind.QUANTITY),
                    Column("stated_price", "Stated", ColumnKind.PRICE),
                    Column("price", "portable", ColumnKind.PRICE),
                    Column("price_date", "Price date", ColumnKind.DATE),
                    Column("difference", "Difference", ColumnKind.MONEY),
                    Column("status", "Status"),
                ),
                rows=tuple(price_line_row(line) for line in lines),
                title="Prices against recorded statements",
                footnotes=(
                    *(
                        f"{line.account} {line.symbol} {line.statement_date.isoformat()}: "
                        f"{line.status} -- {line.note}"
                        for line in lines
                        if line.note
                    ),
                    "Difference is portable's price times the statement's quantity, less "
                    "its market value. A line ties within a cent a unit, plus the half "
                    "cent the statement's rounding accounts for (ADR 0031).",
                ),
            ),
            data={
                "lines": [price_line_row(line) for line in lines],
                "counts": counts,
                "blocked_from": {
                    name: line.statement_date.isoformat() for name, line in blocked.items()
                },
            },
            portfolio=ctx.portfolio_name(),
        )
        if blocked:
            raise ReconciliationBreakError(
                f"{counts['break'] + counts['unpriced']} statement line(s) unresolved; "
                "returns are blocked from "
                + ", ".join(
                    f"{name} {line.statement_date.isoformat()}"
                    for name, line in sorted(blocked.items())
                ),
                code=E_RECONCILE_BREAK,
                remedy=(
                    "Load or set the right price where portable's is wrong. Where the "
                    "custodian's figure is the exception -- an evaluated bond price, a "
                    "different closing print -- accept it with `pt price accept SYMBOL "
                    "--account A --date D --reason TEXT`."
                ),
                breaks=[line.describe() for line in lines if line.is_unresolved],
            )
        return result

    dispatch(action)


@price_app.command()
def accept(
    symbol: Annotated[str, typer.Argument()],
    account: Annotated[str, typer.Option("--account", "-a")],
    date_text: Annotated[str, typer.Option("--date", "-d", help="The statement's date.")],
    reason: Annotated[
        str, typer.Option("--reason", help="Why the difference is right. Required.")
    ],
) -> None:
    """Accept one statement line's difference from portable's price, with a reason.

    The acceptance holds only while every figure it accepted is unchanged:
    the statement's quantity and market value, and portable's price and its
    date. A reloaded price or a re-recorded statement makes the line a break
    again, to be looked at again (ADR 0031). An unpriced line cannot be
    accepted: load or set a price first.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        on = resolve_date(date_text, ctx, what="--date")
        target = repos.accounts.resolve(account)
        instrument = repos.instruments.resolve(symbol, on=on)
        with ctx.write():
            line = price_acceptance.accept(
                repos,
                target,
                instrument,
                on,
                reason,
                staleness_days=int(ctx.config.get("staleness_tolerance_days", 5)),
            )
        return CommandResult(
            command="price accept",
            data=price_line_row(line),
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


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

        # ADR 0031: an unresolved statement line blocks returns from its date.
        # The snapshots are still valid records of value; returns are what
        # `pert` refuses, and until it exists this is where it is said.
        blocked = price_acceptance.blocked_from(
            price_acceptance.check_recorded(
                repos, staleness_days=int(ctx.config.get("staleness_tolerance_days", 5))
            )
        )
        for target in accounts:
            line = blocked.get(target.name)
            if line is not None and line.statement_date <= last:
                warnings.append(
                    f"{target.name}: returns are blocked from "
                    f"{line.statement_date.isoformat()} -- the statement's {line.symbol} "
                    f"line is {line.status}. `pt price check` lists it; fix the price or "
                    "accept the difference with `pt price accept`."
                )

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
