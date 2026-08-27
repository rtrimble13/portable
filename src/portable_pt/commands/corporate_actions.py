"""Corporate actions: split, spinoff, merger, symbol change, delist, sync.

Every command here writes a ledger entry **and** the basis adjustments it
implies, so that the effect is reproducible by replay and explainable
afterwards from each lot's adjustment log.

The traps these exist to get right change a tax *rate*, not a presentation:

* a split does not reset the holding period;
* a spinoff allocates basis by relative fair market value, and the new shares
  inherit the original holding period;
* a fractional share an account cannot hold is an error, not a rounding --
  cash in lieu is a taxable disposition.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

import typer

from portable_core.decimals import from_text
from portable_core.domain.enums import (
    InstrumentType,
    LegRole,
    PositionStatus,
    StrategyType,
    TransactionSource,
    TransactionType,
)
from portable_core.domain.models import Instrument, Position, PositionLeg, Transaction
from portable_core.errors import ValidationError
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.persistence.connection import transaction as db_transaction
from portable_core.providers import as_corporate_action_provider
from portable_core.services.corporate_actions import CorporateActionEngine
from portable_pt import state
from portable_pt.commands._shared import dispatch, maybe_dry_run, resolve_date

app = typer.Typer(help="Corporate actions.", no_args_is_help=True)

ENGINE = CorporateActionEngine()
ZERO = Decimal("0.00")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _held_lots(repos, account_id: int, instrument_id: int, on):  # type: ignore[no-untyped-def]
    return repos.lots.open_lots(account_id, instrument_id, as_of=on)


@app.command()
def split(
    symbol: Annotated[str, typer.Argument()],
    ratio: Annotated[
        str,
        typer.Option("--ratio", help="'3:1' for a 3-for-1, '1:10' for a reverse split."),
    ],
    ex_date: Annotated[str | None, typer.Option("--ex-date", "-d")] = None,
    account: Annotated[str | None, typer.Option("--account", "-a")] = None,
) -> None:
    """Apply a forward or reverse split.

    Total cost basis is unchanged; only quantity and per-share basis move.
    **The holding period is not reset** -- and the adjustment row records that
    explicitly, so the assertion is auditable rather than implicit.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        on = resolve_date(ex_date, ctx, what="--ex-date")
        instrument = repos.instruments.resolve(symbol, on=on)

        if ":" not in ratio:
            raise ValidationError(
                f"malformed split ratio {ratio!r}",
                remedy="Use N:M — '3:1' is a 3-for-1 split, '1:10' a reverse split.",
                value=ratio,
            )
        numerator, denominator = (from_text(p.strip()) for p in ratio.split(":", 1))

        accounts = [repos.accounts.resolve(account)] if account else repos.accounts.all()
        rows: list[dict[str, object]] = []
        warnings: list[str] = []

        for target in accounts:
            lots = _held_lots(repos, target.account_id, instrument.instrument_id, on)
            if not lots:
                continue
            result = ENGINE.split(
                lots,
                numerator=numerator,
                denominator=denominator,
                ex_date=on,
                allows_fractional=target.allows_fractional,
            )
            if result.fractional_shares > 0 and not target.allows_fractional:
                warnings.append(
                    f"{target.name}: {result.fractional_shares} fractional share(s) "
                    "dropped by the split. Record the cash in lieu with `pt sell` -- "
                    "it is a taxable disposition, not a rounding."
                )

            for before, after, adjustment in zip(
                lots, result.lots, result.adjustments, strict=True
            ):
                rows.append(
                    {
                        "account": target.name,
                        "lot_id": before.lot_id,
                        "quantity_before": before.remaining_quantity,
                        "quantity_after": after.remaining_quantity,
                        "basis": after.adjusted_cost_basis,
                        "holding_period_start": after.holding_period_start.isoformat(),
                    }
                )

                if not ctx.dry_run:
                    with db_transaction(repos.con):
                        repos.lots.update_basis(after)
                        repos.lots.add_adjustment(replace(adjustment, adjustment_id=0))

            if not ctx.dry_run:
                with db_transaction(repos.con):
                    txn = Transaction(
                        txn_id=0,
                        account_id=target.account_id,
                        trade_date=on,
                        seq=repos.transactions.next_seq(on),
                        txn_type=(
                            TransactionType.SPLIT
                            if numerator > denominator
                            else TransactionType.REVERSE_SPLIT
                        ),
                        net_cash_effect=ZERO,
                        instrument_id=instrument.instrument_id,
                        quantity=result.lots[0].remaining_quantity if result.lots else None,
                        ex_date=on,
                        note=f"{ratio} split",
                        source=TransactionSource.DERIVED,
                        created_at=_now(),
                    )
                    repos.transactions.append(txn)
                    for leg in {lot.leg_id for lot in lots}:
                        repos.positions.update_leg_quantity(
                            leg,
                            sum(
                                (
                                    lot.remaining_quantity
                                    for lot in repos.lots.by_leg(leg)
                                    if lot.is_open
                                ),
                                Decimal(0),
                            ),
                        )

        result_obj = CommandResult(
            command="ca split",
            table=Table(
                columns=(
                    Column("account", "Account"),
                    Column("lot_id", "Lot", ColumnKind.INTEGER),
                    Column("quantity_before", "Qty Before", ColumnKind.QUANTITY),
                    Column("quantity_after", "Qty After", ColumnKind.QUANTITY),
                    Column("basis", "Basis", ColumnKind.MONEY),
                    Column("holding_period_start", "HP Start", ColumnKind.DATE),
                ),
                rows=tuple(rows),
                title=f"{symbol} {ratio} split, ex {on.isoformat()}",
                footnotes=(
                    "Total basis is unchanged. The holding-period start is unchanged "
                    "too -- a split does not reset it, and resetting it would turn a "
                    "long-term gain into a short-term one.",
                ),
            ),
            data={"symbol": instrument.symbol, "ratio": ratio, "ex_date": on.isoformat()},
            warnings=tuple(warnings),
            portfolio=ctx.portfolio_name(),
        )
        return maybe_dry_run(result_obj) if ctx.dry_run else result_obj

    dispatch(action)


@app.command()
def spinoff(
    symbol: Annotated[str, typer.Argument(help="The parent.")],
    spun: Annotated[str, typer.Option("--spun", help="Symbol of the spun-off company.")],
    ratio: Annotated[
        str, typer.Option("--ratio", help="Spun shares received per parent share.")
    ],
    parent_fmv: Annotated[
        str,
        typer.Option("--parent-fmv", help="Parent fair market value per share, after."),
    ],
    spun_fmv: Annotated[
        str, typer.Option("--spun-fmv", help="Spun fair market value per share.")
    ],
    ex_date: Annotated[str | None, typer.Option("--ex-date", "-d")] = None,
    account: Annotated[str | None, typer.Option("--account", "-a")] = None,
) -> None:
    """Apply a spinoff, allocating basis by relative fair market value.

    Both fair market values are required and are recorded on the adjustment,
    because the allocation is only defensible if its inputs are. They come from
    the company's Form 8937, or the post-spinoff market prices.

    **The spun shares inherit the parent's holding period.** They are not newly
    acquired: a spinoff from a five-year-old lot is long-term on day one.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        on = resolve_date(ex_date, ctx, what="--ex-date")
        parent = repos.instruments.resolve(symbol, on=on)

        try:
            child = repos.instruments.resolve(spun, on=on)
        except ValidationError:
            if ctx.dry_run:
                child = Instrument(
                    instrument_id=0,
                    symbol=spun.upper(),
                    instrument_type=InstrumentType.EQUITY,
                )
            else:
                with db_transaction(repos.con):
                    child_id = repos.instruments.add(
                        Instrument(
                            instrument_id=0,
                            symbol=spun.upper(),
                            instrument_type=InstrumentType.EQUITY,
                            source="derived:spinoff",
                        )
                    )
                child = repos.instruments.get(child_id)  # type: ignore[assignment]

        accounts = [repos.accounts.resolve(account)] if account else repos.accounts.all()
        rows: list[dict[str, object]] = []
        warnings: list[str] = []

        for target in accounts:
            lots = _held_lots(repos, target.account_id, parent.instrument_id, on)
            if not lots:
                continue

            if ctx.dry_run:
                position_id, leg_id = 0, 0
            else:
                with db_transaction(repos.con):
                    position_id = repos.positions.add(
                        Position(
                            position_id=0,
                            account_id=target.account_id,
                            strategy_type=StrategyType.SINGLE,
                            opened_date=on,
                            status=PositionStatus.OPEN,
                            note=f"spun off from {parent.symbol}",
                        )
                    )
                    leg_id = repos.positions.add_leg(
                        PositionLeg(
                            leg_id=0,
                            position_id=position_id,
                            instrument_id=child.instrument_id,
                            role=LegRole.LONG_STOCK,
                            sign=1,
                            quantity=Decimal(0),
                            opened_date=on,
                        )
                    )

            outcome = ENGINE.spinoff(
                lots,
                ratio=from_text(ratio),
                parent_fmv=from_text(parent_fmv),
                spun_fmv=from_text(spun_fmv),
                ex_date=on,
                spun_instrument_id=child.instrument_id,
                spun_leg_id=leg_id,
                spun_position_id=position_id,
                allows_fractional=target.allows_fractional,
            )
            if outcome.fractional_shares > 0 and not target.allows_fractional:
                warnings.append(
                    f"{target.name}: {outcome.fractional_shares} fractional spun "
                    "share(s). Record the cash in lieu as a disposition."
                )

            for before, after, adjustment in zip(
                lots, outcome.parent_lots, outcome.adjustments, strict=True
            ):
                rows.append(
                    {
                        "account": target.name,
                        "lot_id": before.lot_id,
                        "symbol": parent.symbol,
                        "basis_before": before.adjusted_cost_basis,
                        "basis_after": after.adjusted_cost_basis,
                        "holding_period_start": after.holding_period_start.isoformat(),
                    }
                )
                if not ctx.dry_run:
                    with db_transaction(repos.con):
                        repos.lots.update_basis(after)
                        repos.lots.add_adjustment(replace(adjustment, adjustment_id=0))

            for spun_lot in outcome.spun_lots:
                rows.append(
                    {
                        "account": target.name,
                        "lot_id": None,
                        "symbol": child.symbol,
                        "basis_before": ZERO,
                        "basis_after": spun_lot.adjusted_cost_basis,
                        "holding_period_start": spun_lot.holding_period_start.isoformat(),
                    }
                )
                if not ctx.dry_run:
                    with db_transaction(repos.con):
                        repos.lots.add(replace(spun_lot, lot_id=0))

            if not ctx.dry_run:
                with db_transaction(repos.con):
                    repos.transactions.append(
                        Transaction(
                            txn_id=0,
                            account_id=target.account_id,
                            trade_date=on,
                            seq=repos.transactions.next_seq(on),
                            txn_type=TransactionType.SPINOFF,
                            net_cash_effect=ZERO,
                            instrument_id=parent.instrument_id,
                            ex_date=on,
                            note=(
                                f"spinoff of {child.symbol} at {ratio} per share; "
                                f"basis allocated by FMV {parent_fmv}/{spun_fmv}"
                            ),
                            source=TransactionSource.DERIVED,
                            created_at=_now(),
                        )
                    )
                    repos.positions.update_leg_quantity(
                        leg_id,
                        sum(
                            (lot.remaining_quantity for lot in outcome.spun_lots),
                            Decimal(0),
                        ),
                    )

        result_obj = CommandResult(
            command="ca spinoff",
            table=Table(
                columns=(
                    Column("account", "Account"),
                    Column("symbol", "Symbol"),
                    Column("lot_id", "Lot", ColumnKind.INTEGER),
                    Column("basis_before", "Basis Before", ColumnKind.MONEY),
                    Column("basis_after", "Basis After", ColumnKind.MONEY),
                    Column("holding_period_start", "HP Start", ColumnKind.DATE),
                ),
                rows=tuple(rows),
                title=f"{symbol} spinoff of {spun}, ex {on.isoformat()}",
                footnotes=(
                    "Basis is allocated by relative fair market value, not by share "
                    "count and not by the spinoff ratio.",
                    "The spun shares inherit the parent's holding-period start, so "
                    "they are long-term on day one if the parent was.",
                ),
            ),
            data={
                "parent": parent.symbol,
                "spun": child.symbol,
                "ratio": ratio,
                "parent_fmv": from_text(parent_fmv),
                "spun_fmv": from_text(spun_fmv),
                "ex_date": on.isoformat(),
            },
            warnings=tuple(warnings),
            portfolio=ctx.portfolio_name(),
        )
        return maybe_dry_run(result_obj) if ctx.dry_run else result_obj

    dispatch(action)


@app.command(name="symbol-change")
def symbol_change(
    symbol: Annotated[str, typer.Argument(help="The current symbol.")],
    to: Annotated[str, typer.Option("--to", help="The new symbol.")],
    effective: Annotated[str | None, typer.Option("--date", "-d")] = None,
) -> None:
    """Rename an instrument, keeping the old symbol resolvable.

    The history row is what lets a trade dated before the change still resolve
    to the right instrument. Without it, a rename silently rewrites history the
    next time somebody queries by ticker.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        on = resolve_date(effective, ctx)
        instrument = repos.instruments.resolve(symbol, on=on)

        payload = {
            "instrument_id": instrument.instrument_id,
            "from": instrument.symbol,
            "to": to.upper(),
            "effective": on.isoformat(),
        }
        if ctx.dry_run:
            return maybe_dry_run(CommandResult(command="ca symbol-change", data=payload))

        with db_transaction(repos.con):
            repos.instruments.rename(instrument.instrument_id, to.upper(), on)
            repos.transactions.append(
                Transaction(
                    txn_id=0,
                    account_id=repos.accounts.all()[0].account_id,
                    trade_date=on,
                    seq=repos.transactions.next_seq(on),
                    txn_type=TransactionType.SYMBOL_CHANGE,
                    net_cash_effect=ZERO,
                    instrument_id=instrument.instrument_id,
                    note=f"{instrument.symbol} -> {to.upper()}",
                    source=TransactionSource.DERIVED,
                    created_at=_now(),
                )
            )

        return CommandResult(
            command="ca symbol-change",
            data=payload,
            warnings=(
                f"{instrument.symbol} remains resolvable for dates before {on.isoformat()}.",
            ),
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@app.command()
def sync(
    symbols: Annotated[list[str] | None, typer.Argument(help="Default: all held.")] = None,
    start: Annotated[str | None, typer.Option("--from")] = None,
    end: Annotated[str | None, typer.Option("--to")] = None,
) -> None:
    """Show corporate actions the provider knows about, without applying them.

    Applying is a separate, explicit step, because a corporate action changes
    basis and holding periods and should not happen as a side effect of a
    refresh.

    **fafnir carries splits and cash dividends only.** Spinoffs, mergers,
    symbol changes and delistings are not in the warehouse, so this output is
    not a complete list of what happened -- it says so rather than implying
    otherwise (ADR 0006).
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        provider = ctx.provider()
        actions_from = as_corporate_action_provider(provider)

        last = resolve_date(end, ctx, what="--to")
        first = (
            resolve_date(start, ctx, what="--from") if start else last.replace(month=1, day=1)
        )
        wanted = symbols or [i.symbol for i in repos.instruments.all(active_only=True)]

        rows: list[dict[str, object]] = []
        warnings: list[str] = []
        for name in wanted:
            try:
                for record in actions_from.corporate_actions(name, first, last):
                    rows.append(
                        {
                            "symbol": record.symbol,
                            "action_type": record.action_type,
                            "ex_date": record.ex_date.isoformat(),
                            "pay_date": (
                                record.pay_date.isoformat() if record.pay_date else None
                            ),
                            "ratio": (
                                f"{record.split_numerator}:{record.split_denominator}"
                                if record.split_numerator
                                else None
                            ),
                            "cash_amount": record.cash_amount,
                        }
                    )
            except Exception as exc:
                warnings.append(f"{name}: {exc}")

        return CommandResult(
            command="ca sync",
            table=Table(
                columns=(
                    Column("symbol", "Symbol"),
                    Column("action_type", "Action"),
                    Column("ex_date", "Ex Date", ColumnKind.DATE),
                    Column("pay_date", "Pay Date", ColumnKind.DATE),
                    Column("ratio", "Ratio"),
                    Column("cash_amount", "Cash", ColumnKind.MONEY),
                ),
                rows=tuple(rows),
                title=f"Corporate actions from {provider.name}",
                footnotes=(
                    "Nothing was applied. Apply each with `pt ca split`, "
                    "`pt ca spinoff`, and so on -- a basis change should not happen "
                    "as a side effect of a refresh.",
                    "fafnir carries splits and cash dividends only. Spinoffs, "
                    "mergers, symbol changes and delistings are NOT in this list "
                    "and must be entered by hand.",
                ),
            ),
            data={"found": len(rows), "from": first.isoformat(), "to": last.isoformat()},
            warnings=tuple(warnings),
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)
