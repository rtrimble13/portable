"""Positions and lots."""

from __future__ import annotations

from typing import Annotated

import typer

from portable_core.domain.enums import StrategyType
from portable_core.errors import ValidationError
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.persistence.connection import transaction as db_transaction
from portable_core.services.positions import PositionEngine
from portable_pt import state
from portable_pt.commands._shared import dispatch, maybe_dry_run

position_app = typer.Typer(help="Positions.", no_args_is_help=True)
lot_app = typer.Typer(help="Tax lots.", no_args_is_help=True)


@position_app.command(name="list")
def list_positions(
    account: Annotated[str | None, typer.Option("--account", "-a")] = None,
    open_only: Annotated[bool, typer.Option("--open-only")] = True,
) -> None:
    """List positions, with their legs."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        account_id = repos.accounts.resolve(account).account_id if account else None
        positions = repos.positions.all(account_id=account_id, open_only=open_only)

        rows: list[dict[str, object]] = []
        for position in positions:
            for leg in position.legs:
                instrument = repos.instruments.get(leg.instrument_id)
                rows.append(
                    {
                        "position_id": position.position_id,
                        "strategy": str(position.strategy_type),
                        "symbol": instrument.symbol if instrument else None,
                        "role": str(leg.role),
                        "quantity": leg.quantity,
                        "opened": position.opened_date.isoformat(),
                        "status": str(leg.status),
                    }
                )

        return CommandResult(
            command="position list",
            table=Table(
                columns=(
                    Column("position_id", "Position", ColumnKind.INTEGER),
                    Column("strategy", "Strategy"),
                    Column("symbol", "Symbol"),
                    Column("role", "Role"),
                    Column("quantity", "Qty", ColumnKind.QUANTITY),
                    Column("opened", "Opened", ColumnKind.DATE),
                    Column("status", "Status"),
                ),
                rows=tuple(rows),
                title="Positions",
                footnotes=(
                    "A position may span several instruments -- a covered call is one "
                    "position, not two. One row per leg.",
                ),
            ),
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@position_app.command(name="show")
def show_position(position_id: Annotated[int, typer.Argument()]) -> None:
    """Show one position: its legs and every open lot beneath them."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        position = repos.positions.get(position_id)
        if position is None:
            raise ValidationError(
                f"no position {position_id}",
                remedy="`pt position list` shows them.",
                position_id=position_id,
            )

        rows: list[dict[str, object]] = []
        for leg in position.legs:
            instrument = repos.instruments.get(leg.instrument_id)
            for lot in repos.lots.by_leg(leg.leg_id):
                rows.append(
                    {
                        "leg_id": leg.leg_id,
                        "symbol": instrument.symbol if instrument else None,
                        "role": str(leg.role),
                        "lot_id": lot.lot_id,
                        "open_date": lot.open_date.isoformat(),
                        "holding_period_start": lot.holding_period_start.isoformat(),
                        "quantity": lot.remaining_quantity,
                        "basis": lot.adjusted_cost_basis,
                        "per_unit": lot.basis_per_unit,
                    }
                )

        return CommandResult(
            command="position show",
            table=Table(
                columns=(
                    Column("leg_id", "Leg", ColumnKind.INTEGER),
                    Column("symbol", "Symbol"),
                    Column("role", "Role"),
                    Column("lot_id", "Lot", ColumnKind.INTEGER),
                    Column("open_date", "Acquired", ColumnKind.DATE),
                    Column("holding_period_start", "HP Start", ColumnKind.DATE),
                    Column("quantity", "Qty", ColumnKind.QUANTITY),
                    Column("basis", "Basis", ColumnKind.MONEY),
                    Column("per_unit", "Basis/Unit", ColumnKind.PRICE),
                ),
                rows=tuple(rows),
                title=f"Position {position_id} ({position.strategy_type})",
                footnotes=(
                    "HP Start is the holding-period start, which a split does not "
                    "reset and which a spinoff's new shares inherit. It is often not "
                    "the acquisition date.",
                ),
            ),
            data={
                "position_id": position.position_id,
                "account_id": position.account_id,
                "strategy_type": str(position.strategy_type),
                "opened_date": position.opened_date.isoformat(),
                "status": str(position.status),
                "legs": len(position.legs),
                "label": position.label,
            },
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@position_app.command()
def group(
    legs: Annotated[list[int], typer.Argument(help="Leg ids to move.")],
    into: Annotated[int, typer.Option("--into", help="Target position id.")],
    strategy: Annotated[
        str | None,
        typer.Option("--strategy", help="Restate the strategy: covered_call, collar, ..."),
    ] = None,
) -> None:
    """Move legs into one position, restating the trader's intent.

    This is what happens when a long stock holding becomes a covered call
    because a call was written against it. Because lots hang off legs and legs
    carry the position id, regrouping updates one column and touches no lot and
    no basis figure -- **a change of intent does not change tax history**
    (ADR 0009).
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        target = repos.positions.get(into)
        if target is None:
            raise ValidationError(
                f"no position {into}", remedy="`pt position list` shows them."
            )

        payload = {
            "legs": legs,
            "into": into,
            "strategy": strategy or str(target.strategy_type),
        }
        if ctx.dry_run:
            return maybe_dry_run(CommandResult(command="position group", data=payload))

        with db_transaction(repos.con):
            for leg_id in legs:
                repos.positions.move_leg(leg_id, into)
            if strategy:
                repos.positions.set_strategy(into, str(StrategyType(strategy)))

        refreshed = repos.positions.get(into)
        inferred = PositionEngine.infer_strategy(list(refreshed.legs)) if refreshed else None
        return CommandResult(
            command="position group",
            data={**payload, "inferred_strategy": str(inferred) if inferred else None},
            warnings=(
                "No lot and no basis figure was touched. A change of intent does not "
                "change tax history.",
            ),
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@lot_app.command(name="list")
def list_lots(
    symbol: Annotated[str | None, typer.Argument(help="Filter to one instrument.")] = None,
    account: Annotated[str | None, typer.Option("--account", "-a")] = None,
    open_only: Annotated[bool, typer.Option("--open-only")] = True,
) -> None:
    """List tax lots, oldest first, with holding period as of --as-of.

    This is the table to read before choosing a spec-ID designation: it shows
    which lots are already long-term, and what each would realise.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        from portable_core.domain.dates import days_between, holding_period

        rows: list[dict[str, object]] = []
        accounts = [repos.accounts.resolve(account)] if account else repos.accounts.all()
        for target in accounts:
            instruments = (
                [repos.instruments.resolve(symbol, on=ctx.as_of)]
                if symbol
                else repos.instruments.all()
            )
            for instrument in instruments:
                for lot in repos.lots.open_lots(
                    target.account_id, instrument.instrument_id, as_of=ctx.as_of
                ):
                    if open_only and not lot.is_open:
                        continue
                    period = holding_period(
                        lot.holding_period_start, ctx.as_of, is_short_sale=lot.is_short
                    )
                    rows.append(
                        {
                            "lot_id": lot.lot_id,
                            "account": target.name,
                            "symbol": instrument.symbol,
                            "open_date": lot.open_date.isoformat(),
                            "quantity": lot.remaining_quantity,
                            "basis": lot.adjusted_cost_basis,
                            "per_unit": lot.basis_per_unit,
                            "holding_period": str(period),
                            "days_held": days_between(lot.holding_period_start, ctx.as_of),
                        }
                    )

        return CommandResult(
            command="lot list",
            table=Table(
                columns=(
                    Column("lot_id", "Lot", ColumnKind.INTEGER),
                    Column("account", "Account"),
                    Column("symbol", "Symbol"),
                    Column("open_date", "Acquired", ColumnKind.DATE),
                    Column("quantity", "Qty", ColumnKind.QUANTITY),
                    Column("basis", "Basis", ColumnKind.MONEY),
                    Column("per_unit", "Basis/Unit", ColumnKind.PRICE),
                    Column("holding_period", "Holding"),
                    Column("days_held", "Days", ColumnKind.INTEGER),
                ),
                rows=tuple(rows),
                title=f"Open lots as of {ctx.as_of.isoformat()}",
                footnotes=(
                    "Holding period is as of --as-of. Long-term requires MORE than "
                    "one year from the day after acquisition; exactly one year is "
                    "short-term.",
                    "Designate lots on a sale with --lots 'lot_id:qty;lot_id:qty'.",
                ),
            ),
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@lot_app.command(name="show")
def show_lot(lot_id: Annotated[int, typer.Argument()]) -> None:
    """Show a lot and every adjustment ever made to its basis.

    The adjustment log is the difference between a basis you can defend and one
    you can only assert.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        lot = repos.lots.get(lot_id)
        if lot is None:
            raise ValidationError(f"no lot {lot_id}", remedy="`pt lot list` shows them.")

        instrument = repos.instruments.get(lot.instrument_id)
        return CommandResult(
            command="lot show",
            table=Table(
                columns=(
                    Column("adjustment_date", "Date", ColumnKind.DATE),
                    Column("reason", "Reason"),
                    Column("basis_delta", "Basis Δ", ColumnKind.MONEY),
                    Column("quantity_delta", "Qty Δ", ColumnKind.QUANTITY),
                    Column("holding_period_start_after", "HP Start After", ColumnKind.DATE),
                    Column("note", "Note"),
                ),
                rows=tuple(
                    {
                        "adjustment_date": a.adjustment_date.isoformat(),
                        "reason": str(a.reason),
                        "basis_delta": a.basis_delta,
                        "quantity_delta": a.quantity_delta,
                        "holding_period_start_after": (
                            a.holding_period_start_after.isoformat()
                            if a.holding_period_start_after
                            else None
                        ),
                        "note": a.note,
                    }
                    for a in lot.adjustments
                ),
                title=f"Basis adjustments for lot {lot_id}",
            ),
            data={
                "lot_id": lot.lot_id,
                "symbol": instrument.symbol if instrument else None,
                "open_date": lot.open_date.isoformat(),
                "holding_period_start": lot.holding_period_start.isoformat(),
                "original_quantity": lot.original_quantity,
                "remaining_quantity": lot.remaining_quantity,
                "original_cost_basis": lot.original_cost_basis,
                "adjusted_cost_basis": lot.adjusted_cost_basis,
                "basis_per_unit": lot.basis_per_unit,
                "is_short": lot.is_short,
                "status": str(lot.status),
            },
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)
