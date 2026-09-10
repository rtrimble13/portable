"""Cash and income: deposits, withdrawals, transfers, fees, dividends, coupons."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

import typer

from portable_core.decimals import quantize_money
from portable_core.domain.enums import FeeClass, TransactionType
from portable_core.errors import ValidationError
from portable_core.formatters import CommandResult
from portable_core.persistence.connection import transaction as db_transaction
from portable_core.services.trading import TradingService
from portable_pt import state
from portable_pt.commands._shared import (
    RefOpt,
    dispatch,
    maybe_dry_run,
    money_arg,
    resolve_date,
)

#: Said whenever a back-dated entry causes a rebuild, so that a figure changing
#: underneath an already-reported number is visible rather than silent (ADR 0016).
_BACKDATED = (
    "this entry is back-dated, so derived state was rebuilt from the ledger. "
    "Figures that depend on lot relief may have changed."
)

WithheldOpt = Annotated[
    str | None,
    typer.Option(
        "--withheld",
        help="Tax withheld from the gross. Not a fee -- --amount stays the gross.",
    ),
]
ReclaimableOpt = Annotated[
    str | None,
    typer.Option(
        "--reclaimable",
        help=(
            "The reclaimable portion of --withheld. Accrued rather than deducted "
            "from return (PORT-GIPS-A06)."
        ),
    ),
]

app = typer.Typer(help="Cash movements.", no_args_is_help=True)
income_app = typer.Typer(
    help="Income: dividends, coupons, return of capital.", no_args_is_help=True
)


def _record(
    txn_type: TransactionType,
    account: str,
    amount: str,
    on: str | None,
    *,
    counter: str | None = None,
    fee_class: str | None = None,
    note: str | None = None,
    ref: str | None = None,
    allow_overdraft: bool = False,
) -> None:
    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        found = repos.accounts.resolve(account)
        counter_account = repos.accounts.resolve(counter) if counter else None
        when = resolve_date(on, ctx)

        if counter_account is not None and counter_account.account_id == found.account_id:
            raise ValidationError(
                "cannot transfer an account to itself",
                remedy="Name a different destination with --to.",
                account=found.name,
            )

        service = TradingService(repos)
        txn = service.record_cash(
            found,
            txn_type,
            money_arg(amount, what="--amount"),
            when,
            counter_account=counter_account,
            fee_class=FeeClass(fee_class) if fee_class else None,
            note=note,
            external_ref=ref,
            allow_overdraft=allow_overdraft,
        )

        payload = {
            "account": found.name,
            "type": str(txn_type),
            "amount": quantize_money(money_arg(amount, what="--amount")),
            "net_cash_effect": txn.net_cash_effect,
            "date": when.isoformat(),
            **({"to_account": counter_account.name} if counter_account else {}),
        }
        if ctx.dry_run:
            return maybe_dry_run(CommandResult(command=f"cash {txn_type}", data=payload))

        with db_transaction(repos.con):
            txn_id = repos.transactions.append(txn)
            from dataclasses import replace

            rebuilt = service.replay.apply_or_rebuild(replace(txn, txn_id=txn_id))

        return CommandResult(
            command=f"cash {txn_type}",
            data={**payload, "txn_id": txn_id, **({"rebuilt": True} if rebuilt else {})},
            warnings=(_BACKDATED,) if rebuilt else (),
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@app.command()
def deposit(
    account: Annotated[str, typer.Option("--account", "-a")],
    amount: Annotated[str, typer.Option("--amount", help="Always positive.")],
    date_text: Annotated[str | None, typer.Option("--date", "-d")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
    ref: RefOpt = None,
) -> None:
    """Record capital entering the portfolio. An external cash flow at both levels."""
    _record(TransactionType.DEPOSIT, account, amount, date_text, note=note, ref=ref)


@app.command()
def withdraw(
    account: Annotated[str, typer.Option("--account", "-a")],
    amount: Annotated[str, typer.Option("--amount", help="Always positive.")],
    date_text: Annotated[str | None, typer.Option("--date", "-d")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
    ref: RefOpt = None,
    allow_overdraft: Annotated[bool, typer.Option("--allow-overdraft")] = False,
) -> None:
    """Record capital leaving the portfolio."""
    _record(
        TransactionType.WITHDRAWAL,
        account,
        amount,
        date_text,
        note=note,
        ref=ref,
        allow_overdraft=allow_overdraft,
    )


@app.command()
def transfer(
    account: Annotated[str, typer.Option("--from", "-a", help="Source account.")],
    to: Annotated[str, typer.Option("--to", help="Destination account.")],
    amount: Annotated[str, typer.Option("--amount", help="Always positive.")],
    date_text: Annotated[str | None, typer.Option("--date", "-d")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
    ref: RefOpt = None,
) -> None:
    """Move cash between two accounts in this portfolio.

    Recorded as ONE ledger entry with a counter account, not as a withdrawal
    plus a deposit. That is what makes portfolio-level netting structural: the
    transfer is an external cash flow at account level and **no flow at all**
    at portfolio level, rather than two flows that happen to cancel (ADR 0007,
    PORT-GIPS-B02).

    Entered as a withdrawal and a deposit instead, it would produce two genuine
    external flows at portfolio level -- a true statement about what was
    recorded and a false one about what happened.
    """
    _record(
        TransactionType.TRANSFER, account, amount, date_text, counter=to, note=note, ref=ref
    )


@app.command()
def interest(
    account: Annotated[str, typer.Option("--account", "-a")],
    amount: Annotated[str, typer.Option("--amount")],
    date_text: Annotated[str | None, typer.Option("--date", "-d")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
    ref: RefOpt = None,
) -> None:
    """Record interest received. Income -- never an external cash flow."""
    _record(TransactionType.INTEREST, account, amount, date_text, note=note, ref=ref)


@app.command()
def fee(
    account: Annotated[str, typer.Option("--account", "-a")],
    amount: Annotated[str, typer.Option("--amount")],
    fee_class: Annotated[
        str,
        typer.Option(
            "--fee-class",
            help=(
                "transaction_cost | embedded_fund_fee | external_mgmt_fee | "
                "internal_mgmt_cost | other_admin. Required -- the three return "
                "bases are derived from it (PORT-GIPS-D01)."
            ),
        ),
    ],
    date_text: Annotated[str | None, typer.Option("--date", "-d")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
    ref: RefOpt = None,
) -> None:
    """Record a fee.

    The classification is required and is not guessed. A custody fee is
    `internal_mgmt_cost` under the Asset Owner ladder portable follows -- it
    reduces net-of-fees returns only, and is **not** a transaction cost in
    either regime.
    """
    _record(
        TransactionType.FEE,
        account,
        amount,
        date_text,
        fee_class=fee_class,
        note=note,
        ref=ref,
    )


@app.command(name="margin-interest")
def margin_interest(
    account: Annotated[str, typer.Option("--account", "-a")],
    amount: Annotated[str, typer.Option("--amount")],
    date_text: Annotated[str | None, typer.Option("--date", "-d")] = None,
    ref: RefOpt = None,
) -> None:
    """Record margin interest.

    A financing cost rather than a fee: GIPS is silent, and portable treats it
    as reducing return in all three bases, with a disclosure saying so.
    """
    _record(
        TransactionType.MARGIN_INTEREST,
        account,
        amount,
        date_text,
        fee_class="internal_mgmt_cost",
        ref=ref,
    )


# ── income ───────────────────────────────────────────────────────────────────


def _income(
    txn_type: TransactionType,
    symbol: str,
    account: str,
    amount: str,
    *,
    ex_date: str | None,
    pay_date: str | None,
    qualified: bool | None,
    note: str | None,
    ref: str | None = None,
    withheld: str | None = None,
    reclaimable: str | None = None,
    reinvest_units: str | None = None,
) -> None:
    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        found = repos.accounts.resolve(account)
        pay = resolve_date(pay_date, ctx, what="--pay-date")
        ex = resolve_date(ex_date, ctx, what="--ex-date") if ex_date else pay
        gross = money_arg(amount, what="--amount")
        instrument = repos.instruments.resolve(symbol, on=ex)

        service = TradingService(repos)
        txn = service.record_income(
            found,
            instrument,
            txn_type,
            gross,
            pay,
            ex_date=ex,
            taxes_withheld=(
                money_arg(withheld, what="--withheld") if withheld else Decimal("0.00")
            ),
            withholding_reclaimable=(
                money_arg(reclaimable, what="--reclaimable") if reclaimable else None
            ),
            is_qualified=qualified,
            reinvested_units=(
                money_arg(reinvest_units, what="--reinvest-units") if reinvest_units else None
            ),
            note=note,
            external_ref=ref,
        )

        payload: dict[str, object] = {
            "symbol": instrument.symbol,
            "account": found.name,
            "type": str(txn_type),
            "amount": quantize_money(gross),
            "ex_date": ex.isoformat(),
            "pay_date": pay.isoformat(),
            "qualified": qualified,
        }
        if txn.quantity is not None:
            payload["reinvested_units"] = txn.quantity
            payload["price"] = txn.price
        if txn.taxes_withheld:
            # Both figures, because they answer different questions: the return
            # is earned on the gross and the cash balance moved by the net.
            payload["taxes_withheld"] = txn.taxes_withheld
            payload["net_cash_effect"] = txn.net_cash_effect
            if txn.withholding_reclaimable is not None:
                payload["withholding_reclaimable"] = txn.withholding_reclaimable
        if ctx.dry_run:
            return maybe_dry_run(CommandResult(command=f"income {txn_type}", data=payload))

        with db_transaction(repos.con):
            txn_id = repos.transactions.append(txn)
            from dataclasses import replace

            rebuilt = service.replay.apply_or_rebuild(replace(txn, txn_id=txn_id))

        return CommandResult(
            command=f"income {txn_type}",
            data={**payload, "txn_id": txn_id, **({"rebuilt": True} if rebuilt else {})},
            warnings=(_BACKDATED,) if rebuilt else (),
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@income_app.command()
def dividend(
    symbol: Annotated[str, typer.Argument()],
    account: Annotated[str, typer.Option("--account", "-a")],
    amount: Annotated[str, typer.Option("--amount", help="Total cash received.")],
    ex_date: Annotated[
        str | None,
        typer.Option("--ex-date", help="Entitlement date. Drives the accrual."),
    ] = None,
    pay_date: Annotated[
        str | None, typer.Option("--pay-date", help="When the cash arrived.")
    ] = None,
    qualified: Annotated[
        bool, typer.Option("--qualified/--non-qualified", help="Tax character.")
    ] = True,
    note: Annotated[str | None, typer.Option("--note")] = None,
    ref: RefOpt = None,
    withheld: WithheldOpt = None,
    reclaimable: ReclaimableOpt = None,
    reinvest_units: Annotated[
        str | None,
        typer.Option(
            "--reinvest-units",
            help=(
                "Units bought with the distribution. The row is then income and a "
                "lot in one, and moves no cash."
            ),
        ),
    ] = None,
) -> None:
    """Record a cash dividend, or one reinvested into units.

    Both dates are recorded because they answer different questions:
    entitlement is fixed on the **ex-date**, cash arrives on the **pay-date**,
    and accruing on the wrong one shifts return across a period boundary
    (PORT-GIPS-A06).

    With `--reinvest-units` the gross is the income earned and the cost of the
    units, the row opens a lot, and the cash balance is untouched. That is
    one event, recorded once: a dividend plus a buy would put a pair of
    movements in the cash ledger that never happened.
    """
    _income(
        TransactionType.DIVIDEND_REINVEST if reinvest_units else TransactionType.DIVIDEND,
        symbol,
        account,
        amount,
        ex_date=ex_date,
        pay_date=pay_date,
        qualified=qualified,
        note=note,
        ref=ref,
        withheld=withheld,
        reclaimable=reclaimable,
        reinvest_units=reinvest_units,
    )


@income_app.command()
def coupon(
    symbol: Annotated[str, typer.Argument()],
    account: Annotated[str, typer.Option("--account", "-a")],
    amount: Annotated[str, typer.Option("--amount")],
    pay_date: Annotated[str | None, typer.Option("--pay-date")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
    ref: RefOpt = None,
    withheld: WithheldOpt = None,
    reclaimable: ReclaimableOpt = None,
) -> None:
    """Record a bond coupon. Income -- never an external cash flow."""
    _income(
        TransactionType.COUPON,
        symbol,
        account,
        amount,
        ex_date=None,
        pay_date=pay_date,
        qualified=None,
        note=note,
        ref=ref,
        withheld=withheld,
        reclaimable=reclaimable,
    )


@income_app.command()
def roc(
    symbol: Annotated[str, typer.Argument()],
    account: Annotated[str, typer.Option("--account", "-a")],
    amount: Annotated[str, typer.Option("--amount")],
    pay_date: Annotated[str | None, typer.Option("--pay-date")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
    ref: RefOpt = None,
) -> None:
    """Record a return of capital.

    Two facts, both true and about different questions: for **tax** it reduces
    basis and is not income, and once basis reaches zero the excess is capital
    gain. For **performance** it is not an external cash flow. Conflating them
    gets both wrong.
    """
    _income(
        TransactionType.RETURN_OF_CAPITAL,
        symbol,
        account,
        amount,
        ex_date=None,
        pay_date=pay_date,
        qualified=None,
        note=note,
        ref=ref,
    )
