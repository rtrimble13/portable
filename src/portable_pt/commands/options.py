"""The options lifecycle: write, exercise, assign, expire.

Where the premium goes when an option resolves into stock is the part that is
routinely got wrong, and getting it wrong is not cosmetic:

* a **written call that is assigned** adds its premium to the *proceeds* of the
  stock sale -- not a separate short-term gain, which would double-count it and
  apply the wrong holding period, because the stock's own period governs;
* a **written put that is assigned** deducts its premium from the *basis* of
  the stock bought;
* a **long call that is exercised** adds its premium to the acquired stock's
  *basis*;
* a **written option that expires worthless** is short-term gain regardless of
  how long it was open.

The premium was paid or received when the option was opened. **Cash** at
assignment or exercise is therefore the strike alone, less or plus costs: the
premium is in the tax figures, never in the cash a second time.

Writing an option is `pt short`, and closing one early is `pt cover` or
`pt sell`. Each command here appends one ledger row on the option and derives
its effects through replay, as every trade does (ADR 0038). None writes
derived state itself, so a rebuild reproduces exactly what it recorded.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated

import typer

from portable_core.decimals import money_context, quantize_money
from portable_core.domain.enums import (
    AccountStatus,
    FeeClass,
    OptionRight,
    ReliefMethod,
    TransactionSource,
    TransactionType,
)
from portable_core.domain.models import (
    Account,
    Instrument,
    Lot,
    LotDisposition,
    OptionDetail,
    Transaction,
)
from portable_core.errors import ValidationError
from portable_core.errors.kinds import (
    E_ACCOUNT_CLOSED,
    E_FEE_CLASS_MISSING,
    E_LOT_UNMATCHED,
    E_OPTION_UNSUPPORTED,
)
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.persistence.repositories import Repositories
from portable_core.services.corporate_actions import CorporateActionEngine
from portable_core.services.lots import designate, lots_of_side
from portable_core.services.replay import ReplayEngine
from portable_core.services.trading import TradingService
from portable_pt import state
from portable_pt.commands._shared import (
    RefOpt,
    dispatch,
    money_arg,
    resolve_date,
)

app = typer.Typer(help="Options lifecycle.", no_args_is_help=True)

ZERO = Decimal("0.00")
CA = CorporateActionEngine()

_REBUILT = (
    "this entry is back-dated, so derived state was rebuilt from the ledger. "
    "Figures that depend on lot relief may have changed."
)


# ── shared ───────────────────────────────────────────────────────────────────


def _option(repos: Repositories, symbol: str, when: date) -> tuple[Instrument, OptionDetail]:
    option = repos.instruments.resolve(symbol, on=when)
    if option.option is None:
        raise ValidationError(
            f"{option.symbol} is not an option",
            remedy="Only options expire, are assigned or are exercised. "
            "Use `pt sell` or `pt cover` for a stock position.",
            symbol=option.symbol,
        )
    return option, option.option


def _underlier(repos: Repositories, option: Instrument, detail: OptionDetail) -> Instrument:
    underlier = repos.instruments.get(detail.underlier_instrument_id)
    if underlier is None:  # pragma: no cover -- foreign key prevents it
        raise ValidationError(
            f"{option.symbol} has no underlier in the security master",
            remedy="Add the underlier with `pt instrument add`.",
        )
    return underlier


def _open_side(
    repos: Repositories, account: Account, option: Instrument, *, written: bool, verb: str
) -> list[Lot]:
    """The option lots this event resolves, or a refusal naming what is open."""
    held = repos.lots.open_lots(account.account_id, option.instrument_id)
    lots = lots_of_side(held, short=written)
    if lots:
        return lots
    if held:
        raise ValidationError(
            f"{option.symbol} is held {'long' if written else 'written'} in "
            f"{account.name}, so it cannot be {verb}",
            remedy=(
                "A long option is exercised (`pt option exercise`); a written one "
                "is assigned (`pt option assign`)."
            ),
            symbol=option.symbol,
        )
    raise ValidationError(
        f"no open position in {option.symbol} in {account.name}",
        code=E_LOT_UNMATCHED,
        remedy="`pt position list` shows what is open.",
        symbol=option.symbol,
    )


def _check_account(account: Account, when: date) -> None:
    if account.status is AccountStatus.CLOSED:
        raise ValidationError(
            f"account {account.name!r} is closed",
            code=E_ACCOUNT_CLOSED,
            remedy="A closed account keeps its history but takes no new entries.",
            account=account.name,
        )
    if when < account.opened_date:
        raise ValidationError(
            f"{when.isoformat()} precedes the opening of {account.name!r} on "
            f"{account.opened_date.isoformat()}",
            remedy="Check the date.",
            account=account.name,
        )


def _fee(fees: str, fee_class: str | None) -> tuple[Decimal, FeeClass | None]:
    amount = quantize_money(money_arg(fees, what="--fees"))
    if amount != 0 and fee_class is None:
        raise ValidationError(
            "this entry has fees but no fee classification",
            code=E_FEE_CLASS_MISSING,
            remedy="Pass --fee-class. A brokerage commission is `transaction_cost` "
            "(PORT-GIPS-D01).",
            fees=str(amount),
            choices=[str(f) for f in FeeClass],
        )
    return amount, FeeClass(fee_class) if fee_class else None


def _derive(
    repos: Repositories, txn: Transaction
) -> tuple[int, bool, list[LotDisposition], tuple[str, ...]]:
    """Append *txn* and derive from it as every trade does. ADR 0016, ADR 0038.

    Returns the row's id, whether a rebuild was needed, the dispositions the
    row produced, read back from derived state rather than predicted, and what
    it paired as wash sales (ADR 0037).
    """
    txn_id = repos.transactions.append(txn)
    stored = repos.transactions.get(txn_id)
    assert stored is not None
    engine = ReplayEngine(repos)
    rebuilt = engine.apply_or_rebuild(stored)
    produced = [
        d
        for d in repos.lots.dispositions(
            account_id=txn.account_id, start=txn.trade_date, end=txn.trade_date
        )
        if d.txn_id == txn_id
    ]
    return txn_id, rebuilt, produced, engine.wash.notices_for(txn_id)


def _row(
    repos: Repositories,
    *,
    account: Account,
    when: date,
    txn_type: TransactionType,
    option: Instrument,
    contracts: Decimal,
    net_cash: Decimal,
    note: str,
    ref: str | None,
    strike: Decimal | None = None,
    gross: Decimal | None = None,
    fees: Decimal = ZERO,
    fee_class: FeeClass | None = None,
    lot_selection: str | None = None,
    relief_method: ReliefMethod | None = None,
) -> Transaction:
    TradingService(repos).check_external_ref(account, ref)
    return Transaction(
        txn_id=0,
        account_id=account.account_id,
        trade_date=when,
        seq=repos.transactions.next_seq(when),
        txn_type=txn_type,
        net_cash_effect=net_cash,
        instrument_id=option.instrument_id,
        quantity=contracts,
        price=strike,
        gross_amount=gross,
        fees=fees,
        fee_class=fee_class,
        lot_selection=lot_selection,
        relief_method=relief_method,
        note=note,
        external_ref=ref,
        source=TransactionSource.DERIVED,
        created_at=repos.clock.stamp(),
    )


def _disposition_table(dispositions: list[LotDisposition], title: str, note: str) -> Table:
    return Table(
        columns=(
            Column("lot_id", "Lot", ColumnKind.INTEGER),
            Column("quantity", "Qty", ColumnKind.QUANTITY),
            Column("proceeds", "Proceeds", ColumnKind.MONEY),
            Column("basis", "Basis", ColumnKind.MONEY),
            Column("gain", "Gain/Loss", ColumnKind.MONEY),
            Column("holding_period", "Holding"),
            Column("days_held", "Days", ColumnKind.INTEGER),
        ),
        rows=tuple(
            {
                "lot_id": d.lot_id,
                "quantity": d.quantity,
                "proceeds": d.proceeds,
                "basis": d.cost_basis_relieved,
                "gain": d.realized_gain,
                "holding_period": str(d.holding_period),
                "days_held": d.days_held,
            }
            for d in dispositions
        ),
        title=title,
        footnotes=(note,),
    )


def _premium(lots: list[Lot]) -> Decimal:
    """What the option lots hold: the premium received (written) or paid (long).

    Net of the opening trade's costs, because that is what changed hands.
    """
    with money_context():
        return quantize_money(sum((lot.adjusted_cost_basis for lot in lots), ZERO))


def _contracts(lots: list[Lot]) -> Decimal:
    return sum((lot.remaining_quantity for lot in lots), Decimal(0))


# ── expire ───────────────────────────────────────────────────────────────────


@app.command()
def expire(
    symbol: Annotated[str, typer.Argument(help="The option's symbol.")],
    account: Annotated[str, typer.Option("--account", "-a")],
    on: Annotated[str | None, typer.Option("--date", "-d")] = None,
    ref: RefOpt = None,
) -> None:
    """Record an option expiring worthless.

    A **written** option that lapses is short-term gain of the whole premium,
    **regardless of how long it was open** -- there is no long-term treatment
    for a lapsed written option. A **long** option that lapses is a loss of its
    full premium, and its holding period is the ordinary one.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        when = resolve_date(on, ctx)
        target = repos.accounts.resolve(account)
        _check_account(target, when)
        option, detail = _option(repos, symbol, when)
        if when < detail.expiry:
            raise ValidationError(
                f"{option.symbol} expires on {detail.expiry.isoformat()}, after "
                f"{when.isoformat()}",
                remedy="An option open before its expiry is closed by a trade: "
                "`pt cover` for a written one, `pt sell` for a long one.",
                symbol=option.symbol,
            )
        held = repos.lots.open_lots(target.account_id, option.instrument_id)
        written = any(lot.is_short for lot in held)
        lots = _open_side(repos, target, option, written=written, verb="expired")

        txn = _row(
            repos,
            account=target,
            when=when,
            txn_type=TransactionType.OPTION_EXPIRATION,
            option=option,
            contracts=_contracts(lots),
            net_cash=ZERO,
            note="expired worthless",
            ref=ref,
        )
        with ctx.write():
            txn_id, rebuilt, produced, notices = _derive(repos, txn)

        with money_context():
            realized = quantize_money(sum((d.realized_gain for d in produced), ZERO))
        return CommandResult(
            command="option expire",
            table=_disposition_table(
                produced,
                "Expiration",
                "A written option that expires worthless is SHORT-TERM gain however "
                "long it was open. There is no long-term treatment for a lapse.",
            ),
            data={
                "symbol": option.symbol,
                "account": target.name,
                "written": written,
                "contracts": txn.quantity,
                "premium": _premium(lots),
                "realized_gain": realized,
                "date": when.isoformat(),
                "txn_id": txn_id,
                **({"rebuilt": True} if rebuilt else {}),
            },
            warnings=(*notices, *((_REBUILT,) if rebuilt else ())),
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


# ── assign ───────────────────────────────────────────────────────────────────


@app.command()
def assign(
    symbol: Annotated[str, typer.Argument(help="The written option being assigned.")],
    account: Annotated[str, typer.Option("--account", "-a")],
    on: Annotated[str | None, typer.Option("--date", "-d")] = None,
    lots: Annotated[
        str | None,
        typer.Option("--lots", help="Spec-ID designation of the stock a call delivers."),
    ] = None,
    method: Annotated[str | None, typer.Option("--method")] = None,
    fees: Annotated[str, typer.Option("--fees")] = "0",
    fee_class: Annotated[str | None, typer.Option("--fee-class")] = None,
    ref: RefOpt = None,
) -> None:
    """Record assignment of a written option, on every contract open.

    **A call** delivers stock at the strike. **The premium flows into that
    sale's proceeds**: it is not a separate short-term gain, which would both
    double-count it and apply the wrong holding period, because the stock's own
    period governs.

    **A put** buys stock at the strike, and **the premium reduces that stock's
    basis**. Its holding period starts at the assignment.

    Cash is the strike alone, less costs for a call and plus them for a put:
    the premium was received when the option was written.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        when = resolve_date(on, ctx)
        target = repos.accounts.resolve(account)
        _check_account(target, when)
        option, detail = _option(repos, symbol, when)
        underlier = _underlier(repos, option, detail)
        option_lots = _open_side(repos, target, option, written=True, verb="assigned")
        fee_amount, fee_cls = _fee(fees, fee_class)

        contracts = _contracts(option_lots)
        premium = _premium(option_lots)
        is_call = detail.option_right is OptionRight.CALL
        with money_context():
            shares = contracts * detail.multiplier
            strike_amount = quantize_money(detail.strike * shares)

        stored_selection: str | None = None
        if is_call:
            if lots:
                # ADR 0036: the stock lots, by lineage key.
                stock_lots = lots_of_side(
                    repos.lots.open_lots(target.account_id, underlier.instrument_id),
                    short=False,
                )
                _ids, stored_selection = designate(lots, stock_lots)
            with money_context():
                net_cash = quantize_money(strike_amount - fee_amount)
            # The rule this command exists for.
            stock_figure = CA.premium_on_assignment(strike_amount, premium, fee_amount)
        else:
            if lots:
                raise ValidationError(
                    "--lots designates the stock a call delivers; a put's assignment "
                    "buys stock and delivers none",
                    remedy="Drop --lots.",
                )
            with money_context():
                net_cash = quantize_money(-(strike_amount + fee_amount))
                stock_figure = quantize_money(strike_amount + fee_amount - premium)

        txn = _row(
            repos,
            account=target,
            when=when,
            txn_type=TransactionType.OPTION_ASSIGNMENT,
            option=option,
            contracts=contracts,
            net_cash=net_cash,
            note=(
                f"assigned on {option.symbol}; {premium} premium into "
                + ("the stock's proceeds" if is_call else "the stock's basis")
            ),
            ref=ref,
            strike=detail.strike,
            gross=strike_amount,
            fees=fee_amount,
            fee_class=fee_cls,
            lot_selection=stored_selection,
            relief_method=ReliefMethod(method) if method else None,
        )
        with ctx.write():
            txn_id, rebuilt, produced, notices = _derive(repos, txn)

        payload: dict[str, object] = {
            "option": option.symbol,
            "underlier": underlier.symbol,
            "right": str(detail.option_right),
            "contracts": contracts,
            "shares": shares,
            "strike": detail.strike,
            "strike_proceeds" if is_call else "strike_cost": strike_amount,
            "premium": premium,
            "total_proceeds" if is_call else "stock_basis": stock_figure,
            "net_cash_effect": net_cash,
            "date": when.isoformat(),
            "txn_id": txn_id,
            **({"rebuilt": True} if rebuilt else {}),
        }
        if is_call:
            table: Table | None = _disposition_table(
                [d for d in produced if d.instrument_id == underlier.instrument_id],
                f"{underlier.symbol} sold at {detail.strike} on assignment",
                f"Proceeds are {strike_amount} at the strike PLUS {premium} of premium, "
                "less costs. The premium is not a separate short-term gain: the "
                "stock's own holding period governs.",
            )
        else:
            table = None
        return CommandResult(
            command="option assign",
            table=table,
            data=payload,
            warnings=(*notices, *((_REBUILT,) if rebuilt else ())),
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


# ── exercise ─────────────────────────────────────────────────────────────────


@app.command()
def exercise(
    symbol: Annotated[str, typer.Argument(help="The long option being exercised.")],
    account: Annotated[str, typer.Option("--account", "-a")],
    on: Annotated[str | None, typer.Option("--date", "-d")] = None,
    fees: Annotated[str, typer.Option("--fees")] = "0",
    fee_class: Annotated[str | None, typer.Option("--fee-class")] = None,
    ref: RefOpt = None,
) -> None:
    """Exercise a long call, on every contract open.

    **The premium paid is added to the acquired stock's basis.** The option
    produces no independent P&L: its cost becomes part of what the stock cost,
    and the gain is recognised when the stock is eventually sold. Cash is the
    strike plus costs: the premium was paid when the call was bought.

    A long **put** is refused. Buying a put on stock held a year or less is
    treated as a short sale (IRC §1233(b)), which can restart the stock's
    holding period, and portable does not yet record which case applies.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        when = resolve_date(on, ctx)
        target = repos.accounts.resolve(account)
        _check_account(target, when)
        option, detail = _option(repos, symbol, when)
        underlier = _underlier(repos, option, detail)
        option_lots = _open_side(repos, target, option, written=False, verb="exercised")
        if detail.option_right is OptionRight.PUT:
            raise ValidationError(
                f"exercising the long put {option.symbol} is not supported",
                code=E_OPTION_UNSUPPORTED,
                remedy=(
                    "Buying a put on stock held a year or less is treated as a short "
                    "sale (IRC §1233(b)), which can restart the stock's holding "
                    "period; married puts are excepted. portable does not record "
                    "which applies, so it refuses rather than guess (ADR 0038)."
                ),
                symbol=option.symbol,
            )
        fee_amount, fee_cls = _fee(fees, fee_class)

        contracts = _contracts(option_lots)
        premium = _premium(option_lots)
        with money_context():
            shares = contracts * detail.multiplier
            strike_amount = quantize_money(detail.strike * shares)
            net_cash = quantize_money(-(strike_amount + fee_amount))
        # The rule this command exists for.
        basis = CA.premium_on_exercise(strike_amount, premium, fee_amount)

        txn = _row(
            repos,
            account=target,
            when=when,
            txn_type=TransactionType.OPTION_EXERCISE,
            option=option,
            contracts=contracts,
            net_cash=net_cash,
            note=f"exercised {option.symbol}; {premium} premium into the stock's basis",
            ref=ref,
            strike=detail.strike,
            gross=strike_amount,
            fees=fee_amount,
            fee_class=fee_cls,
        )
        with ctx.write():
            txn_id, rebuilt, _produced, notices = _derive(repos, txn)

        return CommandResult(
            command="option exercise",
            data={
                "option": option.symbol,
                "underlier": underlier.symbol,
                "contracts": contracts,
                "shares": shares,
                "strike": detail.strike,
                "strike_cost": strike_amount,
                "premium": premium,
                "stock_basis": basis,
                "net_cash_effect": net_cash,
                "date": when.isoformat(),
                "txn_id": txn_id,
                **({"rebuilt": True} if rebuilt else {}),
            },
            warnings=(
                "The option produced no independent P&L: its premium is now part of "
                "the stock's basis, and the gain is recognised when the stock is sold.",
                *notices,
                *((_REBUILT,) if rebuilt else ()),
            ),
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)
