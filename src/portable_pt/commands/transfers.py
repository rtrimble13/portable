"""`pt transfer in` / `pt transfer out` -- securities crossing the boundary.

ADR 0015. Shares that arrive or leave without being bought or sold: an account
funded in kind from a previous custodian, a gift of stock, a position that
predates every record available and has to enter the ledger somehow.

`portable` had no way to express any of this. Every transaction type that
creates a lot also moves cash, so the only available route was a back-dated
`buy` -- which invents a cash outflow, which then needs an invented `deposit`
to fund it. That deposit is an **external cash flow**, and inventing external
flows is precisely how a track record is silently rewritten (ADR 0007).

Note what these are not: `pt cash transfer` moves money between two of the
owner's own accounts and nets to zero at portfolio level. These cross the
portfolio boundary and do not net. Two opposite flow classifications, which is
why they are two different transaction types.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Annotated

import typer

from portable_core.domain.enums import BasisSource, ReliefMethod, TransactionType
from portable_core.domain.models import Transaction
from portable_core.errors import UsageError
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.persistence.connection import transaction as db_transaction
from portable_core.services.replay import ReplayEngine
from portable_core.services.trading import TradingService
from portable_pt import state
from portable_pt.commands._shared import (
    RefOpt,
    dispatch,
    maybe_dry_run,
    money_arg,
    resolve_date,
)

app = typer.Typer(
    help="Securities crossing the portfolio boundary without being traded.",
    no_args_is_help=True,
)

BasisSourceOpt = Annotated[
    str,
    typer.Option(
        "--basis-source",
        help=(
            "Where the basis came from: custodian_asserted | reconstructed | "
            "estimated | unavailable. Never 'derived' -- that means portable "
            "computed it, and this basis came from elsewhere (ADR 0017)."
        ),
    ),
]


def transfer_in(
    symbol: Annotated[str, typer.Argument(help="Instrument transferred in.")],
    account: Annotated[str, typer.Option("--account", "-a")],
    qty: Annotated[str, typer.Option("--qty", help="Always positive.")],
    value: Annotated[
        str,
        typer.Option(
            "--value",
            help="Market value on the transfer date. The FLOW amount, not the basis.",
        ),
    ],
    basis_source: BasisSourceOpt,
    basis: Annotated[
        str | None,
        typer.Option(
            "--basis",
            help="Total cost basis at the delivering custodian. The TAX number.",
        ),
    ] = None,
    acquired: Annotated[
        str | None,
        typer.Option("--acquired", help="When the owner originally bought the shares."),
    ] = None,
    assumption: Annotated[
        str | None,
        typer.Option(
            "--assumption",
            help="How the basis was arrived at. Required for anything approximate.",
        ),
    ] = None,
    date_text: Annotated[str | None, typer.Option("--date", "-d")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
    ref: RefOpt = None,
) -> None:
    """Record securities arriving in kind.

    **`--value` and `--basis` are different numbers and must not be swapped.**
    `--value` is what the shares were worth on the day they arrived: it is the
    flow amount and drives the period's return. `--basis` is what the owner
    paid at the delivering custodian, whenever that was: it is unrelated to the
    transfer and is what the tax engine uses forever after.

    Swap them and both are wrong in ways that do not announce themselves. Value
    as basis makes every future sale report the gain since the transfer instead
    of since the purchase. Basis as value makes the period's return wrong by the
    entire unrealized gain.

    **The holding period is preserved, not restarted.** `--acquired` becomes the
    lot's open date: a change of custodian is not a disposition. Restarting it
    would convert long-term gains into short-term ones on the next sale, which
    is a wrong number in the direction of a larger tax bill.

    **No cash moves.** That is the whole point of the type.
    """
    _record(
        symbol,
        account=account,
        qty=qty,
        value=value,
        basis=basis,
        acquired=acquired,
        basis_source=basis_source,
        assumption=assumption,
        method=None,
        lots=None,
        date_text=date_text,
        note=note,
        ref=ref,
        outward=False,
    )


def transfer_out(
    symbol: Annotated[str, typer.Argument(help="Instrument transferred out.")],
    account: Annotated[str, typer.Option("--account", "-a")],
    qty: Annotated[str, typer.Option("--qty", help="Always positive.")],
    value: Annotated[str, typer.Option("--value", help="Market value on the transfer date.")],
    method: Annotated[
        str | None,
        typer.Option("--method", help="Relief method: spec, fifo, lifo, hifo, lofo, avg."),
    ] = None,
    lots: Annotated[
        str | None,
        typer.Option("--lots", help="Spec-ID designation, e.g. '12:100;15:50'."),
    ] = None,
    date_text: Annotated[str | None, typer.Option("--date", "-d")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
    ref: RefOpt = None,
) -> None:
    """Record securities leaving in kind.

    Simpler than the inward side: the lots being relieved already carry their
    own basis and acquisition dates, so nothing external is asserted.

    **This is not a disposition.** Nothing was sold, so no gain is realized and
    `pt tax` does not report one. The shares left the portfolio at their market
    value, which is the outward flow.

    Which lots leave is still a real question, and it is the same question a
    sale asks: `--method` and `--lots` work exactly as they do on `pt sell`,
    and an account whose default is spec-ID demands a designation here too.
    Note ADR 0017's consequence — a block seeded by reconstruction is one lot,
    so a designation can name the block but not shares inside it.
    """
    _record(
        symbol,
        account=account,
        qty=qty,
        value=value,
        basis=None,
        acquired=None,
        basis_source=None,
        assumption=None,
        method=method,
        lots=lots,
        date_text=date_text,
        note=note,
        ref=ref,
        outward=True,
    )


def _record(
    symbol: str,
    *,
    account: str,
    qty: str,
    value: str,
    basis: str | None,
    acquired: str | None,
    basis_source: str | None,
    assumption: str | None,
    method: str | None,
    lots: str | None,
    date_text: str | None,
    note: str | None,
    ref: str | None,
    outward: bool,
) -> None:
    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        trade_date = resolve_date(date_text, ctx)

        found_account = repos.accounts.resolve(account)
        instrument = repos.instruments.resolve(symbol, on=trade_date)
        service = TradingService(repos)
        quantity = money_arg(qty, what="--qty")
        market_value = money_arg(value, what="--value")

        if outward:
            txn = service.record_transfer_out(
                found_account,
                instrument,
                quantity,
                trade_date,
                value=market_value,
                relief_method=ReliefMethod(method) if method else None,
                lot_selection=lots,
                note=note,
                external_ref=ref,
            )
        else:
            txn = service.record_transfer_in(
                found_account,
                instrument,
                quantity,
                trade_date,
                value=market_value,
                original_basis=(
                    money_arg(basis, what="--basis") if basis is not None else None
                ),
                original_acquired_date=(
                    resolve_date(acquired, ctx, what="--acquired") if acquired else None
                ),
                basis_source=_basis_source(basis_source),
                basis_assumption=assumption,
                note=note,
                external_ref=ref,
            )

        result = _result(symbol, txn, quantity, market_value, ctx.portfolio_name())
        if ctx.dry_run:
            return maybe_dry_run(result)

        with db_transaction(repos.con):
            txn_id = repos.transactions.append(txn)
            rebuilt = ReplayEngine(repos).apply_or_rebuild(replace(txn, txn_id=txn_id))

        payload: dict[str, object] = {**result.data, "txn_id": txn_id}
        warnings = result.warnings
        if rebuilt:
            payload["rebuilt"] = True
            warnings = (
                *warnings,
                "this entry is back-dated, so derived state was rebuilt from the "
                "ledger. Figures that depend on lot relief may have changed.",
            )
        return replace(result, data=payload, warnings=warnings)

    dispatch(action)


def _basis_source(value: str | None) -> BasisSource:
    if value is None:  # pragma: no cover -- the outward path never asks
        return BasisSource.CUSTODIAN_ASSERTED
    try:
        return BasisSource(value)
    except ValueError:
        raise UsageError(
            f"unknown basis source {value!r}",
            remedy=(
                "One of: custodian_asserted, reconstructed, estimated, unavailable. "
                "Not 'derived' -- that means portable computed the basis from its own "
                "ledger, and a transferred lot's basis came from somewhere else."
            ),
            basis_source=value,
        ) from None


def _result(
    symbol: str,
    txn: Transaction,
    quantity: Decimal,
    value: Decimal,
    portfolio: str | None,
) -> CommandResult:
    inward = txn.txn_type is TransactionType.TRANSFER_IN
    basis = txn.original_basis
    acquired: date | None = txn.original_acquired_date
    source = txn.basis_source

    return CommandResult(
        command=f"transfer {'in' if inward else 'out'}",
        data={
            "symbol": symbol,
            "quantity": str(quantity),
            # The flow amount. Named `value`, never `basis`, everywhere it
            # travels -- the two must stay distinguishable in the output as
            # well as in the input.
            "value": str(value),
            "cost_basis": None if basis is None else str(basis),
            "acquired": acquired.isoformat() if acquired else None,
            "basis_source": str(source) if source else None,
            "net_cash_effect": "0.00",
        },
        table=Table(
            columns=(
                Column("symbol", "Symbol"),
                Column("quantity", "Quantity", ColumnKind.QUANTITY),
                Column("value", "Market value", ColumnKind.MONEY),
                Column("cost_basis", "Cost basis", ColumnKind.MONEY),
                Column("acquired", "Acquired", ColumnKind.DATE),
                Column("basis_source", "Basis from"),
            ),
            rows=(
                {
                    "symbol": symbol,
                    "quantity": quantity,
                    "value": value,
                    "cost_basis": basis,
                    "acquired": acquired,
                    "basis_source": str(source) if source else None,
                },
            ),
            footnotes=(
                "Market value is the flow amount; cost basis is the tax number. "
                "They are different figures about different questions and are "
                "never interchangeable.",
                "No cash moved. The holding period runs from the acquisition date, "
                "not from the transfer: a change of custodian is not a disposition.",
            )
            if inward
            else (
                "No cash moved, and nothing was sold — this is not a disposition "
                "and no gain is realized.",
            ),
        ),
        portfolio=portfolio,
    )
