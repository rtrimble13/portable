"""Instrument definition and hydration."""

from __future__ import annotations

from typing import Annotated

import typer

from portable_core.decimals import from_text
from portable_core.domain.enums import DayCount, ExerciseStyle, InstrumentType, OptionRight
from portable_core.domain.models import BondDetail, Instrument, OptionDetail
from portable_core.errors import DataUnavailableError, ValidationError
from portable_core.errors.kinds import E_PRICE_MISSING
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.persistence.connection import transaction as db_transaction
from portable_core.providers import as_security_master
from portable_core.services import price_history
from portable_pt import state
from portable_pt.commands._shared import dispatch, maybe_dry_run, resolve_date

app = typer.Typer(help="Instruments: the local security master.", no_args_is_help=True)


@app.command()
def add(
    symbol: Annotated[str, typer.Argument(help="Ticker or OCC symbol.")],
    instrument_type: Annotated[
        str,
        typer.Option(
            "--type",
            help="equity | etf | mutual_fund | adr | cash | money_market | option | bond",
        ),
    ] = "equity",
    name: Annotated[str | None, typer.Option("--name")] = None,
    exchange: Annotated[str | None, typer.Option("--exchange")] = None,
    cusip: Annotated[str | None, typer.Option("--cusip")] = None,
    isin: Annotated[str | None, typer.Option("--isin")] = None,
    sector: Annotated[str | None, typer.Option("--sector")] = None,
    asset_class: Annotated[str | None, typer.Option("--asset-class")] = None,
    # option detail
    underlier: Annotated[str | None, typer.Option("--underlier")] = None,
    right: Annotated[str | None, typer.Option("--right", help="call | put")] = None,
    strike: Annotated[str | None, typer.Option("--strike")] = None,
    expiry: Annotated[str | None, typer.Option("--expiry")] = None,
    multiplier: Annotated[
        str,
        typer.Option(
            "--multiplier",
            help="Shares per contract. Stored, never assumed -- an adjusted contract "
            "is not 100.",
        ),
    ] = "100",
    exercise_style: Annotated[str, typer.Option("--exercise-style")] = "american",
    # bond detail
    issuer: Annotated[str | None, typer.Option("--issuer")] = None,
    coupon: Annotated[
        str | None, typer.Option("--coupon", help="Annual rate, e.g. 0.0425")
    ] = None,
    coupon_frequency: Annotated[int, typer.Option("--coupon-frequency")] = 2,
    maturity: Annotated[str | None, typer.Option("--maturity")] = None,
    day_count: Annotated[
        str, typer.Option("--day-count", help="30/360 | ACT/ACT | ACT/365 | ACT/360")
    ] = "ACT/ACT",
    face: Annotated[str, typer.Option("--face")] = "1000",
) -> None:
    """Define an instrument by hand."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        kind = InstrumentType(instrument_type)

        option = None
        bond = None
        if kind is InstrumentType.OPTION:
            if not all([underlier, right, strike, expiry]):
                raise ValidationError(
                    "an option needs --underlier, --right, --strike and --expiry",
                    remedy=(
                        "An option without a strike is not an option. These are "
                        "required rather than defaulted for the same reason the "
                        "multiplier is stored: a guess here is a wrong number with a "
                        "multiplier attached."
                    ),
                )
            assert underlier is not None and expiry is not None
            assert right is not None and strike is not None
            underlier_instrument = repos.instruments.resolve(underlier)
            option = OptionDetail(
                underlier_instrument_id=underlier_instrument.instrument_id,
                option_right=OptionRight(right),
                strike=from_text(str(strike)),
                expiry=resolve_date(expiry, ctx, what="--expiry"),
                multiplier=from_text(multiplier),
                exercise_style=ExerciseStyle(exercise_style),
            )
        elif kind is InstrumentType.BOND:
            if not all([issuer, coupon, maturity]):
                raise ValidationError(
                    "a bond needs --issuer, --coupon and --maturity",
                    remedy=(
                        "The day count matters too: it decides accrued interest, "
                        "which is part of market value (PORT-GIPS-A06)."
                    ),
                )
            assert issuer is not None and coupon is not None and maturity is not None
            bond = BondDetail(
                issuer=issuer,
                coupon_rate=from_text(coupon),
                coupon_frequency=coupon_frequency,
                maturity_date=resolve_date(maturity, ctx, what="--maturity"),
                day_count=DayCount(day_count),
                face_value=from_text(face),
            )

        instrument = Instrument(
            instrument_id=0,
            symbol=symbol.upper(),
            instrument_type=kind,
            name=name,
            exchange=exchange,
            cusip=cusip,
            isin=isin,
            sector=sector,
            asset_class=asset_class,
            option=option,
            bond=bond,
        )

        payload = {
            "symbol": instrument.symbol,
            "instrument_type": str(kind),
            "name": name,
            "contract_size": instrument.contract_size,
        }
        if ctx.dry_run:
            return maybe_dry_run(CommandResult(command="instrument add", data=payload))

        with db_transaction(repos.con):
            instrument_id = repos.instruments.add(instrument)

        return CommandResult(
            command="instrument add",
            data={**payload, "instrument_id": instrument_id},
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@app.command(name="list")
def list_instruments(
    active_only: Annotated[bool, typer.Option("--active-only")] = False,
) -> None:
    """List the local security master."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        instruments = repos.instruments.all(active_only=active_only)

        return CommandResult(
            command="instrument list",
            table=Table(
                columns=(
                    Column("instrument_id", "Id", ColumnKind.INTEGER),
                    Column("symbol", "Symbol", ColumnKind.TEXT),
                    Column("instrument_type", "Type", ColumnKind.TEXT),
                    Column("name", "Name", ColumnKind.TEXT),
                    Column("exchange", "Exchange", ColumnKind.TEXT),
                    Column("sector", "Sector", ColumnKind.TEXT),
                    Column("contract_size", "Contract", ColumnKind.QUANTITY),
                ),
                rows=tuple(
                    {
                        "instrument_id": i.instrument_id,
                        "symbol": i.symbol,
                        "instrument_type": str(i.instrument_type),
                        "name": i.name,
                        "exchange": i.exchange,
                        "sector": i.sector,
                        "contract_size": i.contract_size,
                    }
                    for i in instruments
                ),
                title="Instruments",
            ),
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@app.command()
def show(symbol: Annotated[str, typer.Argument()]) -> None:
    """Show one instrument, including its option or bond detail."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        instrument = repos.instruments.resolve(symbol, on=ctx.as_of)

        data: dict[str, object] = {
            "instrument_id": instrument.instrument_id,
            "symbol": instrument.symbol,
            "instrument_type": str(instrument.instrument_type),
            "name": instrument.name,
            "currency": instrument.currency,
            "exchange": instrument.exchange,
            "cusip": instrument.cusip,
            "isin": instrument.isin,
            "sector": instrument.sector,
            "asset_class": instrument.asset_class,
            "contract_size": instrument.contract_size,
            "source": instrument.source,
        }
        if instrument.option is not None:
            option = instrument.option
            data |= {
                "option_right": str(option.option_right),
                "strike": option.strike,
                "expiry": option.expiry.isoformat(),
                "multiplier": option.multiplier,
                "exercise_style": str(option.exercise_style),
            }
        if instrument.bond is not None:
            bond = instrument.bond
            data |= {
                "issuer": bond.issuer,
                "coupon_rate": bond.coupon_rate,
                "coupon_frequency": bond.coupon_frequency,
                "maturity_date": bond.maturity_date.isoformat(),
                "day_count": str(bond.day_count),
                "face_value": bond.face_value,
            }

        return CommandResult(
            command="instrument show",
            data=data,
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@app.command()
def sync(
    symbols: Annotated[
        list[str] | None,
        typer.Argument(help="Symbols. Default: every instrument the portfolio has held."),
    ] = None,
) -> None:
    """Compare the local security master with the provider's. Writes nothing.

    Each instrument is looked up as of the last day it was held -- or as of
    --as-of, when it is still held or named here -- under the ticker it
    carried that day. Looked up as of today, a ticker sold years ago can name
    another issuer entirely, or nothing; that is the check ADR 0020 needs
    before a history is priced (R4, R5).

    Nothing is written: the security master is reference data that later
    reports rest on, and a refresh that rewrote it as a side effect would change
    what those reports mean. The differences are listed for a person to act on.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        provider = ctx.provider()
        master = as_security_master(provider)

        warnings: list[str] = []
        if symbols:
            named = [repos.instruments.resolve(s, on=ctx.as_of) for s in symbols]
            on_date = price_history.Span(ctx.as_of, ctx.as_of)
            targets = [
                (
                    instrument,
                    price_history.ticker_spans(repos, instrument, on_date)[0][0],
                    ctx.as_of,
                )
                for instrument in named
            ]
        else:
            targets, replay_warnings = price_history.lookups_when_last_held(
                repos, through=ctx.as_of
            )
            warnings.extend(replay_warnings)

        rows: list[dict[str, object]] = []
        unresolved: list[str] = []
        for instrument, ticker, on in targets:
            try:
                fetched = master.lookup_security(ticker, on=on)
            except DataUnavailableError as exc:
                if exc.code != E_PRICE_MISSING:
                    raise
                unresolved.append(f"{ticker} on {on.isoformat()}: {exc.message}")
                continue
            rows.append(
                {
                    "symbol": instrument.symbol,
                    "looked_up": f"{ticker} on {on.isoformat()}",
                    "provider_type": str(fetched.instrument_type),
                    "provider_active": fetched.is_active,
                    "differences": "; ".join(price_history.differences(instrument, fetched))
                    or None,
                    "provider_ref": fetched.provider_ref,
                }
            )

        if targets and len(unresolved) == len(targets):
            raise DataUnavailableError(
                f"{provider.name} could not place any instrument asked about",
                code=E_PRICE_MISSING,
                remedy="Check the symbols, and that the provider covers them.",
                provider=provider.name,
                problems=unresolved,
            )
        warnings.extend(unresolved)

        return CommandResult(
            command="instrument sync",
            table=Table(
                columns=(
                    Column("symbol", "Symbol"),
                    Column("looked_up", "Looked Up"),
                    Column("provider_type", "Type There"),
                    Column("provider_active", "Active There", ColumnKind.BOOL),
                    Column("differences", "Differences (here -> there)"),
                    Column("provider_ref", "Provider Ref"),
                ),
                rows=tuple(rows),
                title=f"Compared with {provider.name}",
                footnotes=(
                    "Nothing was written. A difference is for a person to resolve: "
                    "the provider is not assumed to be right.",
                ),
            ),
            data={
                "compared": len(rows),
                "with_differences": sum(1 for r in rows if r["differences"]),
                "unresolved": len(unresolved),
            },
            warnings=tuple(warnings),
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)
