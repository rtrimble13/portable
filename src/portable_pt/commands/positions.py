"""Positions and lots."""

from __future__ import annotations

from datetime import date
from typing import Annotated

import typer

from portable_core.disclaimer import (
    BOND_TAX_NOT_COMPUTED,
    TAX_DISCLAIMER,
    WASH_SALES_DETECTED,
    WASH_SALES_NOT_DETECTED,
)
from portable_core.domain.enums import StrategyType, WashSaleStatus
from portable_core.errors import ValidationError
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.persistence.repositories import Repositories
from portable_core.services.labels import Label, record_label
from portable_core.services.regrouping import Regroup, record_regroup
from portable_core.services.replay import state_on
from portable_core.services.sale_candidates import (
    Candidate,
    CandidateReport,
    CandidateSort,
    UnstatedReason,
    sale_candidates,
)
from portable_pt import state
from portable_pt.commands._shared import dispatch, resolve_date

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
                        "label": position.label,
                        "strategy": str(position.strategy_type),
                        "symbol": instrument.symbol if instrument else None,
                        "role": str(leg.role),
                        # ADR 0038: a short leg shows negative, as statements do.
                        "quantity": leg.signed_quantity,
                        "opened": position.opened_date.isoformat(),
                        "status": str(leg.status),
                    }
                )

        return CommandResult(
            command="position list",
            table=Table(
                columns=(
                    Column("position_id", "Position", ColumnKind.INTEGER),
                    Column("label", "Label"),
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
    date_text: Annotated[str | None, typer.Option("--date", "-d")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Move legs into one position, restating the trader's intent.

    This is what happens when a long stock holding becomes a covered call
    because a call was written against it. Each leg moved is a
    `position_regroup` row in the ledger, applied by replay like any trade --
    so the grouping survives every rebuild, and is undone with
    `pt trade reverse` like any other entry (ADR 0023). No lot and no basis
    figure moves: **a change of intent does not change tax history** (ADR 0009).
    Without --strategy, the position is named from its legs.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        on = resolve_date(date_text, ctx)
        target = repos.positions.get(into)
        if target is None or target.opened_txn_id is None:
            raise ValidationError(
                f"no position {into}", remedy="`pt position list` shows them."
            )
        chosen = StrategyType(strategy) if strategy else None
        moves = [
            _regroup_for(repos, leg_id, on, target.opened_txn_id, chosen, note)
            for leg_id in legs
        ]

        payload: dict[str, object] = {
            "legs": legs,
            "into": into,
            "date": on.isoformat(),
            "strategy": str(chosen) if chosen else None,
        }
        with ctx.write():
            recorded = [record_regroup(repos, move) for move in moves]

        regrouped = repos.positions.opened_by(target.opened_txn_id)
        return CommandResult(
            command="position group",
            data={
                **payload,
                "txn_ids": [r.transaction.txn_id for r in recorded],
                # Looked up again: a back-dated regroup rebuilds, and a rebuild
                # may renumber the position.
                "position_id": regrouped.position_id if regrouped else None,
                "strategy": str(regrouped.strategy_type) if regrouped else None,
                "rebuilt": any(r.rebuilt for r in recorded),
            },
            warnings=(
                "No lot and no basis figure was touched. A change of intent does not "
                "change tax history.",
            ),
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@position_app.command()
def ungroup(
    legs: Annotated[list[int], typer.Argument(help="Leg ids to move out, each on its own.")],
    date_text: Annotated[str | None, typer.Option("--date", "-d")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Move legs out of their position, each into a new position of its own.

    The inverse of `pt position group`, recorded the same way: a
    `position_regroup` row per leg, replayed like any other (ADR 0023).
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        on = resolve_date(date_text, ctx)
        moves = []
        for leg_id in legs:
            leg = repos.positions.leg(leg_id)
            if leg is not None and len(repos.positions.legs(leg.position_id)) == 1:
                raise ValidationError(
                    f"leg {leg_id} is the only leg of position {leg.position_id}",
                    remedy="There is nothing to ungroup it from.",
                )
            moves.append(_regroup_for(repos, leg_id, on, None, None, note))

        payload: dict[str, object] = {"legs": legs, "date": on.isoformat()}
        with ctx.write():
            recorded = [record_regroup(repos, move) for move in moves]

        return CommandResult(
            command="position ungroup",
            data={
                **payload,
                "txn_ids": [r.transaction.txn_id for r in recorded],
                "rebuilt": any(r.rebuilt for r in recorded),
            },
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@position_app.command()
def label(
    position_id: Annotated[
        int, typer.Argument(help="Position id, as `pt position list` shows it.")
    ],
    text: Annotated[str | None, typer.Argument(help="The label.")] = None,
    clear: Annotated[bool, typer.Option("--clear", help="Remove the label.")] = False,
    date_text: Annotated[str | None, typer.Option("--date", "-d")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Name a position, or remove its name.

    A label is a `position_label` row in the ledger, replayed like any other, so
    it survives every rebuild and is undone with `pt trade reverse` (ADR 0027).
    It names the position and changes nothing else: no leg, no lot, no cash.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        if (text is None) == (not clear):
            raise ValidationError(
                "give a label, or --clear, but not both",
                remedy='`pt position label 12 "Covered call on AAPL"`, or `--clear`.',
            )
        on = resolve_date(date_text, ctx)
        position = repos.positions.get(position_id)
        if position is None or position.opened_txn_id is None:
            raise ValidationError(
                f"no position {position_id}", remedy="`pt position list` shows them."
            )

        with ctx.write():
            recorded = record_label(
                repos, Label(position.opened_txn_id, on, None if clear else text, note)
            )

        labelled = repos.positions.opened_by(position.opened_txn_id)
        return CommandResult(
            command="position label",
            data={
                # Looked up again: a back-dated label rebuilds, and a rebuild
                # may renumber the position.
                "position_id": labelled.position_id if labelled else None,
                "label": labelled.label if labelled else None,
                "date": on.isoformat(),
                "txn_id": recorded.transaction.txn_id,
                "rebuilt": recorded.rebuilt,
            },
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


def _regroup_for(
    repos: Repositories,
    leg_id: int,
    on: date,
    into_opened_by: int | None,
    strategy: StrategyType | None,
    note: str | None,
) -> Regroup:
    """The ledger row that moves *leg_id*: named by account and instrument.

    A leg id, like a position id, is derived and renumbered by a rebuild, so
    the row records what the leg *is* -- this account's holding of this
    instrument -- rather than which number it had today.
    """
    leg = repos.positions.leg(leg_id)
    position = repos.positions.get(leg.position_id) if leg is not None else None
    if leg is None or position is None:
        raise ValidationError(f"no leg {leg_id}", remedy="`pt position show` lists legs.")
    return Regroup(
        account_id=position.account_id,
        instrument_id=leg.instrument_id,
        on=on,
        into_opened_by=into_opened_by,
        strategy=strategy,
        note=note,
    )


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

        def read() -> list[dict[str, object]]:
            # Lots as they stood at the end of --as-of: a lot sold since is
            # listed, and one bought since is not (ADR 0021).
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
                                "lot_key": lot.lot_key,
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
            return rows

        as_of_state = state_on(repos, ctx.as_of, read)
        rows = as_of_state.results[0]

        return CommandResult(
            command="lot list",
            table=Table(
                columns=(
                    Column("lot_id", "Lot", ColumnKind.INTEGER),
                    Column("lot_key", "Key"),
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
                    "Designate lots on a sale with --lots 'lot_id:qty;lot_id:qty', or "
                    "by key as '@key:qty'. The sale records each lot by its key, which "
                    "no later entry or rebuild can change (ADR 0036).",
                ),
            ),
            warnings=as_of_state.warnings,
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@lot_app.command(name="candidates")
def lot_candidates(
    symbol: Annotated[str | None, typer.Argument(help="Filter to one instrument.")] = None,
    account: Annotated[str | None, typer.Option("--account", "-a")] = None,
    losses: Annotated[
        bool, typer.Option("--losses", help="Only lots that would be sold at a loss.")
    ] = False,
    sort: Annotated[
        CandidateSort, typer.Option("--sort", help="Row order; unknown figures last.")
    ] = CandidateSort.ACQUIRED,
) -> None:
    """Every open lot in a taxable account, as if sold alone on --as-of.

    For each lot: the proceeds at the newest price no older than the
    staleness tolerance, the gain, the holding period and the first day it is
    long-term, the estimated tax, and for a loss, the replacements already
    bought, in any account, that would wash it (ADR 0047). **An estimate, not
    tax advice. Read-only, and it proposes nothing.** Each row is one sale on
    its own, so the rows do not add up to anything. Sell a row with the
    `--lots` designation it shows.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        # The rule `pt holdings` and `pt pnl` apply, so the report agrees with
        # the holdings beside it (ADR 0047, answer 1).
        tolerance = int(ctx.config.get("staleness_tolerance_days", 5))
        report, warnings = sale_candidates(
            repos,
            as_of=ctx.as_of,
            staleness_tolerance_days=tolerance,
            account=repos.accounts.resolve(account) if account else None,
            instrument=repos.instruments.resolve(symbol, on=ctx.as_of) if symbol else None,
            losses_only=losses,
            sort=sort,
        )
        return CommandResult(
            command="lot candidates",
            table=Table(
                columns=_CANDIDATE_COLUMNS,
                rows=tuple(_candidate_row(row) for row in report.rows),
                title=f"Each open lot sold alone on {ctx.as_of.isoformat()}",
                footnotes=_candidate_notes(report),
            ),
            data={
                "staleness_tolerance_days": tolerance,
                "sort": str(sort),
                "losses_only": losses,
                # The rows are alternatives: two loss lots can each be washed
                # by the same recent shares, and sold together only those
                # shares wash. A field, so a consumer cannot sum them unawares.
                "is_additive": False,
                "candidates": [_candidate_detail(row) for row in report.rows],
                "not_listed": {
                    "averaged": [
                        {
                            "account": a.account,
                            "symbol": a.symbol,
                            "lots": a.lots,
                            "quantity": a.quantity,
                            "reason": a.reason,
                        }
                        for a in report.averaged
                    ],
                    "short_lots": report.short_lots,
                    "sheltered_lots": report.sheltered_lots,
                },
                # ADR 0037. Envelope fields, as in `pt tax`: what the check
                # covers, and what it does not, travel with every row.
                "wash_sales": {
                    "detected": list(WASH_SALES_DETECTED),
                    "not_detected": list(WASH_SALES_NOT_DETECTED),
                },
            },
            human_data=False,
            disclaimer=TAX_DISCLAIMER,
            warnings=warnings,
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


_CANDIDATE_COLUMNS = (
    Column("designation", "Sell with"),
    Column("account", "Account"),
    Column("symbol", "Symbol"),
    Column("open_date", "Acquired", ColumnKind.DATE),
    Column("quantity", "Qty", ColumnKind.QUANTITY),
    Column("basis", "Basis", ColumnKind.MONEY),
    Column("basis_from", "Basis from"),
    Column("price", "Price", ColumnKind.PRICE),
    Column("price_date", "Priced", ColumnKind.DATE),
    Column("price_age_days", "Age", ColumnKind.INTEGER),
    Column("price_source", "Source"),
    Column("proceeds", "Proceeds", ColumnKind.MONEY),
    Column("gain", "Gain/Loss", ColumnKind.MONEY),
    Column("holding_period", "Holding"),
    Column("long_term_from", "Long from", ColumnKind.DATE),
    Column("days_to_long_term", "To long", ColumnKind.INTEGER),
    Column("wash_deferred", "Wash deferred", ColumnKind.MONEY),
    Column("wash_lost", "Wash lost", ColumnKind.MONEY),
    Column("window_clears", "Clears", ColumnKind.DATE),
    Column("estimated_tax", "Est. Tax", ColumnKind.MONEY),
    Column("tax_per_dollar", "Tax/$", ColumnKind.RATE),
    Column("waiting_saves", "Waiting saves", ColumnKind.MONEY),
    Column("note", "Not stated"),
)


def _candidate_row(row: Candidate) -> dict[str, object]:
    """The row as the table shows it: one line, nulls where nothing is known."""
    wash = row.wash
    price = row.price
    return {
        "designation": row.designation,
        "account": row.account.name,
        "symbol": row.instrument.symbol,
        "open_date": row.lot.open_date.isoformat(),
        "quantity": row.lot.remaining_quantity,
        "basis": row.lot.adjusted_cost_basis,
        "basis_from": str(row.lot.basis_source),
        "price": price.price if price is not None else None,
        "price_date": price.price_date.isoformat() if price is not None else None,
        "price_age_days": row.price_age_days,
        "price_source": price.source if price is not None else None,
        "proceeds": row.proceeds,
        "gain": row.gain,
        "holding_period": str(row.holding_period),
        "long_term_from": row.long_term_from.isoformat(),
        "days_to_long_term": row.days_to_long_term,
        "wash_deferred": wash.disallowed(WashSaleStatus.DEFERRED) if wash else None,
        "wash_lost": wash.disallowed(WashSaleStatus.PERMANENT) if wash else None,
        "window_clears": (
            wash.window_clears.isoformat() if wash and wash.window_clears else None
        ),
        "estimated_tax": row.estimated_tax,
        "tax_per_dollar": row.tax_per_dollar,
        "waiting_saves": row.waiting_saves,
        "note": "; ".join(_SHORT_REASON[u.reason] for u in row.unstated) or None,
    }


_SHORT_REASON = {
    UnstatedReason.PRICE_MISSING: "no price",
    UnstatedReason.PRICE_STALE: "stale price",
    UnstatedReason.NO_MARKET_PRICE: "no market price",
    UnstatedReason.BASIS_UNAVAILABLE: "basis unavailable",
    UnstatedReason.NO_RATE_SCHEDULE: "no rate schedule",
}


def _candidate_detail(row: Candidate) -> dict[str, object]:
    """Everything about one row, for a machine: `data.candidates`, in row order."""
    price = row.price
    wash = row.wash
    tax = row.tax
    notes: list[str] = []
    if row.instrument.is_bond:
        notes.extend(f"not computed for a bond: {item}" for item in BOND_TAX_NOT_COMPUTED)
    if row.instrument.is_option:
        notes.append(
            "a wash by another option, or by the underlying, is not detected: "
            + WASH_SALES_NOT_DETECTED[1]
        )
    return {
        "lot_id": row.lot.lot_id,
        "lot_key": row.lot.lot_key,
        "designation": row.designation,
        "account": row.account.name,
        "symbol": row.instrument.symbol,
        "instrument_type": str(row.instrument.instrument_type),
        "open_date": row.lot.open_date.isoformat(),
        "quantity": row.lot.remaining_quantity,
        "basis": row.lot.adjusted_cost_basis,
        "basis_from": str(row.lot.basis_source),
        "is_approximate_basis": row.is_approximate,
        "holding_period_start": row.lot.holding_period_start.isoformat(),
        "holding_period": str(row.holding_period),
        "days_held": row.days_held,
        "long_term_from": row.long_term_from.isoformat(),
        "days_to_long_term": row.days_to_long_term,
        "price": (
            {
                "price": price.price,
                "price_date": price.price_date.isoformat(),
                "source": price.source,
                "age_days": row.price_age_days,
                "status": str(row.price_status),
                # PORT-GIPS-A02: a judged estimate is a price, and is used,
                # but a sale decided at it should know it is one.
                "valuation_level": price.valuation_level,
                "is_estimate": price.is_estimate,
            }
            if price is not None
            else None
        ),
        "proceeds": row.proceeds,
        "gain": row.gain,
        "wash": (
            {
                "replacements": [
                    {
                        "lot_key": r.lot_key,
                        "account": r.account,
                        "acquired": r.acquired.isoformat(),
                        "quantity": r.quantity,
                        "disallowed": r.disallowed,
                        "status": str(r.status),
                    }
                    for r in wash.replacements
                ],
                "washed_quantity": wash.washed_quantity,
                "deferred": wash.disallowed(WashSaleStatus.DEFERRED),
                "permanently_disallowed": wash.disallowed(WashSaleStatus.PERMANENT),
                "manual": wash.disallowed(WashSaleStatus.MANUAL),
                "window_clears": wash.window_clears.isoformat() if wash.window_clears else None,
                "buying_washes_through": wash.buying_washes_through.isoformat(),
            }
            if wash is not None
            else None
        ),
        "allowed_gain": row.allowed_gain,
        "tax": (
            {
                "is_estimate": True,
                "federal_rate": tax.federal_rate,
                "state_rate": tax.state_rate,
                "niit_rate": tax.niit_rate,
                "estimated_tax": tax.estimated_tax,
            }
            if tax is not None
            else None
        ),
        "tax_per_dollar": row.tax_per_dollar,
        "waiting_saves": row.waiting_saves,
        "unstated": [
            {"reason": str(u.reason), "message": u.message, "code": u.code}
            for u in row.unstated
        ],
        "notes": notes,
    }


def _candidate_notes(report: CandidateReport) -> tuple[str, ...]:
    """What a reader must know before using any row."""
    rows = report.rows
    notes = [
        "Each row is its lot sold alone, all of it, on this date. The rows are "
        "alternatives, not parts of one sale, and do not add up: two loss lots can "
        "each be washed by the same recent shares, and sold together only those "
        "shares wash. There are no totals.",
        f"Priced at the newest price on or before the date, no older than "
        f"{report.staleness_tolerance_days} days. No selling cost is deducted, since "
        "none is known before the trade.",
        "Est. Tax is the allowed gain times the account's rate in force for the "
        "holding period (ADR 0011); Tax/$ is it over the proceeds, negative where a "
        "loss saves tax. Waiting saves: the estimate today less the estimate on the "
        "first long-term day, at today's price and before any wash sale; positive "
        "means waiting saves tax.",
    ]
    if any(row.lot.holding_period_start != row.lot.open_date for row in rows):
        notes.append(
            "Long from counts from the holding-period start, not the acquisition: a "
            "spinoff's shares inherit their parent's, and a wash sale's replacement "
            "absorbs the days of the shares sold."
        )
    if any(row.wash is not None for row in rows):
        notes.append(
            "A loss is also washed by buying the instrument, or one declared "
            "substantially identical, in any account, IRAs included, within 30 days "
            "after the sale -- a dividend reinvestment too. Wash lost: a replacement "
            "in a sheltered account loses the loss for good; it is not deferred. "
            "Clears: the first day no replacement already bought is within 30 days "
            "before the sale, if nothing more is bought."
        )
    if any(r.status is WashSaleStatus.MANUAL for row in rows if row.wash
           for r in row.wash.replacements):  # fmt: skip
        notes.append(
            "Some replacements were disposed of before the date; a sale would report "
            "them for manual handling and mark its tax year incomplete (ADR 0037)."
        )
    estimated = sorted(
        {
            row.instrument.symbol
            for row in rows
            if row.price is not None and row.price.is_estimate and row.proceeds is not None
        }
    )
    if estimated:
        notes.append(
            f"Priced at a judged estimate, not a market price: {', '.join(estimated)}. "
            "Its proceeds, gain and tax are only as good as that judgement."
        )
    if any(row.is_approximate for row in rows):
        notes.append(
            "Only 'derived' basis is this portfolio's own arithmetic; the other rungs "
            "came from a custodian or a reconstruction, and their gains are "
            "approximate (ADR 0017)."
        )
    bonds = sorted({row.instrument.symbol for row in rows if row.instrument.is_bond})
    if bonds:
        notes.append(
            f"Bonds ({', '.join(bonds)}): accrued interest is not proceeds (ADR 0041), "
            "and NOT computed -- " + "; ".join(BOND_TAX_NOT_COMPUTED) + "."
        )
    if any(row.unstated for row in rows):
        notes.append(
            "A blank figure is not stated, never zero: no price, a price older than "
            "the tolerance, a basis that cannot be derived, or no rate schedule."
        )
    left_out: list[str] = []
    if report.short_lots:
        left_out.append(f"{report.short_lots} short lot(s); closing one is a purchase")
    if report.sheltered_lots:
        left_out.append(
            f"{report.sheltered_lots} lot(s) in sheltered accounts, which owe no tax on a sale"
        )
    left_out.extend(f"{a.symbol} in {a.account} ({a.reason})" for a in report.averaged)
    if left_out:
        notes.append("Not listed: " + "; ".join(left_out) + ".")
    notes.append("Wash sales NOT detected: " + "; ".join(WASH_SALES_NOT_DETECTED) + ".")
    return tuple(notes)


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
                "lot_key": lot.lot_key,
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
