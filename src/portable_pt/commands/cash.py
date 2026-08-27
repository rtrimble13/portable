"""Cash and income: deposits, withdrawals, transfers, fees, dividends, coupons."""

from __future__ import annotations

from typing import Annotated

import typer

from portable_core.decimals import quantize_money
from portable_core.domain.enums import FeeClass, TransactionType
from portable_core.errors import ValidationError
from portable_core.formatters import CommandResult
from portable_core.persistence.connection import transaction as db_transaction
from portable_core.services.trading import TradingService
from portable_pt import state
from portable_pt.commands._shared import dispatch, maybe_dry_run, money_arg, resolve_date

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

            service.replay.apply_transaction(replace(txn, txn_id=txn_id))

        return CommandResult(
            command=f"cash {txn_type}",
            data={**payload, "txn_id": txn_id},
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@app.command()
def deposit(
    account: Annotated[str, typer.Option("--account", "-a")],
    amount: Annotated[str, typer.Option("--amount", help="Always positive.")],
    date_text: Annotated[str | None, typer.Option("--date", "-d")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
    ref: Annotated[str | None, typer.Option("--ref")] = None,
) -> None:
    """Record capital entering the portfolio. An external cash flow at both levels."""
    _record(TransactionType.DEPOSIT, account, amount, date_text, note=note, ref=ref)


@app.command()
def withdraw(
    account: Annotated[str, typer.Option("--account", "-a")],
    amount: Annotated[str, typer.Option("--amount", help="Always positive.")],
    date_text: Annotated[str | None, typer.Option("--date", "-d")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
    ref: Annotated[str | None, typer.Option("--ref")] = None,
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
    _record(TransactionType.TRANSFER, account, amount, date_text, counter=to, note=note)


@app.command()
def interest(
    account: Annotated[str, typer.Option("--account", "-a")],
    amount: Annotated[str, typer.Option("--amount")],
    date_text: Annotated[str | None, typer.Option("--date", "-d")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Record interest received. Income -- never an external cash flow."""
    _record(TransactionType.INTEREST, account, amount, date_text, note=note)


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
) -> None:
    """Record a fee.

    The classification is required and is not guessed. A custody fee is
    `internal_mgmt_cost` under the Asset Owner ladder portable follows -- it
    reduces net-of-fees returns only, and is **not** a transaction cost in
    either regime.
    """
    _record(TransactionType.FEE, account, amount, date_text, fee_class=fee_class, note=note)


@app.command(name="margin-interest")
def margin_interest(
    account: Annotated[str, typer.Option("--account", "-a")],
    amount: Annotated[str, typer.Option("--amount")],
    date_text: Annotated[str | None, typer.Option("--date", "-d")] = None,
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
) -> None:
    def action() -> CommandResult:
        from datetime import UTC, datetime

        from portable_core.domain.models import Transaction

        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        found = repos.accounts.resolve(account)
        pay = resolve_date(pay_date, ctx, what="--pay-date")
        ex = resolve_date(ex_date, ctx, what="--ex-date") if ex_date else pay
        gross = money_arg(amount, what="--amount")
        instrument = repos.instruments.resolve(symbol, on=ex)

        if ex > pay:
            raise ValidationError(
                f"ex-date {ex.isoformat()} is after pay-date {pay.isoformat()}",
                remedy=(
                    "Entitlement is fixed on the ex-date and cash arrives on the "
                    "pay-date, so the ex-date comes first."
                ),
            )

        # Recognition is on the pay date; the ex-date drives the accrual that
        # ValuationEngine picks up between the two (PORT-GIPS-A06).
        txn = Transaction(
            txn_id=0,
            account_id=found.account_id,
            trade_date=pay,
            seq=repos.transactions.next_seq(pay),
            txn_type=txn_type,
            net_cash_effect=quantize_money(gross),
            instrument_id=instrument.instrument_id,
            gross_amount=quantize_money(gross),
            ex_date=ex,
            pay_date=pay,
            is_qualified=qualified,
            note=note,
            created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        payload = {
            "symbol": instrument.symbol,
            "account": found.name,
            "type": str(txn_type),
            "amount": quantize_money(gross),
            "ex_date": ex.isoformat(),
            "pay_date": pay.isoformat(),
            "qualified": qualified,
        }
        if ctx.dry_run:
            return maybe_dry_run(CommandResult(command=f"income {txn_type}", data=payload))

        with db_transaction(repos.con):
            txn_id = repos.transactions.append(txn)
            from dataclasses import replace

            TradingService(repos).replay.apply_transaction(replace(txn, txn_id=txn_id))

        return CommandResult(
            command=f"income {txn_type}",
            data={**payload, "txn_id": txn_id},
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
) -> None:
    """Record a cash dividend.

    Both dates are recorded because they answer different questions:
    entitlement is fixed on the **ex-date**, cash arrives on the **pay-date**,
    and accruing on the wrong one shifts return across a period boundary
    (PORT-GIPS-A06).
    """
    _income(
        TransactionType.DIVIDEND,
        symbol,
        account,
        amount,
        ex_date=ex_date,
        pay_date=pay_date,
        qualified=qualified,
        note=note,
    )


@income_app.command()
def coupon(
    symbol: Annotated[str, typer.Argument()],
    account: Annotated[str, typer.Option("--account", "-a")],
    amount: Annotated[str, typer.Option("--amount")],
    pay_date: Annotated[str | None, typer.Option("--pay-date")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
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
    )


@income_app.command()
def roc(
    symbol: Annotated[str, typer.Argument()],
    account: Annotated[str, typer.Option("--account", "-a")],
    amount: Annotated[str, typer.Option("--amount")],
    pay_date: Annotated[str | None, typer.Option("--pay-date")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
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
    )
