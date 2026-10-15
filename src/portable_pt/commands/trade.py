"""Trading: buy, sell, short, cover, and the ledger's correction path."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated

import typer

from portable_core.domain.enums import (
    FeeClass,
    ReliefMethod,
    TransactionSource,
    TransactionType,
)
from portable_core.domain.models import Instrument, Transaction
from portable_core.errors import ValidationError
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.persistence.connection import transaction as db_transaction
from portable_core.persistence.repositories import Repositories
from portable_core.services.regrouping import Regroup, record_regroup
from portable_core.services.replay import ReplayEngine, state_on
from portable_core.services.trading import TradeIntent, TradePlan, TradingService
from portable_pt import state
from portable_pt.commands._shared import (
    RefOpt,
    dispatch,
    maybe_dry_run,
    money_arg,
    resolve_date,
)

app = typer.Typer(help="Trade listing, reversal, and correction.", no_args_is_help=True)

FeesOpt = Annotated[str, typer.Option("--fees", help="Total fees on this trade.")]
CommissionOpt = Annotated[str, typer.Option("--commission", help="Broker commission.")]
FeeClassOpt = Annotated[
    str | None,
    typer.Option(
        "--fee-class",
        help=(
            "transaction_cost | embedded_fund_fee | external_mgmt_fee | "
            "internal_mgmt_cost | other_admin. Required whenever a fee is present "
            "(PORT-GIPS-D01)."
        ),
    ),
]
MethodOpt = Annotated[
    str | None,
    typer.Option("--method", help="Relief method: spec, fifo, lifo, hifo, lofo, avg."),
]
LotsOpt = Annotated[
    str | None,
    typer.Option("--lots", help="Spec-ID designation, e.g. '12:100;15:50'."),
]


def _trade(
    txn_type: TransactionType,
    symbol: str,
    *,
    account: str,
    qty: str,
    price: str,
    date_text: str | None,
    fees: str,
    commission: str,
    fee_class: str | None,
    method: str | None,
    lots: str | None,
    position: int | None,
    new_position: bool,
    note: str | None,
    ref: str | None,
    settlement: str | None,
) -> None:
    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        trade_date = resolve_date(date_text, ctx)

        found_account = repos.accounts.resolve(account)
        instrument = repos.instruments.resolve(symbol, on=trade_date)

        if position is not None and new_position:
            raise ValidationError(
                "--position and --new-position are mutually exclusive",
                remedy="Name a position, or ask for a new one -- not both.",
            )
        into = _position_intent(
            repos, txn_type, found_account.account_id, instrument, trade_date, position,
            new_position,
        )  # fmt: skip

        service = TradingService(repos)
        plan = service.plan(
            TradeIntent(
                account=found_account,
                instrument=instrument,
                txn_type=txn_type,
                quantity=money_arg(qty, what="--qty"),
                price=money_arg(price, what="--price"),
                trade_date=trade_date,
                fees=money_arg(fees, what="--fees"),
                commissions=money_arg(commission, what="--commission"),
                fee_class=FeeClass(fee_class) if fee_class else None,
                relief_method=ReliefMethod(method) if method else None,
                lot_selection=lots,
                settlement_date=resolve_date(settlement, ctx, what="--settlement")
                if settlement
                else None,
                note=note,
                external_ref=ref,
            )
        )

        result = _plan_result(txn_type, symbol, plan, ctx.portfolio_name())
        if ctx.dry_run:
            return maybe_dry_run(result)

        with db_transaction(repos.con):
            committed = service.commit(plan)
            if into is not None:
                # ADR 0023: joining a position is a regroup recorded beside the
                # trade, in the same transaction -- so both land or neither does.
                _join(repos, committed.transaction, into)

        from dataclasses import replace

        payload: dict[str, object] = {
            **result.data,
            "txn_id": committed.transaction.txn_id,
        }
        warnings = result.warnings
        if committed.rebuilt:
            # A back-dated entry sorts before rows already applied, so derived
            # state was replayed from the ledger rather than extended. Worth
            # saying: it can change a realized gain already reported (ADR 0016).
            payload["rebuilt"] = True
            warnings = (
                *warnings,
                "this entry is back-dated, so derived state was rebuilt from the "
                "ledger. Figures that depend on lot relief may have changed.",
            )
        return replace(result, data=payload, warnings=warnings)

    dispatch(action)


def _position_intent(
    repos: Repositories,
    txn_type: TransactionType,
    account_id: int,
    instrument: Instrument,
    on: date,
    position: int | None,
    new_position: bool,
) -> int | None:
    """What --position / --new-position ask for, checked before anything is written.

    Returns the opening transaction of the position to join, or ``None``.
    Resolved here, before the trade commits: a back-dated trade rebuilds, and
    a rebuild may renumber the position the user named.
    """
    held = state_on(
        repos, on, lambda: repos.positions.leg_for(account_id, instrument.instrument_id)
    ).results[0]
    if new_position and held is not None:
        raise ValidationError(
            f"--new-position: the account already holds {instrument.symbol} in position "
            f"{held.position_id} on {on.isoformat()}",
            remedy=(
                "One account's holding of one instrument is one leg in one position "
                "(ADR 0009); a second position in it is not modelled. Group the holding "
                "with `pt position group` instead."
            ),
        )
    if position is None:
        return None
    if txn_type not in (TransactionType.BUY, TransactionType.SELL_SHORT):
        raise ValidationError(
            "--position applies to a trade that opens or adds to a holding",
            remedy="A closing trade reduces the leg wherever it is grouped.",
        )
    target = repos.positions.get(position)
    if target is None or target.opened_txn_id is None or target.account_id != account_id:
        raise ValidationError(
            f"no position {position} in this account",
            remedy="`pt position list` shows them; a position holds legs in one account.",
        )
    if held is not None and held.position_id != position:
        raise ValidationError(
            f"{instrument.symbol} is already held in position {held.position_id}, and this "
            "trade adds to that holding",
            remedy="Move the holding with `pt position group`, then trade.",
        )
    return target.opened_txn_id


def _join(repos: Repositories, trade: Transaction, into: int) -> None:
    """Record that the leg *trade* opened belongs to the position *into* opened."""
    assert trade.instrument_id is not None
    record_regroup(
        repos,
        Regroup(
            account_id=trade.account_id,
            instrument_id=trade.instrument_id,
            on=trade.trade_date,
            into_opened_by=into,
        ),
    )


def _plan_result(
    txn_type: TransactionType, symbol: str, plan: TradePlan, portfolio: str | None
) -> CommandResult:
    table = None
    if plan.relief_plan is not None:
        table = Table(
            columns=(
                Column("lot_id", "Lot", ColumnKind.INTEGER),
                Column("open_date", "Acquired", ColumnKind.DATE),
                Column("quantity", "Qty", ColumnKind.QUANTITY),
                Column("basis", "Basis Relieved", ColumnKind.MONEY),
                Column("holding_period", "Holding", ColumnKind.TEXT),
                Column("days_held", "Days", ColumnKind.INTEGER),
            ),
            rows=tuple(
                {
                    "lot_id": c.lot.lot_id,
                    "open_date": c.lot.open_date.isoformat(),
                    "quantity": c.quantity,
                    "basis": c.cost_basis_relieved,
                    "holding_period": str(c.holding_period),
                    "days_held": c.days_held,
                }
                for c in plan.relief_plan.consumptions
            ),
            title=f"Lots consumed ({plan.relief_plan.method})",
            footnotes=(
                "Long-term requires more than one year from the day after "
                "acquisition; exactly one year is short-term.",
            ),
        )

    return CommandResult(
        command=str(txn_type),
        table=table,
        data={
            "symbol": symbol,
            "quantity": plan.intent.quantity,
            "price": plan.intent.price,
            "gross_amount": plan.gross_amount,
            "fees": plan.intent.fees,
            "commissions": plan.intent.commissions,
            "net_cash_effect": plan.net_cash_effect,
            "trade_date": plan.intent.trade_date.isoformat(),
            "account": plan.intent.account.name,
            **(
                {
                    "cost_basis_relieved": plan.relief_plan.total_basis,
                    "relief_method": str(plan.relief_plan.method),
                }
                if plan.relief_plan
                else {}
            ),
        },
        warnings=plan.warnings,
        portfolio=portfolio,
    )


def make_trade_command(txn_type: TransactionType, help_text: str) -> Callable[..., None]:
    """Build one of the four trade commands.

    They differ only in the transaction type they record, so they are generated
    rather than copied -- four near-identical bodies is four places for a flag
    to be forgotten.
    """

    def command(
        symbol: Annotated[str, typer.Argument(help="Ticker.")],
        account: Annotated[str, typer.Option("--account", "-a")],
        qty: Annotated[str, typer.Option("--qty", help="Quantity. Always positive.")],
        price: Annotated[str, typer.Option("--price", help="Per-unit price.")],
        date_text: Annotated[
            str | None, typer.Option("--date", "-d", help="Trade date (YYYY-MM-DD).")
        ] = None,
        fees: FeesOpt = "0",
        commission: CommissionOpt = "0",
        fee_class: FeeClassOpt = None,
        method: MethodOpt = None,
        lots: LotsOpt = None,
        position: Annotated[int | None, typer.Option("--position")] = None,
        new_position: Annotated[bool, typer.Option("--new-position")] = False,
        note: Annotated[str | None, typer.Option("--note")] = None,
        ref: RefOpt = None,
        settlement: Annotated[str | None, typer.Option("--settlement")] = None,
    ) -> None:
        _trade(
            txn_type,
            symbol,
            account=account,
            qty=qty,
            price=price,
            date_text=date_text,
            fees=fees,
            commission=commission,
            fee_class=fee_class,
            method=method,
            lots=lots,
            position=position,
            new_position=new_position,
            note=note,
            ref=ref,
            settlement=settlement,
        )

    command.__doc__ = help_text
    return command


@app.command(name="list")
def list_trades(
    account: Annotated[str | None, typer.Option("--account", "-a")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
) -> None:
    """List ledger entries in replay order."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        account_id = repos.accounts.resolve(account).account_id if account else None
        rows = repos.transactions.in_ledger_order(until=ctx.as_of, account_id=account_id)

        return CommandResult(
            command="trade list",
            table=Table(
                columns=(
                    Column("txn_id", "Id", ColumnKind.INTEGER),
                    Column("trade_date", "Date", ColumnKind.DATE),
                    Column("txn_type", "Type", ColumnKind.TEXT),
                    Column("symbol", "Symbol", ColumnKind.TEXT),
                    Column("quantity", "Qty", ColumnKind.QUANTITY),
                    Column("price", "Price", ColumnKind.PRICE),
                    Column("net_cash_effect", "Cash", ColumnKind.MONEY),
                    Column("note", "Note", ColumnKind.TEXT),
                ),
                rows=tuple(
                    {
                        "txn_id": t.txn_id,
                        "trade_date": t.trade_date.isoformat(),
                        "txn_type": str(t.txn_type),
                        "symbol": _symbol(repos, t.instrument_id),
                        "quantity": t.quantity,
                        "price": t.price,
                        "net_cash_effect": t.net_cash_effect,
                        "note": t.note,
                    }
                    for t in rows[-limit:]
                ),
                title="Ledger",
                footnotes=(
                    f"Showing {min(limit, len(rows))} of {len(rows)} entries, "
                    "in replay order (trade_date, seq, txn_id).",
                ),
            ),
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


def _symbol(repos: object, instrument_id: int | None) -> str | None:
    if instrument_id is None:
        return None
    instrument = repos.instruments.get(instrument_id)  # type: ignore[attr-defined]
    return instrument.symbol if instrument else None


@app.command()
def reverse(
    txn_id: Annotated[int, typer.Argument(help="The transaction to reverse.")],
    note: Annotated[str | None, typer.Option("--note", help="Why.")] = None,
    on: Annotated[str | None, typer.Option("--date", "-d")] = None,
    ref: RefOpt = None,
) -> None:
    """Reverse a transaction with a new, opposite ledger entry.

    The ledger is append-only, so a mistake is corrected with a reversing entry
    plus a new correct entry -- never by editing history. All three stay
    visible, which is what makes the trail defensible (CLAUDE.md invariant 2).
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        original = repos.transactions.get(txn_id)
        if original is None:
            raise ValidationError(
                f"no transaction {txn_id}",
                remedy="`pt trade list` shows the ledger.",
                txn_id=txn_id,
            )
        if original.txn_type is TransactionType.REVERSAL:
            raise ValidationError(
                f"transaction {txn_id} is itself a reversal",
                remedy=(
                    "Reversing a reversal would re-apply the original. Record the "
                    "intended entry directly instead."
                ),
                txn_id=txn_id,
            )

        reversal_date = resolve_date(on, ctx)
        reversal = Transaction(
            txn_id=0,
            account_id=original.account_id,
            trade_date=reversal_date,
            seq=repos.transactions.next_seq(reversal_date),
            txn_type=TransactionType.REVERSAL,
            net_cash_effect=-original.net_cash_effect,
            instrument_id=original.instrument_id,
            quantity=original.quantity,
            price=original.price,
            gross_amount=(
                -original.gross_amount if original.gross_amount is not None else None
            ),
            reverses_txn_id=txn_id,
            note=note or f"reverses txn {txn_id}",
            external_ref=ref,
            source=TransactionSource.DERIVED,
            created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        payload = {
            "reverses_txn_id": txn_id,
            "original_type": str(original.txn_type),
            "original_cash_effect": original.net_cash_effect,
            "reversal_cash_effect": reversal.net_cash_effect,
            "trade_date": reversal_date.isoformat(),
        }
        if ctx.dry_run:
            return maybe_dry_run(CommandResult(command="trade reverse", data=payload))

        with db_transaction(repos.con):
            new_id = repos.transactions.append(reversal)
            # A reversal changes which lots later trades should have consumed,
            # so derived state is rebuilt rather than patched. The ledger is
            # unchanged and remains the record of what happened.
            ReplayEngine(repos).rebuild()

        return CommandResult(
            command="trade reverse",
            data={**payload, "txn_id": new_id},
            warnings=(
                "Derived state was rebuilt from the ledger, because a reversal "
                "changes which lots later trades consume.",
            ),
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@app.command()
def show(txn_id: Annotated[int, typer.Argument()]) -> None:
    """Show one ledger entry and everything derived from it."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        txn = repos.transactions.get(txn_id)
        if txn is None:
            raise ValidationError(
                f"no transaction {txn_id}", remedy="`pt trade list` shows the ledger."
            )

        dispositions = [d for d in repos.lots.dispositions() if d.txn_id == txn_id]
        return CommandResult(
            command="trade show",
            table=Table(
                columns=(
                    Column("lot_id", "Lot", ColumnKind.INTEGER),
                    Column("quantity", "Qty", ColumnKind.QUANTITY),
                    Column("proceeds", "Proceeds", ColumnKind.MONEY),
                    Column("cost_basis_relieved", "Basis", ColumnKind.MONEY),
                    Column("realized_gain", "Gain", ColumnKind.MONEY),
                    Column("holding_period", "Holding", ColumnKind.TEXT),
                ),
                rows=tuple(
                    {
                        "lot_id": d.lot_id,
                        "quantity": d.quantity,
                        "proceeds": d.proceeds,
                        "cost_basis_relieved": d.cost_basis_relieved,
                        "realized_gain": d.realized_gain,
                        "holding_period": str(d.holding_period),
                    }
                    for d in dispositions
                ),
                title=f"Dispositions from transaction {txn_id}",
            ),
            data={
                "txn_id": txn.txn_id,
                "account_id": txn.account_id,
                "trade_date": txn.trade_date.isoformat(),
                "settlement_date": (
                    txn.settlement_date.isoformat() if txn.settlement_date else None
                ),
                "txn_type": str(txn.txn_type),
                "symbol": _symbol(repos, txn.instrument_id),
                "quantity": txn.quantity,
                "price": txn.price,
                "gross_amount": txn.gross_amount,
                "fees": txn.fees,
                "commissions": txn.commissions,
                "fee_class": str(txn.fee_class) if txn.fee_class else None,
                "net_cash_effect": txn.net_cash_effect,
                # Withholding is tax, not a fee, so it is reported beside the
                # gross rather than inside `fees`: the return is earned on the
                # gross and the cash balance moved by the net (PORT-GIPS-A06).
                "taxes_withheld": txn.taxes_withheld,
                "withholding_reclaimable": txn.withholding_reclaimable,
                "external_ref": txn.external_ref,
                # Where the row came from, so a figure can be traced to the
                # document that produced it (PORT-GIPS-J03).
                "source": str(txn.source),
                "reverses_txn_id": txn.reverses_txn_id,
                "note": txn.note,
                "created_at": txn.created_at,
            },
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)
