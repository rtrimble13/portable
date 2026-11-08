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
from portable_core.providers import as_security_master
from portable_core.services import price_history, restatement, security_master
from portable_pt import state
from portable_pt.commands._shared import dispatch, resolve_date

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
    face: Annotated[
        str, typer.Option("--face", help="A bond's denomination: face per bond.")
    ] = "1000",
    quote_basis: Annotated[
        str,
        typer.Option(
            "--quote-basis",
            help=(
                "percent_of_par (99.50 per 100 of face) | price_per_bond. A bond's "
                "quantity is its face amount either way (ADR 0041)."
            ),
        ),
    ] = "percent_of_par",
    first_coupon: Annotated[
        str | None,
        typer.Option(
            "--first-coupon",
            help=(
                "A bond's first coupon date, when its first period is irregular. "
                "Nothing accrues before it."
            ),
        ),
    ] = None,
    no_market_price: Annotated[
        bool,
        typer.Option(
            "--no-market-price",
            help="Nothing quotes it, ever -- a contingent value right. Valued at zero, "
            "at level 5 (ADR 0028).",
        ),
    ] = False,
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
                first_coupon_date=(
                    resolve_date(first_coupon, ctx, what="--first-coupon")
                    if first_coupon
                    else None
                ),
                quote_basis=_quote_basis(quote_basis),
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
            has_market_price=not no_market_price,
            option=option,
            bond=bond,
        )

        payload = {
            "symbol": instrument.symbol,
            "instrument_type": str(kind),
            "name": name,
            "contract_size": instrument.contract_size,
            "has_market_price": instrument.has_market_price,
        }
        with ctx.write():
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


_QUOTE_BASES = ("percent_of_par", "price_per_bond")


def _quote_basis(value: str) -> str:
    if value not in _QUOTE_BASES:
        raise ValidationError(
            f"unknown quote basis {value!r}",
            remedy="percent_of_par prices per 100 of face; price_per_bond per bond.",
            choices=list(_QUOTE_BASES),
        )
    return value


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
            "has_market_price": instrument.has_market_price,
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
                "quote_basis": bond.quote_basis,
                "first_coupon_date": (
                    bond.first_coupon_date.isoformat() if bond.first_coupon_date else None
                ),
            }

        return CommandResult(
            command="instrument show",
            data=data,
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@app.command(name="market-price")
def market_price(
    symbol: Annotated[str, typer.Argument()],
    none: Annotated[
        bool, typer.Option("--none", help="Nothing quotes it, and nothing ever will.")
    ] = False,
    quoted: Annotated[bool, typer.Option("--quoted", help="Undo --none.")] = False,
    restate: Annotated[
        bool,
        typer.Option("--restate", help="Restate the stored snapshots that hold it."),
    ] = False,
) -> None:
    """Mark an instrument as having no observable market price, or undo it.

    ADR 0028. A holding nothing quotes -- a contingent value right -- is valued
    at zero as a level-5 (subjective, unobservable) valuation, recorded in the
    snapshot with that provenance, and the snapshot holding it is complete.
    Without this, one such holding left every snapshot of its account
    incomplete, and an incomplete snapshot is never used for a return.

    Refused for an instrument that has ever had a price: the mark applies to
    every date. A stored snapshot it changes is refused with
    PT-E-RESTATES-HISTORY unless --restate (ADR 0025).
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        if none == quoted:
            raise ValidationError(
                "say --none or --quoted",
                remedy="`pt instrument market-price 003CVR016 --none` marks one.",
            )
        instrument = repos.instruments.resolve(symbol, on=ctx.as_of)

        with ctx.write():
            recorded = restatement.record_market_price(
                repos, instrument, has_market_price=quoted, restate=restate
            )

        return CommandResult(
            command="instrument market-price",
            data={
                "symbol": instrument.symbol,
                "has_market_price": quoted,
                "restated": bool(recorded.restated),
            },
            warnings=recorded.restated,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@app.command(name="equivalent")
def equivalent(
    first: Annotated[str, typer.Argument(help="One instrument.")],
    second: Annotated[str, typer.Argument(help="The other.")],
    reason: Annotated[
        str, typer.Option("--reason", help="Why they are substantially identical.")
    ],
    restate: Annotated[
        bool, typer.Option("--restate", help="Restate the recorded gains it changes.")
    ] = False,
) -> None:
    """Declare two instruments substantially identical for wash sales.

    ADR 0037. `portable` never infers it: whether two securities are
    substantially identical is a judgement, and a wrong guess silently allows
    or disallows a loss. A purchase of either is then matched against a loss on
    the other, in every account.

    The declaration applies to every past and future pairing. One that changes
    a recorded gain is refused with PT-E-RESTATES-HISTORY unless --restate
    (ADR 0025). `pt tax wash-candidates` lists the pairs worth considering.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        one = repos.instruments.resolve(first, on=ctx.as_of)
        other = repos.instruments.resolve(second, on=ctx.as_of)

        with ctx.write():
            recorded = restatement.record_equivalence(
                repos, one, other, reason=reason, restate=restate
            )

        return CommandResult(
            command="instrument equivalent",
            data={
                "instruments": [one.symbol, other.symbol],
                "reason": reason,
                "restated": bool(recorded.restated),
            },
            warnings=recorded.restated,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@app.command(name="equivalents")
def equivalents() -> None:
    """Every pair declared substantially identical, with its reason (ADR 0037)."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        rows: list[dict[str, object]] = []
        for pair in repos.equivalences.all():
            a = repos.instruments.get(pair.instrument_a_id)
            b = repos.instruments.get(pair.instrument_b_id)
            rows.append(
                {
                    "first": a.symbol if a else None,
                    "second": b.symbol if b else None,
                    "reason": pair.reason,
                    "declared": pair.created_at,
                }
            )
        return CommandResult(
            command="instrument equivalents",
            table=Table(
                columns=(
                    Column("first", "Instrument"),
                    Column("second", "Substantially identical to"),
                    Column("reason", "Reason"),
                    Column("declared", "Declared"),
                ),
                rows=tuple(rows),
                title="Declared substantially identical",
            ),
            data={"pairs": len(rows)},
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@app.command(name="correct-symbol")
def correct_symbol(
    symbol: Annotated[str, typer.Argument(help="The symbol recorded in error.")],
    to: Annotated[str, typer.Option("--to", help="The symbol the instrument carried.")],
    reason: Annotated[
        str,
        typer.Option(
            "--reason",
            help="What shows it -- e.g. the custodian's statements naming the other ticker.",
        ),
    ],
) -> None:
    """Correct a symbol recorded in error, over every period that carried it.

    ADR 0034. For a security the master named wrongly -- a crosswalk that
    mapped a fund's description to another share class's ticker -- not for a
    rename. A rename happened to the security on a date, and `pt ca
    symbol-change` records it as such; a correction says the old symbol was
    never the security's at all, so no period is opened or closed and no
    ledger row is written. The change is logged with its reason instead.

    Refused when another instrument carries the new symbol over an
    overlapping period, and when the instrument has any stored price: each
    was found under the wrong symbol and may be another security's. Correct,
    then price. Ledger notes written before keep the text they were written
    with.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        old = symbol.strip().upper()
        instrument = repos.instruments.resolve(old, on=ctx.as_of)

        with ctx.write():
            corrected = security_master.correct_symbol(
                repos, instrument, to, old_symbol=old, reason=reason
            )

        return CommandResult(
            command="instrument correct-symbol",
            data={
                "instrument_id": corrected.instrument_id,
                "from": corrected.old,
                "to": corrected.new,
                "periods": [
                    {
                        "valid_from": valid_from.isoformat(),
                        "valid_to": valid_to.isoformat() if valid_to else None,
                    }
                    for valid_from, valid_to in corrected.periods
                ],
                "reason": reason.strip(),
            },
            warnings=(
                f"Re-run `pt instrument sync {corrected.new}` to compare the corrected "
                "instrument with the provider.",
            ),
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

        provider.prefetch(ticker for _, ticker, _ in targets)
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
