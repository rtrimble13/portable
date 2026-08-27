"""Account definition and maintenance."""

from __future__ import annotations

from typing import Annotated

import typer

from portable_core.decimals import from_text
from portable_core.domain.enums import AccountType, CashTreatment, ReliefMethod
from portable_core.domain.models import Account, TaxRateSchedule
from portable_core.errors import ValidationError
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.persistence.connection import transaction
from portable_pt import state
from portable_pt.commands._shared import dispatch, maybe_dry_run, resolve_date

app = typer.Typer(help="Accounts: definition, status, and tax rates.", no_args_is_help=True)
rates_app = typer.Typer(help="Effective-dated tax rates.", no_args_is_help=True)
app.add_typer(rates_app, name="tax-rates")

_TYPES = {
    "taxable": AccountType.TAXABLE,
    "tax-deferred": AccountType.TAX_DEFERRED,
    "tax_deferred": AccountType.TAX_DEFERRED,
    "tax-exempt": AccountType.TAX_EXEMPT,
    "tax_exempt": AccountType.TAX_EXEMPT,
}


@app.command()
def add(
    name: Annotated[str, typer.Option("--name", help="Unique account name.")],
    account_type: Annotated[
        str, typer.Option("--type", help="taxable | tax-deferred | tax-exempt")
    ],
    opened: Annotated[str | None, typer.Option("--opened", help="YYYY-MM-DD.")] = None,
    custodian: Annotated[str | None, typer.Option("--custodian")] = None,
    alias: Annotated[
        str | None,
        typer.Option("--alias", help="An account number ALIAS, never the real number."),
    ] = None,
    relief_method: Annotated[
        str,
        typer.Option(
            "--relief-method",
            help="Default lot relief: spec, fifo, lifo, hifo, lofo, avg.",
        ),
    ] = "spec",
    cash_treatment: Annotated[
        str,
        typer.Option(
            "--cash-treatment",
            help=(
                "invested (default) or operating. Operating cash is excluded from "
                "returns, and only ever by this explicit flag (PORT-GIPS-A07)."
            ),
        ),
    ] = "invested",
    allows_fractional: Annotated[
        bool,
        typer.Option("--allows-fractional", help="The custodian permits fractional shares."),
    ] = False,
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Add an account."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()

        if account_type not in _TYPES:
            raise ValidationError(
                f"unknown account type {account_type!r}",
                remedy="Choose one of: taxable, tax-deferred, tax-exempt.",
                value=account_type,
            )

        account = Account(
            account_id=0,
            name=name,
            account_type=_TYPES[account_type],
            opened_date=resolve_date(opened, ctx, what="--opened"),
            custodian=custodian,
            account_alias=alias,
            cash_treatment=CashTreatment(cash_treatment),
            default_relief_method=ReliefMethod(relief_method),
            allows_fractional=allows_fractional,
            note=note,
        )

        if ctx.dry_run:
            return maybe_dry_run(
                CommandResult(command="account add", data=_account_row(account))
            )

        with transaction(repos.con):
            account_id = repos.accounts.add(account)

        return CommandResult(
            command="account add",
            data={**_account_row(account), "account_id": account_id},
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


def _account_row(account: Account) -> dict[str, object]:
    return {
        "name": account.name,
        "account_type": str(account.account_type),
        "custodian": account.custodian,
        "opened_date": account.opened_date.isoformat(),
        "cash_treatment": str(account.cash_treatment),
        "default_relief_method": str(account.default_relief_method),
        "allows_fractional": account.allows_fractional,
        "status": str(account.status),
    }


@app.command(name="list")
def list_accounts(
    include_closed: Annotated[bool, typer.Option("--include-closed")] = True,
) -> None:
    """List accounts."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        accounts = repos.accounts.all(include_closed=include_closed)

        return CommandResult(
            command="account list",
            table=Table(
                columns=(
                    Column("account_id", "Id", ColumnKind.INTEGER),
                    Column("name", "Name", ColumnKind.TEXT),
                    Column("account_type", "Type", ColumnKind.TEXT),
                    Column("custodian", "Custodian", ColumnKind.TEXT),
                    Column("cash", "Cash", ColumnKind.MONEY),
                    Column("margin", "Margin Loan", ColumnKind.MONEY),
                    Column("relief", "Relief", ColumnKind.TEXT),
                    Column("cash_treatment", "Cash", ColumnKind.TEXT),
                    Column("status", "Status", ColumnKind.TEXT),
                ),
                rows=tuple(
                    {
                        "account_id": a.account_id,
                        "name": a.name,
                        "account_type": str(a.account_type),
                        "custodian": a.custodian,
                        "cash": repos.valuations.cash(a.account_id, currency=a.currency)[0],
                        "margin": repos.valuations.cash(a.account_id, currency=a.currency)[1],
                        "relief": str(a.default_relief_method),
                        "cash_treatment": str(a.cash_treatment),
                        "status": str(a.status),
                    }
                    for a in accounts
                ),
                title="Accounts",
            ),
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@app.command()
def show(account: Annotated[str, typer.Argument(help="Account name or id.")]) -> None:
    """Show one account, with its tax rate history."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        found = repos.accounts.resolve(account)
        schedules = repos.accounts.rate_schedules(found.account_id)
        cash, margin = repos.valuations.cash(found.account_id, currency=found.currency)

        return CommandResult(
            command="account show",
            table=Table(
                columns=(
                    Column("effective_from", "Effective From", ColumnKind.DATE),
                    Column("short_term_federal", "ST Federal", ColumnKind.RATE),
                    Column("long_term_federal", "LT Federal", ColumnKind.RATE),
                    Column("state", "State", ColumnKind.RATE),
                    Column("niit", "NIIT", ColumnKind.RATE),
                ),
                rows=tuple(
                    {
                        "effective_from": s.effective_from.isoformat(),
                        "short_term_federal": s.short_term_federal,
                        "long_term_federal": s.long_term_federal,
                        "state": s.state,
                        "niit": s.niit,
                    }
                    for s in schedules
                ),
                title=f"Tax rates for {found.name}",
                footnotes=(
                    "Rates are effective-dated: a change never restates a past "
                    "disposition. Components are separate so the effective rate is "
                    "explainable rather than a magic number.",
                ),
            ),
            data={
                **_account_row(found),
                "account_id": found.account_id,
                "cash": cash,
                "margin_loan": margin,
                "alias": found.account_alias,
                "note": found.note,
            },
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@app.command()
def close(
    account: Annotated[str, typer.Argument(help="Account name or id.")],
    on: Annotated[str | None, typer.Option("--date", "-d")] = None,
) -> None:
    """Close an account. History is retained; nothing is deleted."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        found = repos.accounts.resolve(account)
        closing_date = resolve_date(on, ctx)

        if ctx.dry_run:
            return maybe_dry_run(
                CommandResult(
                    command="account close",
                    data={"account": found.name, "closed_date": closing_date.isoformat()},
                )
            )
        if not ctx.confirm(f"Close account {found.name!r} on {closing_date.isoformat()}?"):
            raise ValidationError("cancelled", remedy="Pass --yes to confirm.")

        with transaction(repos.con):
            repos.accounts.close(found.account_id, closing_date)

        return CommandResult(
            command="account close",
            data={"account": found.name, "closed_date": closing_date.isoformat()},
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@rates_app.command(name="set")
def set_rates(
    account: Annotated[str, typer.Option("--account", "-a")],
    short: Annotated[str, typer.Option("--short", help="Short-term federal rate, e.g. 0.37")],
    long: Annotated[str, typer.Option("--long", help="Long-term federal rate, e.g. 0.20")],
    effective_from: Annotated[str, typer.Option("--effective-from", help="YYYY-MM-DD.")],
    state_rate: Annotated[str, typer.Option("--state")] = "0",
    niit: Annotated[str, typer.Option("--niit", help="Net investment income tax.")] = "0",
    qualified_dividend: Annotated[str | None, typer.Option("--qualified-dividend")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Set an effective-dated tax rate schedule.

    Components are stored separately -- federal, state, NIIT -- so the effective
    rate on a report is explainable arithmetic rather than a number the user
    has to take on trust.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        found = repos.accounts.resolve(account)

        schedule = TaxRateSchedule(
            rate_id=0,
            account_id=found.account_id,
            effective_from=resolve_date(effective_from, ctx, what="--effective-from"),
            short_term_federal=from_text(short),
            long_term_federal=from_text(long),
            state=from_text(state_rate),
            niit=from_text(niit),
            qualified_dividend=from_text(qualified_dividend) if qualified_dividend else None,
            note=note,
        )

        payload = {
            "account": found.name,
            "effective_from": schedule.effective_from.isoformat(),
            "short_term_federal": schedule.short_term_federal,
            "long_term_federal": schedule.long_term_federal,
            "state": schedule.state,
            "niit": schedule.niit,
            "effective_short_rate": schedule.effective_rate(
                __import__(
                    "portable_core.domain.enums", fromlist=["HoldingPeriod"]
                ).HoldingPeriod.SHORT
            ),
        }

        if ctx.dry_run:
            return maybe_dry_run(CommandResult(command="account tax-rates set", data=payload))

        with transaction(repos.con):
            rate_id = repos.accounts.add_rate_schedule(schedule)

        return CommandResult(
            command="account tax-rates set",
            data={**payload, "rate_id": rate_id},
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)
