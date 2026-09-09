"""Reporting: holdings, pnl, tax, activity, cash-flows, reconcile, policy."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from portable_core.decimals import from_text, money_context, quantize_money
from portable_core.disclaimer import TAX_DISCLAIMER
from portable_core.domain.enums import FlowLevel, TransactionType
from portable_core.domain.models import ReturnPolicy
from portable_core.errors import GipsRefusalError, ReconciliationBreakError, ValidationError
from portable_core.errors.kinds import E_GIPS_NO_FLOW_POLICY, E_RECONCILE_BREAK
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.persistence.connection import transaction as db_transaction
from portable_core.services import cash_flow
from portable_core.services.reconciliation import (
    ExternalHolding,
    ReconciliationService,
)
from portable_core.services.tax import TaxEngine
from portable_pt import state
from portable_pt.commands._shared import dispatch, maybe_dry_run, money_arg, resolve_date

policy_app = typer.Typer(help="Return policy thresholds.", no_args_is_help=True)

ZERO = Decimal("0.00")


# ── holdings ─────────────────────────────────────────────────────────────────


def holdings(
    account: Annotated[str | None, typer.Option("--account", "-a")] = None,
    by: Annotated[
        str,
        typer.Option("--by", help="account | position | instrument | sector | asset-class"),
    ] = "instrument",
) -> None:
    """What the portfolio holds, as of --as-of."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        accounts = [repos.accounts.resolve(account)] if account else repos.accounts.all()

        rows: list[dict[str, object]] = []
        warnings: list[str] = []
        with money_context():
            total = ZERO
            for target in accounts:
                for position in repos.positions.all(
                    account_id=target.account_id, open_only=True
                ):
                    for leg in position.legs:
                        if leg.quantity == 0:
                            continue
                        instrument = repos.instruments.get(leg.instrument_id)
                        if instrument is None:
                            continue
                        price = repos.prices.newest_on_or_before(
                            instrument.instrument_id, ctx.as_of
                        )
                        lots = repos.lots.by_leg(leg.leg_id)
                        basis = sum((lot.adjusted_cost_basis for lot in lots), ZERO)
                        value = (
                            quantize_money(
                                price.price * leg.quantity * instrument.contract_size
                            )
                            if price
                            else None
                        )
                        if price is None:
                            warnings.append(
                                f"{instrument.symbol}: no price on or before "
                                f"{ctx.as_of.isoformat()}; market value shown as null, "
                                "not as zero."
                            )
                        else:
                            total += value or ZERO
                        rows.append(
                            {
                                "account": target.name,
                                "position_id": position.position_id,
                                "symbol": instrument.symbol,
                                "sector": instrument.sector,
                                "asset_class": instrument.asset_class,
                                "role": str(leg.role),
                                "quantity": leg.quantity,
                                "price": price.price if price else None,
                                "price_date": (price.price_date.isoformat() if price else None),
                                "market_value": value,
                                "cost_basis": basis,
                                "unrealized": (
                                    quantize_money(value - basis) if value is not None else None
                                ),
                            }
                        )

                cash, margin = repos.valuations.cash(
                    target.account_id, currency=target.currency
                )
                if cash != 0 or margin != 0:
                    total += cash - margin
                    rows.append(
                        {
                            "account": target.name,
                            "position_id": None,
                            "symbol": "CASH",
                            "sector": None,
                            "asset_class": "cash",
                            "role": "cash",
                            "quantity": None,
                            "price": None,
                            "price_date": None,
                            "market_value": quantize_money(cash - margin),
                            "cost_basis": quantize_money(cash - margin),
                            "unrealized": ZERO,
                        }
                    )

            for row in rows:
                row_value = row.get("market_value")
                row["weight"] = (
                    (row_value / total) if isinstance(row_value, Decimal) and total else None
                )

        rows = _group_holdings(rows, by)

        return CommandResult(
            command="holdings",
            table=Table(
                columns=_holdings_columns(by),
                rows=tuple(rows),
                title=f"Holdings as of {ctx.as_of.isoformat()}",
                footnotes=(
                    "Cash is included: it is always in the return unless an account "
                    "is explicitly designated operating cash (PORT-GIPS-A07).",
                    "A missing price renders as null, never as zero.",
                ),
            ),
            data={"total_market_value": quantize_money(total), "grouped_by": by},
            warnings=tuple(warnings),
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


def _holdings_columns(by: str) -> tuple[Column, ...]:
    """Columns that match the grouping.

    A grouped row has no single quantity, price, or role, so those columns are
    dropped rather than rendered empty -- an em dash in every cell of a column
    invites the reader to wonder what is missing, when the answer is that the
    question does not apply.
    """
    money = (
        Column("market_value", "Market Value", ColumnKind.MONEY),
        Column("cost_basis", "Cost", ColumnKind.MONEY),
        Column("unrealized", "Unrealized", ColumnKind.MONEY),
        Column("weight", "Weight", ColumnKind.RATE),
    )
    if by == "instrument":
        return (
            Column("account", "Account"),
            Column("symbol", "Symbol"),
            Column("role", "Role"),
            Column("quantity", "Qty", ColumnKind.QUANTITY),
            Column("price", "Price", ColumnKind.PRICE),
            *money,
        )
    headers = {
        "account": "Account",
        "position": "Position",
        "sector": "Sector",
        "asset-class": "Asset Class",
        "asset_class": "Asset Class",
    }
    return (
        Column("symbol", headers.get(by, by.title())),
        Column("holdings", "Holdings", ColumnKind.INTEGER),
        *money,
    )


def _group_holdings(rows: list[dict[str, object]], by: str) -> list[dict[str, object]]:
    """Aggregate holdings by the requested dimension.

    `--by instrument` (the default) is the ungrouped view, one row per leg.
    Anything else sums market value, cost and unrealized P&L into one row per
    group, and blanks the fields that stop meaning anything once summed: a
    group has no single quantity or price, and rendering the first member's is
    worse than rendering nothing.
    """
    keys = {
        "instrument": None,
        "account": "account",
        "position": "position_id",
        "sector": "sector",
        "asset-class": "asset_class",
        "asset_class": "asset_class",
    }
    if by not in keys:
        raise ValidationError(
            f"unknown grouping {by!r}",
            remedy="Choose one of: account, position, instrument, sector, asset-class.",
            value=by,
            choices=sorted(set(keys)),
        )

    key = keys[by]
    if key is None:
        return sorted(rows, key=lambda r: (str(r["account"]), str(r["symbol"])))

    grouped: dict[str, dict[str, object]] = {}
    with money_context():
        for row in rows:
            # An ungrouped attribute is "(unclassified)", not blank: a holding
            # with no sector is a real holding and dropping it would make the
            # totals disagree with `--by instrument`.
            label = str(row.get(key) or "(unclassified)")
            bucket = grouped.setdefault(
                label,
                {
                    by: label,
                    "account": label if key == "account" else None,
                    "symbol": label,
                    "role": None,
                    "quantity": None,
                    "price": None,
                    "market_value": ZERO,
                    "cost_basis": ZERO,
                    "unrealized": ZERO,
                    "weight": None,
                    "holdings": 0,
                },
            )
            count = bucket["holdings"]
            bucket["holdings"] = (count if isinstance(count, int) else 0) + 1
            for field in ("market_value", "cost_basis", "unrealized"):
                value = row.get(field)
                running = bucket[field]
                if isinstance(value, Decimal) and isinstance(running, Decimal):
                    bucket[field] = running + value

        total = sum(
            (
                b["market_value"]
                for b in grouped.values()
                if isinstance(b["market_value"], Decimal)
            ),
            ZERO,
        )
        for bucket in grouped.values():
            value = bucket["market_value"]
            bucket["weight"] = value / total if isinstance(value, Decimal) and total else None

    return sorted(grouped.values(), key=lambda r: str(r["symbol"]))


# ── pnl ──────────────────────────────────────────────────────────────────────


def pnl(
    account: Annotated[str | None, typer.Option("--account", "-a")] = None,
    year: Annotated[int | None, typer.Option("--year")] = None,
    net_of_tax: Annotated[
        bool, typer.Option("--net-of-tax", help="Deduct the estimated tax liability.")
    ] = False,
) -> None:
    """Realized and unrealized P&L.

    Realized gain is exact. The tax estimate deducted by --net-of-tax is not --
    see `pt tax` and docs/tax-methodology.md for what it does and does not
    model.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        account_id = repos.accounts.resolve(account).account_id if account else None
        gains = repos.lots.realized_gains(tax_year=year, account_id=account_id)

        rows: list[dict[str, object]] = []
        with money_context():
            realized = ZERO
            estimated_tax = ZERO
            for gain in gains:
                instrument = repos.instruments.get(gain.instrument_id)
                realized += gain.gain
                estimated_tax += gain.estimated_tax or ZERO
                rows.append(
                    {
                        "date": gain.disposition_date.isoformat(),
                        "symbol": instrument.symbol if instrument else None,
                        "holding_period": str(gain.holding_period),
                        "proceeds": gain.proceeds,
                        "cost_basis": gain.cost_basis,
                        "gain": gain.gain,
                        "estimated_tax": gain.estimated_tax,
                        "net_of_tax": TaxEngine.net_of_tax(gain.gain, gain.estimated_tax),
                    }
                )

            unrealized = ZERO
            unpriced: list[str] = []
            accounts = [repos.accounts.resolve(account)] if account else repos.accounts.all()
            for target in accounts:
                for position in repos.positions.all(
                    account_id=target.account_id, open_only=True
                ):
                    for leg in position.legs:
                        instrument = repos.instruments.get(leg.instrument_id)
                        if instrument is None or leg.quantity == 0:
                            continue
                        price = repos.prices.newest_on_or_before(
                            instrument.instrument_id, ctx.as_of
                        )
                        if price is None:
                            unpriced.append(instrument.symbol)
                            continue
                        lots = repos.lots.by_leg(leg.leg_id)
                        basis = sum((lot.adjusted_cost_basis for lot in lots), ZERO)
                        unrealized += (
                            price.price * leg.quantity * instrument.contract_size - basis
                        )

        return CommandResult(
            command="pnl",
            table=Table(
                columns=(
                    Column("date", "Date", ColumnKind.DATE),
                    Column("symbol", "Symbol"),
                    Column("holding_period", "Holding"),
                    Column("proceeds", "Proceeds", ColumnKind.MONEY),
                    Column("cost_basis", "Basis", ColumnKind.MONEY),
                    Column("gain", "Realized", ColumnKind.MONEY),
                    Column("estimated_tax", "Est. Tax", ColumnKind.MONEY),
                    Column("net_of_tax", "Net of Tax", ColumnKind.MONEY),
                ),
                rows=tuple(rows),
                title="Realized gains",
            ),
            data={
                "realized": quantize_money(realized),
                "unrealized": quantize_money(unrealized),
                "estimated_tax": quantize_money(estimated_tax),
                "realized_net_of_tax": (
                    quantize_money(realized - estimated_tax) if net_of_tax else None
                ),
                "unpriced_instruments": sorted(set(unpriced)),
            },
            warnings=(
                (
                    f"{len(set(unpriced))} instrument(s) could not be priced; "
                    "unrealized P&L excludes them rather than treating them as zero.",
                )
                if unpriced
                else ()
            ),
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


# ── tax ──────────────────────────────────────────────────────────────────────


def tax(
    year: Annotated[int | None, typer.Option("--year")] = None,
    account: Annotated[str | None, typer.Option("--account", "-a")] = None,
) -> None:
    """Realized gains by holding period, with estimated liability.

    Schedule-D-shaped. **This is an estimate and not tax advice**, and it does
    NOT account for wash sales -- the 30-day window spans every account the
    taxpayer has, including IRAs, and detection is deferred to v0.2. That
    statement travels with the output in every format and cannot be suppressed.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        tax_year = year or ctx.as_of.year
        account_id = repos.accounts.resolve(account).account_id if account else None
        gains = repos.lots.realized_gains(tax_year=tax_year, account_id=account_id)

        engine = TaxEngine()
        sheltered = tuple(a.name for a in repos.accounts.all() if not a.is_taxable)
        summary = engine.summarise(gains, tax_year, non_taxable_accounts=sheltered)

        rows: list[dict[str, object]] = []
        for gain in gains:
            instrument = repos.instruments.get(gain.instrument_id)
            target = repos.accounts.get(gain.account_id)
            rows.append(
                {
                    "date": gain.disposition_date.isoformat(),
                    "account": target.name if target else None,
                    "symbol": instrument.symbol if instrument else None,
                    "holding_period": str(gain.holding_period),
                    "proceeds": gain.proceeds,
                    "cost_basis": gain.cost_basis,
                    "gain": gain.gain,
                    "federal_rate": gain.federal_rate,
                    "state_rate": gain.state_rate,
                    "niit_rate": gain.niit_rate,
                    "estimated_tax": gain.estimated_tax,
                    "taxable": gain.is_taxable,
                }
            )

        return CommandResult(
            command="tax",
            table=Table(
                columns=(
                    Column("date", "Date", ColumnKind.DATE),
                    Column("account", "Account"),
                    Column("symbol", "Symbol"),
                    Column("holding_period", "Holding"),
                    Column("proceeds", "Proceeds", ColumnKind.MONEY),
                    Column("cost_basis", "Basis", ColumnKind.MONEY),
                    Column("gain", "Gain/Loss", ColumnKind.MONEY),
                    Column("federal_rate", "Fed", ColumnKind.RATE),
                    Column("state_rate", "State", ColumnKind.RATE),
                    Column("niit_rate", "NIIT", ColumnKind.RATE),
                    Column("estimated_tax", "Est. Tax", ColumnKind.MONEY),
                ),
                rows=tuple(rows),
                title=f"Realized gains, {tax_year}",
                footnotes=(
                    "Rate components are shown separately so the effective rate is "
                    "explainable rather than a magic number.",
                    "A blank estimated tax means the account is sheltered -- "
                    "inapplicable, not zero.",
                ),
            ),
            data={
                "tax_year": tax_year,
                "short_term_gain": summary.short_term_gain,
                "long_term_gain": summary.long_term_gain,
                "short_term_tax": summary.short_term_tax,
                "long_term_tax": summary.long_term_tax,
                "total_gain": summary.total_gain,
                "total_estimated_tax": summary.total_tax,
                "proceeds": summary.proceeds,
                "cost_basis": summary.cost_basis,
                "dispositions": summary.disposition_count,
                "sheltered_accounts": list(summary.non_taxable_accounts),
                "excludes_wash_sales": summary.excludes_wash_sales,
            },
            disclaimer=TAX_DISCLAIMER,
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


# ── activity ─────────────────────────────────────────────────────────────────


def activity(
    start: Annotated[str | None, typer.Option("--from")] = None,
    end: Annotated[str | None, typer.Option("--to")] = None,
    txn_type: Annotated[str | None, typer.Option("--type")] = None,
    account: Annotated[str | None, typer.Option("--account", "-a")] = None,
) -> None:
    """Ledger activity over a period."""

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        last = resolve_date(end, ctx, what="--to")
        first = resolve_date(start, ctx, what="--from") if start else date(last.year, 1, 1)
        account_id = repos.accounts.resolve(account).account_id if account else None

        transactions = [
            t
            for t in repos.transactions.in_ledger_order(until=last, account_id=account_id)
            if t.trade_date >= first and (txn_type is None or str(t.txn_type) == txn_type)
        ]

        rows = []
        for t in transactions:
            instrument = repos.instruments.get(t.instrument_id) if t.instrument_id else None
            target = repos.accounts.get(t.account_id)
            rows.append(
                {
                    "txn_id": t.txn_id,
                    "date": t.trade_date.isoformat(),
                    "account": target.name if target else None,
                    "type": str(t.txn_type),
                    "symbol": instrument.symbol if instrument else None,
                    "quantity": t.quantity,
                    "price": t.price,
                    "cash": t.net_cash_effect,
                    "fees": t.fees + t.commissions,
                    "note": t.note,
                }
            )

        return CommandResult(
            command="activity",
            table=Table(
                columns=(
                    Column("txn_id", "Id", ColumnKind.INTEGER),
                    Column("date", "Date", ColumnKind.DATE),
                    Column("account", "Account"),
                    Column("type", "Type"),
                    Column("symbol", "Symbol"),
                    Column("quantity", "Qty", ColumnKind.QUANTITY),
                    Column("price", "Price", ColumnKind.PRICE),
                    Column("cash", "Cash", ColumnKind.MONEY),
                    Column("fees", "Fees", ColumnKind.MONEY),
                    Column("note", "Note"),
                ),
                rows=tuple(rows),
                title=f"Activity {first.isoformat()} to {last.isoformat()}",
            ),
            data={"count": len(rows), "from": first.isoformat(), "to": last.isoformat()},
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


# ── cash flows ───────────────────────────────────────────────────────────────


def cash_flows(
    start: Annotated[str | None, typer.Option("--from")] = None,
    end: Annotated[str | None, typer.Option("--to")] = None,
    external_only: Annotated[
        bool,
        typer.Option(
            "--external-only",
            help="Only external flows. Income is excluded; transfers net at portfolio level.",
        ),
    ] = False,
    level: Annotated[str, typer.Option("--level", help="account | portfolio")] = "portfolio",
) -> None:
    """The cash-flow series `pert` will consume.

    **The level is the whole point.** An inter-account transfer is an external
    flow at *account* level and is **not** one at *portfolio* level, because it
    nets to zero. Income -- dividends, coupons, reinvestments, return of capital
    -- is never an external flow at either level.

    The classification comes from one service function and is not re-derived
    here (ADR 0007). The full matrix is PORT-GIPS-B02.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        try:
            flow_level = FlowLevel(level)
        except ValueError as exc:
            raise ValidationError(
                f"unknown level {level!r}",
                remedy="Choose account or portfolio. The answer differs between them.",
                value=level,
            ) from exc

        last = resolve_date(end, ctx, what="--to")
        first = resolve_date(start, ctx, what="--from") if start else date(last.year, 1, 1)

        rows: list[dict[str, object]] = []
        with money_context():
            total = ZERO
            for txn in repos.transactions.in_ledger_order(until=last):
                if txn.trade_date < first:
                    continue
                if txn.txn_type is TransactionType.REVERSAL and txn.reverses_txn_id:
                    original = repos.transactions.get(txn.reverses_txn_id)
                    classified = (
                        cash_flow.classify_reversal(txn, original, flow_level)
                        if original
                        else cash_flow.classify(txn, flow_level)
                    )
                else:
                    classified = cash_flow.classify(txn, flow_level)

                if external_only and not classified.is_external:
                    continue

                target = repos.accounts.get(txn.account_id)
                total += classified.amount
                rows.append(
                    {
                        "date": classified.flow_date.isoformat(),
                        "txn_id": txn.txn_id,
                        "account": target.name if target else None,
                        "type": str(txn.txn_type),
                        "classification": str(classified.classification),
                        "amount": classified.amount,
                        "in_kind": classified.is_in_kind,
                    }
                )

        return CommandResult(
            command="cash-flows",
            table=Table(
                columns=(
                    Column("date", "Date", ColumnKind.DATE),
                    Column("txn_id", "Txn", ColumnKind.INTEGER),
                    Column("account", "Account"),
                    Column("type", "Type"),
                    Column("classification", "Classification"),
                    Column("amount", "Amount", ColumnKind.MONEY),
                ),
                rows=tuple(rows),
                title=f"Cash flows at {flow_level} level",
                footnotes=(
                    "Income is never an external cash flow, at any level.",
                    "An inter-account transfer is external at account level and NOT "
                    "at portfolio level -- it nets to zero (PORT-GIPS-B02).",
                ),
            ),
            data={
                "level": str(flow_level),
                "external_only": external_only,
                "net_external_flow": quantize_money(total),
                "from": first.isoformat(),
                "to": last.isoformat(),
            },
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


# ── reconcile ────────────────────────────────────────────────────────────────


def reconcile(
    against: Annotated[
        Path, typer.Option("--against", help="The custodian's position statement, CSV.")
    ],
    account: Annotated[
        str | None,
        typer.Option("--account", "-a", help="Reconcile one account. Default: all of them."),
    ] = None,
    cash: Annotated[
        str | None,
        typer.Option(
            "--cash",
            help=(
                "The custodian's cash for --account, where the file does not carry it. "
                "Include any sweep or money-market balance (ADR 0013)."
            ),
        ),
    ] = None,
    tolerance: Annotated[
        str, typer.Option("--tolerance", help="Absolute, per line, for quantities and money.")
    ] = "0.01",
) -> None:
    """Compare holdings and cash against a custodian's statement, per account.

    **The file:** one row per line of the statement. `quantity` plus one of
    `symbol`, `cusip` or `isin`. Optionally `account` -- required once more than
    one account is being reconciled -- and `cash`, marking a row as the cash
    line or a sweep vehicle, whose amount is taken from `market_value` when
    present.

    Cash is compared, not only quantities. It is the only check that catches a
    sign error, a dropped row, or a double-counted transfer: the failures that
    leave every share count right and the money wrong.

    Cash equivalents are folded into cash before comparing, because a custodian
    reports its sweep as a position and `portable` holds it as cash (ADR 0013).

    A break beyond tolerance exits **6** -- a distinct code, because a
    reconciliation break is not a bug in portable and not a bad argument: it
    means the book and the custodian disagree, which needs a person.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        limit = money_arg(tolerance, what="--tolerance")

        if state.current_options().as_of is not None:
            # Reconciliation compares what the portfolio holds *now* against the
            # statement. There is no as-of position query, and silently ignoring
            # the flag would answer a question nobody asked.
            raise ValidationError(
                "`pt reconcile` compares current holdings, so --as-of does not apply",
                remedy=(
                    "Drop --as-of and reconcile against a current statement. Reconciling "
                    "to a past date needs positions as they stood then, which portable "
                    "does not yet reconstruct."
                ),
            )

        if not against.is_file():
            raise ValidationError(
                f"statement not found: {against}",
                remedy="Point --against at a CSV with quantity and an identifier column.",
                path=str(against),
            )

        external = _read_statement(against)
        targets = [repos.accounts.resolve(account)] if account else list(repos.accounts.all())
        if not targets:
            raise ValidationError(
                "the portfolio has no accounts to reconcile",
                remedy="Add one with `pt account add`.",
            )

        overrides: dict[str, Decimal] = {}
        if cash is not None:
            if account is None:
                raise ValidationError(
                    "--cash names one account's cash, so it needs --account",
                    remedy=(
                        "Reconcile one account at a time with --account, or put the cash "
                        "in the file as a row marked `cash`."
                    ),
                )
            overrides[targets[0].name] = money_arg(cash, what="--cash")

        outcome = ReconciliationService(repos).reconcile(
            external, targets, tolerance=limit, cash_override=overrides
        )

        rows = [
            {
                "account": line.account,
                "kind": line.kind,
                "identifier": line.identifier,
                "portable": line.ours,
                "custodian": line.theirs,
                "difference": line.difference,
                "break": line.is_break,
            }
            for line in outcome.lines
        ]
        result = CommandResult(
            command="reconcile",
            table=Table(
                columns=(
                    Column("account", "Account"),
                    Column("kind", "Kind"),
                    Column("identifier", "Identifier"),
                    Column("portable", "portable", ColumnKind.QUANTITY),
                    Column("custodian", "Custodian", ColumnKind.QUANTITY),
                    Column("difference", "Difference", ColumnKind.QUANTITY),
                    Column("break", "Break", ColumnKind.BOOL),
                ),
                rows=tuple(rows),
                title=f"Reconciliation against {against.name}",
            ),
            data={
                "breaks": len(outcome.breaks),
                "tolerance": limit,
                "lines": len(outcome.lines),
                "accounts": [target.name for target in targets],
            },
            portfolio=ctx.portfolio_name(),
        )

        if outcome.breaks:
            raise ReconciliationBreakError(
                f"{len(outcome.breaks)} reconciliation break(s) beyond a tolerance of {limit}",
                code=E_RECONCILE_BREAK,
                remedy=(
                    "Compare the ledger with the custodian's activity for the period. "
                    "A missing transaction is the usual cause; correct it with a new "
                    "entry rather than by editing history. A cash break with clean "
                    "quantities usually means a sign convention or a transfer counted "
                    "twice."
                ),
                breaks=[
                    f"{line.account} {line.kind} {line.identifier}: "
                    f"portable {line.ours} vs custodian {line.theirs}"
                    for line in outcome.breaks
                ],
            )
        return result

    dispatch(action)


_TRUE = frozenset({"1", "true", "yes", "y", "t"})


def _read_statement(path: Path) -> list[ExternalHolding]:
    """Parse a custodian statement into canonical lines.

    Deliberately forgiving about *which* identifier a custodian uses and strict
    about there being one: a row `portable` cannot identify is a row it cannot
    reconcile, and reporting it as absent would read as agreement.
    """
    rows: list[ExternalHolding] = []
    reader = csv.DictReader(path.read_text(encoding="utf-8").splitlines())
    for line_number, row in enumerate(reader, start=2):
        cleaned = {
            (key or "").strip().lower(): (value or "").strip() for key, value in row.items()
        }
        identifier = cleaned.get("symbol") or cleaned.get("cusip") or cleaned.get("isin") or ""
        is_cash = cleaned.get("cash", "").lower() in _TRUE
        if not identifier and not is_cash:
            raise ValidationError(
                f"{path}:{line_number}: no symbol, cusip or isin",
                remedy=(
                    "Every line needs an identifier, or a `cash` column marking it as the "
                    "cash line. A line that cannot be identified cannot be reconciled."
                ),
                path=str(path),
                line=line_number,
            )

        # A statement's cash row carries a value and no share count.
        raw = (cleaned.get("market_value") if is_cash else "") or cleaned.get("quantity", "")
        if not raw:
            raise ValidationError(
                f"{path}:{line_number}: no quantity" + (" or market_value" if is_cash else ""),
                remedy="Each line needs an amount to compare against.",
                path=str(path),
                line=line_number,
            )
        try:
            amount = from_text(raw.replace(",", "").replace("$", ""))
        except ValueError as exc:
            raise ValidationError(
                f"{path}:{line_number}: {raw!r} is not a decimal",
                remedy="Amounts are plain decimals; strip any currency formatting.",
                path=str(path),
                line=line_number,
            ) from exc

        rows.append(
            ExternalHolding(
                account=cleaned.get("account") or None,
                identifier=identifier.upper() or "CASH",
                amount=amount,
                is_cash_equivalent=is_cash,
            )
        )
    return rows


# ── policy ───────────────────────────────────────────────────────────────────


@policy_app.command(name="set")
def set_policy(
    effective_from: Annotated[str, typer.Option("--effective-from")],
    large_flow_pct: Annotated[
        str | None,
        typer.Option("--large-flow-pct", help="As a fraction, e.g. 0.10 for 10%."),
    ] = None,
    large_flow_amount: Annotated[
        str | None, typer.Option("--large-flow-amount", help="A currency amount.")
    ] = None,
    materiality_bps: Annotated[str | None, typer.Option("--materiality-bps")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Set the effective-dated return policy.

    GIPS defines "large cash flow" and requires the entity to define the
    *amount* -- it supplies no number (PORT-GIPS-B03). Until a policy exists,
    `pert` refuses to compute returns rather than defaulting to zero, because
    a defaulted threshold produces a plausible number that is wrong.

    Effective-dated, like tax rates, so a policy change never restates history.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()

        if (large_flow_pct is None) == (large_flow_amount is None):
            raise ValidationError(
                "specify exactly one of --large-flow-pct or --large-flow-amount",
                remedy=(
                    "GIPS requires the amount to be defined either as a percentage of "
                    "assets or as a currency value -- one or the other, not both and "
                    "not neither."
                ),
            )

        policy = ReturnPolicy(
            policy_id=0,
            effective_from=resolve_date(effective_from, ctx, what="--effective-from"),
            large_flow_basis="percent" if large_flow_pct else "amount",
            large_flow_value=money_arg(
                large_flow_pct or large_flow_amount, what="--large-flow"
            ),
            materiality_return_bps=(
                money_arg(materiality_bps, what="--materiality-bps")
                if materiality_bps
                else None
            ),
            note=note,
        )

        payload = {
            "effective_from": policy.effective_from.isoformat(),
            "large_flow_basis": policy.large_flow_basis,
            "large_flow_value": policy.large_flow_value,
            "materiality_return_bps": policy.materiality_return_bps,
        }
        if ctx.dry_run:
            return maybe_dry_run(CommandResult(command="policy set", data=payload))

        with db_transaction(repos.con):
            policy_id = repos.policies.add(policy)

        return CommandResult(
            command="policy set",
            data={**payload, "policy_id": policy_id},
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


@policy_app.command(name="show")
def show_policy() -> None:
    """The effective return policy, and its history.

    Refuses with PT-E-GIPS-NO-FLOW-POLICY when none is in force -- a missing
    policy is an error, not a zero (PORT-GIPS-B03).
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        policies = repos.policies.all()
        in_force = repos.policies.in_force(ctx.as_of)

        if in_force is None:
            raise GipsRefusalError(
                f"no return policy in force on {ctx.as_of.isoformat()}",
                requirement="PORT-GIPS-B03",
                code=E_GIPS_NO_FLOW_POLICY,
                remedy=(
                    "Set one with `pt policy set --large-flow-pct 0.10 "
                    "--effective-from YYYY-MM-DD`. GIPS requires the entity to define "
                    "the large-cash-flow threshold and supplies no number; portable "
                    "will not default it to zero."
                ),
                as_of=ctx.as_of.isoformat(),
                known_policies=[p.effective_from.isoformat() for p in policies],
            )

        return CommandResult(
            command="policy show",
            table=Table(
                columns=(
                    Column("effective_from", "Effective From", ColumnKind.DATE),
                    Column("large_flow_basis", "Large Flow Basis"),
                    Column("large_flow_value", "Large Flow", ColumnKind.RATE),
                    Column("significant_flow_value", "Significant Flow", ColumnKind.RATE),
                    Column("materiality_return_bps", "Materiality (bps)", ColumnKind.MONEY),
                    Column("risk_measure_basis", "Risk Basis"),
                ),
                rows=tuple(
                    {
                        "effective_from": p.effective_from.isoformat(),
                        "large_flow_basis": p.large_flow_basis,
                        "large_flow_value": p.large_flow_value,
                        "significant_flow_value": p.significant_flow_value,
                        "materiality_return_bps": p.materiality_return_bps,
                        "risk_measure_basis": p.risk_measure_basis,
                    }
                    for p in policies
                ),
                title="Return policy history",
                footnotes=(
                    "Large and significant cash flows are DIFFERENT thresholds for "
                    "different purposes: large triggers revaluation and a sub-period "
                    "return; significant triggers temporary removal from a composite "
                    "(PORT-GIPS-B03, E09).",
                    "Because portable values daily, the large-flow threshold is "
                    "informational: the sub-period requirement is already satisfied.",
                ),
            ),
            data={
                "in_force_from": in_force.effective_from.isoformat(),
                "large_flow_basis": in_force.large_flow_basis,
                "large_flow_value": in_force.large_flow_value,
                "as_of": ctx.as_of.isoformat(),
            },
            as_of=ctx.as_of,
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)
