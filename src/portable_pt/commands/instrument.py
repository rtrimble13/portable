"""Instrument definition and hydration."""

from __future__ import annotations

from typing import Annotated

import typer

from portable_core.decimals import from_text
from portable_core.domain.enums import DayCount, ExerciseStyle, InstrumentType, OptionRight
from portable_core.domain.models import BondDetail, Instrument, OptionDetail
from portable_core.errors import ValidationError
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.persistence.connection import transaction as db_transaction
from portable_core.providers import as_security_master
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
        list[str] | None, typer.Argument(help="Symbols. Default: all held.")
    ] = None,
) -> None:
    """Hydrate or refresh instruments from the market data provider."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        provider = ctx.provider()
        master = as_security_master(provider)

        wanted = symbols or [i.symbol for i in repos.instruments.all()]
        updated: list[dict[str, object]] = []
        warnings: list[str] = []

        for symbol in wanted:
            try:
                fetched = master.lookup_security(symbol, on=ctx.as_of)
            except Exception as exc:
                warnings.append(f"{symbol}: {exc}")
                continue
            updated.append(
                {
                    "symbol": fetched.symbol,
                    "name": fetched.name,
                    "exchange": fetched.exchange,
                    "provider_ref": fetched.provider_ref,
                }
            )

        if ctx.dry_run:
            return maybe_dry_run(
                CommandResult(
                    command="instrument sync",
                    table=Table(
                        columns=(
                            Column("symbol", "Symbol"),
                            Column("name", "Name"),
                            Column("exchange", "Exchange"),
                            Column("provider_ref", "Provider Ref"),
                        ),
                        rows=tuple(updated),
                        title="Would hydrate",
                    ),
                    warnings=tuple(warnings),
                )
            )

        return CommandResult(
            command="instrument sync",
            table=Table(
                columns=(
                    Column("symbol", "Symbol"),
                    Column("name", "Name"),
                    Column("exchange", "Exchange"),
                    Column("provider_ref", "Provider Ref"),
                ),
                rows=tuple(updated),
                title=f"Hydrated from {provider.name}",
            ),
            warnings=tuple(warnings),
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)
