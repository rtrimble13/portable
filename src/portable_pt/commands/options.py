"""The options lifecycle: write, exercise, assign, expire, roll.

Where the premium goes when an option resolves into stock is the part that is
routinely got wrong, and getting it wrong is not cosmetic:

* a **written call that is assigned** adds its premium to the *proceeds* of the
  stock sale -- not a separate short-term gain, which would double-count it and
  apply the wrong holding period, because the stock's own period governs;
* a **long call that is exercised** adds its premium to the acquired stock's
  *basis*;
* a **written option that expires worthless** is short-term gain regardless of
  how long it was open.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated

import typer

from portable_core.decimals import money_context, quantize_money
from portable_core.domain.dates import holding_period
from portable_core.domain.enums import (
    FeeClass,
    HoldingPeriod,
    ReliefMethod,
    TransactionSource,
    TransactionType,
)
from portable_core.domain.models import Lot, LotDisposition, Transaction
from portable_core.errors import ValidationError
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.persistence.connection import transaction as db_transaction
from portable_core.services.corporate_actions import CorporateActionEngine
from portable_core.services.lots import LotEngine, parse_lot_selection
from portable_core.services.positions import PositionEngine
from portable_core.services.tax import TaxEngine
from portable_pt import state
from portable_pt.commands._shared import dispatch, maybe_dry_run, money_arg, resolve_date

app = typer.Typer(help="Options lifecycle.", no_args_is_help=True)

ZERO = Decimal("0.00")
CA = CorporateActionEngine()
LOTS = LotEngine()
POSITIONS = PositionEngine()
TAX = TaxEngine()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@app.command()
def expire(
    symbol: Annotated[str, typer.Argument(help="The option's symbol.")],
    account: Annotated[str, typer.Option("--account", "-a")],
    on: Annotated[str | None, typer.Option("--date", "-d")] = None,
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
        instrument = repos.instruments.resolve(symbol, on=when)

        if not instrument.is_option:
            raise ValidationError(
                f"{instrument.symbol} is not an option",
                remedy="Only options expire. Use `pt sell` for a stock position.",
                symbol=instrument.symbol,
            )

        lots = repos.lots.open_lots(target.account_id, instrument.instrument_id)
        if not lots:
            raise ValidationError(
                f"no open position in {instrument.symbol}",
                code="PT-E-LOT-UNMATCHED",
                remedy="`pt position list` shows what is open.",
                symbol=instrument.symbol,
            )

        rows: list[dict[str, object]] = []
        with money_context():
            total_gain = ZERO
            for lot in lots:
                is_written = lot.is_short
                # A written option's basis is negative premium received; a long
                # option's is what was paid. Either way the whole of it
                # resolves here.
                gain = lot.adjusted_cost_basis if is_written else -lot.adjusted_cost_basis
                period = HoldingPeriod.SHORT if is_written else _period(lot, when)
                total_gain += gain
                rows.append(
                    {
                        "lot_id": lot.lot_id,
                        "written": is_written,
                        "quantity": lot.remaining_quantity,
                        "premium": abs(lot.adjusted_cost_basis),
                        "gain": quantize_money(gain),
                        "holding_period": str(period),
                    }
                )

        payload = {
            "symbol": instrument.symbol,
            "account": target.name,
            "date": when.isoformat(),
            "realized_gain": quantize_money(total_gain),
        }
        if ctx.dry_run:
            return maybe_dry_run(
                CommandResult(command="option expire", data=payload, table=_expire_table(rows))
            )

        with db_transaction(repos.con):
            txn = Transaction(
                txn_id=0,
                account_id=target.account_id,
                trade_date=when,
                seq=repos.transactions.next_seq(when),
                txn_type=TransactionType.OPTION_EXPIRATION,
                net_cash_effect=ZERO,
                instrument_id=instrument.instrument_id,
                quantity=sum((lot.remaining_quantity for lot in lots), Decimal(0)),
                note="expired worthless",
                source=TransactionSource.DERIVED,
                created_at=_now(),
            )
            txn_id = repos.transactions.append(txn)

            schedules = repos.accounts.rate_schedules(target.account_id)
            for lot in lots:
                gain = lot.adjusted_cost_basis if lot.is_short else -lot.adjusted_cost_basis
                period = HoldingPeriod.SHORT if lot.is_short else _period(lot, when)
                disposition = LotDisposition(
                    disposition_id=0,
                    lot_id=lot.lot_id,
                    txn_id=txn_id,
                    account_id=target.account_id,
                    instrument_id=instrument.instrument_id,
                    disposition_date=when,
                    quantity=lot.remaining_quantity,
                    proceeds=ZERO,
                    cost_basis_relieved=-gain,
                    realized_gain=quantize_money(gain),
                    holding_period=period,
                    days_held=(when - lot.holding_period_start).days,
                    relief_method=ReliefMethod.SPEC,
                )
                disposition_id = repos.lots.add_disposition(disposition)
                repos.lots.add_realized_gain(
                    TAX.estimate(
                        replace(disposition, disposition_id=disposition_id),
                        target,
                        schedules,
                    )
                )
                repos.lots.update_after_disposition(
                    POSITIONS.apply_disposition(lot, lot.remaining_quantity, when)
                )
            for leg_id in {lot.leg_id for lot in lots}:
                repos.positions.close_leg(leg_id, when)

        return CommandResult(
            command="option expire",
            table=_expire_table(rows),
            data={**payload, "txn_id": txn_id},
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


def _period(lot: Lot, when: date) -> HoldingPeriod:
    """A lot's holding period as at *when*, honouring the short-sale rule."""
    return holding_period(lot.holding_period_start, when, is_short_sale=lot.is_short)


def _expire_table(rows: list[dict[str, object]]) -> Table:
    return Table(
        columns=(
            Column("lot_id", "Lot", ColumnKind.INTEGER),
            Column("written", "Written", ColumnKind.BOOL),
            Column("quantity", "Contracts", ColumnKind.QUANTITY),
            Column("premium", "Premium", ColumnKind.MONEY),
            Column("gain", "Realized", ColumnKind.MONEY),
            Column("holding_period", "Holding"),
        ),
        rows=tuple(rows),
        title="Expiration",
        footnotes=(
            "A written option that expires worthless is SHORT-TERM gain however "
            "long it was open. There is no long-term treatment for a lapse.",
        ),
    )


@app.command()
def assign(
    symbol: Annotated[str, typer.Argument(help="The written option being assigned.")],
    account: Annotated[str, typer.Option("--account", "-a")],
    on: Annotated[str | None, typer.Option("--date", "-d")] = None,
    lots: Annotated[
        str | None, typer.Option("--lots", help="Spec-ID designation for the stock.")
    ] = None,
    method: Annotated[str | None, typer.Option("--method")] = None,
    fees: Annotated[str, typer.Option("--fees")] = "0",
    fee_class: Annotated[str | None, typer.Option("--fee-class")] = None,
) -> None:
    """Record assignment of a written option.

    **The premium flows into the stock sale's proceeds.** It is not a separate
    short-term gain: treating it as one both double-counts the premium and
    applies the wrong holding period, because the stock's own period governs
    the disposition.

    The stock is sold at the strike, and the option's short position closes.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        when = resolve_date(on, ctx)
        target = repos.accounts.resolve(account)
        option = repos.instruments.resolve(symbol, on=when)

        if option.option is None:
            raise ValidationError(
                f"{option.symbol} is not an option",
                remedy="Assignment applies to options.",
            )
        detail = option.option
        underlier = repos.instruments.get(detail.underlier_instrument_id)
        if underlier is None:
            raise ValidationError(
                f"{option.symbol} has no underlier in the security master",
                remedy="Add the underlier with `pt instrument add`.",
            )

        option_lots = repos.lots.open_lots(target.account_id, option.instrument_id)
        if not option_lots:
            raise ValidationError(
                f"no open position in {option.symbol}",
                code="PT-E-LOT-UNMATCHED",
                remedy="`pt position list` shows what is open.",
            )
        if not all(lot.is_short for lot in option_lots):
            raise ValidationError(
                f"{option.symbol} is held long, not written",
                remedy="A long option is *exercised*, not assigned. Use `pt option exercise`.",
            )

        with money_context():
            contracts = sum((lot.remaining_quantity for lot in option_lots), Decimal(0))
            shares = contracts * detail.multiplier
            premium = sum((abs(lot.adjusted_cost_basis) for lot in option_lots), ZERO)
            strike_proceeds = quantize_money(detail.strike * shares)
            # The rule this command exists for.
            proceeds = CA.premium_on_assignment(
                strike_proceeds, premium, money_arg(fees, what="--fees")
            )

        relief = LOTS.select(
            repos.lots.open_lots(target.account_id, underlier.instrument_id),
            shares,
            ReliefMethod(method) if method else target.default_relief_method,
            when,
            selection=parse_lot_selection(lots) if lots else None,
        )

        rows = [
            {
                "lot_id": c.lot.lot_id,
                "quantity": c.quantity,
                "basis": c.cost_basis_relieved,
                "holding_period": str(c.holding_period),
                "days_held": c.days_held,
            }
            for c in relief.consumptions
        ]
        payload = {
            "option": option.symbol,
            "underlier": underlier.symbol,
            "contracts": contracts,
            "shares": shares,
            "strike": detail.strike,
            "strike_proceeds": strike_proceeds,
            "premium": premium,
            "total_proceeds": proceeds,
            "date": when.isoformat(),
        }

        table = Table(
            columns=(
                Column("lot_id", "Lot", ColumnKind.INTEGER),
                Column("quantity", "Shares", ColumnKind.QUANTITY),
                Column("basis", "Basis", ColumnKind.MONEY),
                Column("holding_period", "Holding"),
                Column("days_held", "Days", ColumnKind.INTEGER),
            ),
            rows=tuple(rows),
            title=f"{underlier.symbol} sold at {detail.strike} on assignment",
            footnotes=(
                f"Proceeds are {strike_proceeds} at the strike PLUS {premium} of "
                "premium. The premium is not a separate short-term gain: the stock's "
                "own holding period governs.",
            ),
        )

        if ctx.dry_run:
            return maybe_dry_run(
                CommandResult(command="option assign", table=table, data=payload)
            )

        with db_transaction(repos.con):
            txn_id = repos.transactions.append(
                Transaction(
                    txn_id=0,
                    account_id=target.account_id,
                    trade_date=when,
                    seq=repos.transactions.next_seq(when),
                    txn_type=TransactionType.OPTION_ASSIGNMENT,
                    net_cash_effect=proceeds,
                    instrument_id=underlier.instrument_id,
                    quantity=shares,
                    price=detail.strike,
                    gross_amount=strike_proceeds,
                    fees=quantize_money(money_arg(fees, what="--fees")),
                    fee_class=FeeClass(fee_class) if fee_class else None,
                    note=(f"assigned on {option.symbol}; {premium} premium into proceeds"),
                    source=TransactionSource.DERIVED,
                    created_at=_now(),
                )
            )

            schedules = repos.accounts.rate_schedules(target.account_id)
            for disposition in LOTS.realize(
                relief,
                txn_id=txn_id,
                account_id=target.account_id,
                instrument_id=underlier.instrument_id,
                disposition_date=when,
                gross_proceeds=proceeds,
                fees=ZERO,
            ):
                disposition_id = repos.lots.add_disposition(disposition)
                repos.lots.add_realized_gain(
                    TAX.estimate(
                        replace(disposition, disposition_id=disposition_id),
                        target,
                        schedules,
                    )
                )

            for consumption in relief.consumptions:
                repos.lots.update_after_disposition(
                    POSITIONS.apply_disposition(consumption.lot, consumption.quantity, when)
                )

            # The written option closes with no independent P&L: its premium
            # has become part of the stock's proceeds above.
            for lot in option_lots:
                repos.lots.update_after_disposition(
                    POSITIONS.apply_disposition(lot, lot.remaining_quantity, when)
                )
            for leg_id in {lot.leg_id for lot in option_lots}:
                repos.positions.close_leg(leg_id, when)
            for leg_id in {c.lot.leg_id for c in relief.consumptions}:
                remaining = sum(
                    (
                        lot.remaining_quantity
                        for lot in repos.lots.by_leg(leg_id)
                        if lot.is_open
                    ),
                    Decimal(0),
                )
                if remaining > 0:
                    repos.positions.update_leg_quantity(leg_id, remaining)
                else:
                    repos.positions.close_leg(leg_id, when)

        return CommandResult(
            command="option assign",
            table=table,
            data={**payload, "txn_id": txn_id},
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@app.command()
def exercise(
    symbol: Annotated[str, typer.Argument(help="The long option being exercised.")],
    account: Annotated[str, typer.Option("--account", "-a")],
    on: Annotated[str | None, typer.Option("--date", "-d")] = None,
    fees: Annotated[str, typer.Option("--fees")] = "0",
    fee_class: Annotated[str | None, typer.Option("--fee-class")] = None,
) -> None:
    """Exercise a long option.

    **The premium paid is added to the acquired stock's basis.** The option
    produces no independent P&L: its cost becomes part of what the stock cost,
    and the gain is recognised when the stock is eventually sold.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        when = resolve_date(on, ctx)
        target = repos.accounts.resolve(account)
        option = repos.instruments.resolve(symbol, on=when)

        if option.option is None:
            raise ValidationError(
                f"{option.symbol} is not an option", remedy="Exercise applies to options."
            )
        detail = option.option
        underlier = repos.instruments.get(detail.underlier_instrument_id)
        if underlier is None:
            raise ValidationError(
                f"{option.symbol} has no underlier in the security master",
                remedy="Add the underlier with `pt instrument add`.",
            )

        option_lots = repos.lots.open_lots(target.account_id, option.instrument_id)
        if not option_lots:
            raise ValidationError(
                f"no open position in {option.symbol}",
                code="PT-E-LOT-UNMATCHED",
                remedy="`pt position list` shows what is open.",
            )
        if any(lot.is_short for lot in option_lots):
            raise ValidationError(
                f"{option.symbol} is written, not held long",
                remedy="A written option is *assigned*, not exercised. Use `pt option assign`.",
            )

        with money_context():
            contracts = sum((lot.remaining_quantity for lot in option_lots), Decimal(0))
            shares = contracts * detail.multiplier
            premium = sum((lot.adjusted_cost_basis for lot in option_lots), ZERO)
            strike_cost = quantize_money(detail.strike * shares)
            # The rule this command exists for.
            basis = CA.premium_on_exercise(strike_cost, premium, money_arg(fees, what="--fees"))

        payload = {
            "option": option.symbol,
            "underlier": underlier.symbol,
            "contracts": contracts,
            "shares": shares,
            "strike": detail.strike,
            "strike_cost": strike_cost,
            "premium": premium,
            "stock_basis": basis,
            "date": when.isoformat(),
        }
        if ctx.dry_run:
            return maybe_dry_run(CommandResult(command="option exercise", data=payload))

        with db_transaction(repos.con):
            txn_id = repos.transactions.append(
                Transaction(
                    txn_id=0,
                    account_id=target.account_id,
                    trade_date=when,
                    seq=repos.transactions.next_seq(when),
                    txn_type=TransactionType.BUY,
                    net_cash_effect=-basis,
                    instrument_id=underlier.instrument_id,
                    quantity=shares,
                    price=quantize_money(basis / shares) if shares else ZERO,
                    gross_amount=strike_cost,
                    fees=quantize_money(money_arg(fees, what="--fees")),
                    fee_class=FeeClass(fee_class) if fee_class else None,
                    note=(f"exercised {option.symbol}; {premium} premium into stock basis"),
                    source=TransactionSource.DERIVED,
                    created_at=_now(),
                )
            )
            from portable_core.services.replay import ReplayEngine

            stored = repos.transactions.get(txn_id)
            assert stored is not None
            ReplayEngine(repos).apply_transaction(stored)

            for lot in option_lots:
                repos.lots.update_after_disposition(
                    POSITIONS.apply_disposition(lot, lot.remaining_quantity, when)
                )
            for leg_id in {lot.leg_id for lot in option_lots}:
                repos.positions.close_leg(leg_id, when)

        return CommandResult(
            command="option exercise",
            data={**payload, "txn_id": txn_id},
            warnings=(
                "The option produced no independent P&L: its premium is now part of "
                "the stock's basis, and the gain is recognised when the stock is sold.",
            ),
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)
