"""`pt lot designation`: what a designation written before schema 10 means.

ADR 0036. A spec-ID row written before schema 10 names lot ids, which every
rebuild renumbers. `pt migrate` recorded what each one meant. Where the meaning
when written and the reading before schema 10 disagree, or the first cannot be
established, the row is pending and replay refuses it until it is decided here.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

import typer

from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_USAGE
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.services import designations
from portable_core.services.replay import ReplayEngine
from portable_pt import state
from portable_pt.commands._shared import dispatch

app = typer.Typer(
    help="Spec-ID designations written before schema 10, and what they mean (ADR 0036).",
    no_args_is_help=True,
)


@app.command(name="list")
def list_designations(
    pending: Annotated[
        bool, typer.Option("--pending", help="Only the rows waiting for a decision.")
    ] = False,
) -> None:
    """List each old designation, both readings, and the gain each gives.

    A pending row shows the realized gain under each reading, so the decision
    is made with its consequence in view.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        recorded = repos.designations.all(pending_only=pending)
        gains = (
            designations.gains_by_reading(repos) if any(d.is_pending for d in recorded) else {}
        )

        rows: list[dict[str, object]] = []
        for designation in recorded:
            txn = repos.transactions.get(designation.txn_id)
            instrument = (
                repos.instruments.get(txn.instrument_id)
                if txn is not None and txn.instrument_id is not None
                else None
            )
            account = repos.accounts.get(txn.account_id) if txn is not None else None
            by_reading = gains.get(designation.txn_id, {})
            rows.append(
                {
                    "txn_id": designation.txn_id,
                    "trade_date": txn.trade_date.isoformat() if txn else None,
                    "account": account.name if account else None,
                    "symbol": instrument.symbol if instrument else None,
                    "written": txn.lot_selection if txn else None,
                    "status": str(designation.status),
                    "as_written": designation.as_written,
                    "as_read": designation.as_read,
                    "gain_as_written": by_reading.get("as_written"),
                    "gain_as_read": by_reading.get("as_read"),
                    "in_force": designation.lot_selection,
                    "note": designation.note,
                }
            )

        waiting = sum(1 for d in recorded if d.is_pending)
        return CommandResult(
            command="lot designation list",
            table=Table(
                columns=(
                    Column("txn_id", "Txn", ColumnKind.INTEGER),
                    Column("trade_date", "Date", ColumnKind.DATE),
                    Column("account", "Account"),
                    Column("symbol", "Symbol"),
                    Column("written", "Written"),
                    Column("status", "Status"),
                    Column("as_written", "As Written"),
                    Column("as_read", "As Read"),
                    Column("gain_as_written", "Gain As Written", ColumnKind.MONEY),
                    Column("gain_as_read", "Gain As Read", ColumnKind.MONEY),
                    Column("note", "Note"),
                ),
                rows=tuple(rows),
                title="Designations written before schema 10",
                footnotes=(
                    "As written: the lots the ids named when the row was appended. As "
                    "read: the lots they named as the ledger was read before schema 10.",
                    "A pending row is refused by replay until `pt lot designation "
                    "resolve` decides it. Neither reading is chosen for you.",
                ),
            ),
            data={"designations": rows, "pending": waiting},
            warnings=(
                (
                    f"{waiting} designation(s) wait for a decision; replay refuses each "
                    "until it is resolved.",
                )
                if waiting
                else ()
            ),
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@app.command()
def resolve(
    txn_id: Annotated[int, typer.Argument(help="The sale's transaction id.")],
    as_written: Annotated[
        bool,
        typer.Option("--as-written", help="The lots the ids named when the row was written."),
    ] = False,
    as_read: Annotated[
        bool,
        typer.Option("--as-read", help="The lots they named as read before schema 10."),
    ] = False,
    reason: Annotated[
        str, typer.Option("--reason", help="The evidence: a lot report, a 1099-B.")
    ] = "",
) -> None:
    """Decide what a pending designation means, once, and rebuild.

    The decision is recorded with its reason and never changed. If neither
    reading is right, reverse the sale with `pt trade reverse` and record it
    again naming the lots it relieved.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        if as_written == as_read:
            raise ValidationError(
                "choose exactly one of --as-written and --as-read",
                code=E_USAGE,
                remedy="`pt lot designation list --pending` shows both readings.",
            )
        reading = "as_written" if as_written else "as_read"

        with ctx.write():
            decided = designations.resolve(repos, txn_id, reading=reading, reason=reason)
            outcome = ReplayEngine(repos).rebuild()

        gain = sum(
            (d.realized_gain for d in repos.lots.dispositions() if d.txn_id == txn_id),
            Decimal("0.00"),
        )
        return CommandResult(
            command="lot designation resolve",
            data={
                "txn_id": txn_id,
                "reading": reading,
                "lot_selection": decided.lot_selection,
                "reason": decided.note,
                "realized_gain": gain,
                "rebuilt": True,
            },
            warnings=outcome.warnings,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)
