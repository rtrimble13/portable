"""`pert mwr` -- the money-weighted return, beside the time-weighted return. ADR 0046.

Thin: it resolves the dates and the account, calls
:func:`portable_core.services.money_weighted.money_weighted_return`, and lays
the two rates out side by side. The MWR is never shown without the TWR
(PORT-GIPS-C01), and the service refuses one the TWR refuses.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Any

import typer

from portable_core.disclaimer import GIPS_DISCLAIMER
from portable_core.domain.returns import BASES, Basis, MoneyWeightedReturn, ReturnMethod
from portable_core.errors import PortableError
from portable_core.formatters import Column, ColumnKind, CommandResult, ReturnValue, Table
from portable_core.services.money_weighted import YEAR_COUNT, money_weighted_return
from portable_core.services.periods import annualize
from portable_pert import state
from portable_pert.commands.twr import (
    BASES_NOTE,
    BASIS_LABELS,
    ESTIMATE_HELP,
    FLOW_TIMING,
    disclosure_data,
    estimate_data,
    estimate_limit,
    estimate_note,
)

__all__ = ["mwr"]

_DAY = timedelta(days=1)

#: What the two rates measure, said beside every pair (PORT-GIPS-C01, I04).
WHAT_EACH_MEASURES = (
    "Money-weighted: the one rate at which the money paid in, compounded to the end, "
    "comes to what was taken out and what is left. It counts when the deposits and "
    "withdrawals were made, so it is the owner's own result. The time-weighted return "
    "removes that timing and measures the investments. Neither replaces the other, and "
    "the money-weighted return is never shown alone (PORT-GIPS-C01)."
)


def mwr(
    start: Annotated[
        str | None,
        typer.Option("--from", help="First day. Default: where the track record begins."),
    ] = None,
    end: Annotated[str | None, typer.Option("--to", help="Last day. Default: --as-of.")] = None,
    account: Annotated[
        str | None,
        typer.Option("--account", "-a", help="One account. Default: the whole portfolio."),
    ] = None,
    estimate: Annotated[
        str | None, typer.Option("--estimate", metavar="MAX_SHARE", help=ESTIMATE_HELP)
    ] = None,
) -> None:
    """Money-weighted return, beside the time-weighted return over the same days.

    The money-weighted return is the one rate at which the deposits and
    withdrawals, with what is left, balance: the owner's own result. It is
    solved once from the flows, for the portfolio from every account's
    together, and is shown only beside the time-weighted return. A period
    the time-weighted return refuses is refused.

    From a year, both are annualized. Under a year, neither is. A rate that
    cannot be shown to be the only one is refused, naming the withdrawal
    responsible.
    """

    def action() -> CommandResult:
        limit = estimate_limit(estimate)
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        last = state.resolve_date(end, ctx, what="--to")
        first = state.resolve_date(start, ctx, what="--from") if start else None
        target = repos.accounts.resolve(account) if account else None
        result = money_weighted_return(
            repos,
            start=first,
            end=last,
            account=target,
            staleness_days=int(ctx.config.get("staleness_tolerance_days", 5)),
            estimate=limit,
        )
        return _render(result, portfolio=ctx.portfolio_name())

    state.dispatch(action)


def _mwr_value(result: MoneyWeightedReturn, basis: Basis) -> ReturnValue:
    entry = result.for_basis(basis)
    return ReturnValue(
        value=entry.annual if result.is_annualized else entry.cumulative,
        method="mwr",
        basis=str(basis),
        # From the end of the day before the first day, as every period here.
        period_start=result.start - _DAY,
        period_end=result.end,
        is_annualized=result.is_annualized,
        is_supplemental=basis is Basis.NET_OF_ALL_COSTS,
        is_estimated=result.is_estimated,
        label=BASIS_LABELS[basis],
    )


def _twr_value(
    result: MoneyWeightedReturn, basis: Basis, value: Decimal, *, annual: bool
) -> ReturnValue:
    return ReturnValue(
        value=value,
        method="twr",
        basis=str(basis),
        period_start=result.start - _DAY,
        period_end=result.end,
        is_annualized=annual,
        is_supplemental=basis is Basis.NET_OF_ALL_COSTS,
        is_estimated=result.twr.is_estimated,
        label=BASIS_LABELS[basis],
    )


def _twr_annualized(
    result: MoneyWeightedReturn, basis: Basis
) -> tuple[Decimal | None, str | None]:
    """The TWR annualised over the same days, or why there is none (ADR 0045 §5)."""
    if not result.is_annualized:
        return None, None
    try:
        return (
            annualize(
                result.twr.return_for(basis), after=result.start - _DAY, through=result.end
            ),
            None,
        )
    except PortableError as refusal:
        return None, refusal.message


def _render(result: MoneyWeightedReturn, *, portfolio: str | None) -> CommandResult:
    twr = result.twr
    combined = not twr.disclosures.external_fees
    shown = [b for b in BASES if not (combined and b is Basis.NET_OF_EXTERNAL_COSTS_ONLY)]
    annual = {basis: _twr_annualized(result, basis) for basis in BASES}

    rows: list[dict[str, Any]] = []
    for basis in shown:
        row: dict[str, Any] = {
            "basis": (
                "Gross-of-fees = net-of-external-costs-only"
                if combined and basis is Basis.GROSS_OF_FEES
                else BASIS_LABELS[basis]
            ),
            "mwr": _mwr_value(result, basis),
            "twr": _twr_value(result, basis, twr.return_for(basis), annual=False),
        }
        if result.is_annualized:
            value = annual[basis][0]
            row["twr_annualized"] = (
                _twr_value(result, basis, value, annual=True) if value is not None else None
            )
        rows.append(row)

    notes = _notes(result, annual)
    data: dict[str, Any] = {
        "level": str(twr.level),
        "account": twr.account,
        "method": "mwr",
        "flow_timing": FLOW_TIMING,
        "year_count": YEAR_COUNT,
        "period": {
            "requested_start": twr.requested_start.isoformat(),
            "start": result.start.isoformat(),
            "end": result.end.isoformat(),
            "requested_end": twr.requested_end.isoformat(),
            "days": twr.days,
            "years": {
                "whole": result.whole_years,
                "days": result.part_days,
                "of": result.part_of,
            },
            "is_since_inception": result.since_inception,
            "is_annualized": result.is_annualized,
        },
        "beginning_value": result.beginning_value,
        "ending_value": result.ending_value,
        "flows": {"count": result.flow_count, "net_paid_in": result.flow_total},
        "is_estimated": result.is_estimated,
        "returns": [
            {
                "basis": str(entry.basis),
                "mwr": _mwr_value(result, entry.basis),
                "solve": {
                    "variable": "ln(1 + R)",
                    "newton_steps": entry.solve.newton_steps,
                    "bisection_steps": entry.solve.bisection_steps,
                    "bracket": [entry.solve.lower, entry.solve.upper],
                    "residual": entry.solve.residual,
                },
                "only_rate": {
                    "test": "balance_at_rate",
                    "lowest_balance": entry.lowest_balance,
                    "lowest_balance_date": entry.lowest_balance_on.isoformat(),
                },
            }
            for entry in result.bases
        ],
        "twr": {
            "returns": [
                _twr_value(result, basis, twr.return_for(basis), annual=False)
                for basis in BASES
            ],
            "annualized": [
                _twr_value(result, basis, value, annual=True) if value is not None else None
                for basis, (value, _) in annual.items()
            ],
            "subperiods": {
                "daily": twr.count(ReturnMethod.DAILY),
                "modified_dietz": twr.count(ReturnMethod.MODIFIED_DIETZ),
            },
            "estimate": estimate_data(twr.estimate),
        },
        "gross_equals_net_of_external_costs_only": combined,
        "disclosures": {**disclosure_data(twr.disclosures), "notes": list(notes)},
    }

    columns = [
        Column("basis", "Basis", ColumnKind.TEXT),
        Column("mwr", "Money-weighted", ColumnKind.RETURN),
        Column("twr", "Time-weighted, cumulative", ColumnKind.RETURN),
    ]
    if result.is_annualized:
        columns.append(Column("twr_annualized", "Time-weighted, annualized", ColumnKind.RETURN))
    scope = twr.account or "portfolio"
    estimated = result.is_estimated or twr.is_estimated
    since = ", since inception" if result.since_inception else ""
    return CommandResult(
        command="mwr",
        table=Table(
            columns=tuple(columns),
            rows=tuple(rows),
            title=(
                ("ESTIMATED " if estimated else "")
                + f"Money-weighted and time-weighted returns, {scope}, "
                f"{result.start.isoformat()} to {result.end.isoformat()} "
                f"({twr.days} days{since})"
            ),
            footnotes=notes,
        ),
        data=data,
        warnings=_warnings(result),
        as_of=twr.requested_end,
        portfolio=portfolio,
        disclaimer=GIPS_DISCLAIMER,
        schema_ref="mwr-1.0.json",
        human_data=False,
    )


def _warnings(result: MoneyWeightedReturn) -> tuple[str, ...]:
    out: list[str] = []
    if result.is_estimated:
        out.append(MWR_ESTIMATED)
    if result.twr.estimate is not None:
        out.append(f"Time-weighted: {estimate_note(result.twr.estimate)}")
    return tuple(out)


#: ADR 0044 §3: the MWR reads only its two ends, so only they can make it an estimate.
MWR_ESTIMATED = (
    "The money-weighted return is ESTIMATED: its beginning or ending value was valued "
    "at carried prices, and it is not part of the track record (ADR 0044 §3)."
)


def _notes(
    result: MoneyWeightedReturn, annual: dict[Basis, tuple[Decimal | None, str | None]]
) -> tuple[str, ...]:
    """What a reader needs beside the two rates, generated from the result."""
    twr = result.twr
    notes = [WHAT_EACH_MEASURES]
    if result.is_estimated:
        notes.insert(0, MWR_ESTIMATED)
    if twr.estimate is not None:
        notes.insert(0, f"Time-weighted: {estimate_note(twr.estimate)}")
    notes.append(
        "Inflows count from the start of their day and outflows from its end, for both. "
        "A year is counted by anniversary."
    )
    if result.since_inception:
        notes.append(f"Since inception: the track record begins on {result.start.isoformat()}.")
    else:
        notes.append(
            f"A period money-weighted return, from {result.start.isoformat()}: not the "
            "since-inception figure (PORT-GIPS-C02)."
        )
    if result.is_annualized:
        notes.append(
            f"Annualized over {result.whole_years} year(s) and {result.part_days} day(s)."
        )
        refusals = {why for _, why in annual.values() if why is not None}
        for why in sorted(refusals):
            notes.append(f"Time-weighted, not annualized: {why}.")
    else:
        notes.append("Under a year, so neither is annualized (PORT-GIPS-B07, C03).")
    net = result.for_basis(Basis.NET_OF_FEES)
    notes.append(
        f"{result.flow_count} external flow(s). The rate is the only one: compounded at "
        "it, the money paid in never fell below what was taken out before the end "
        f"(ADR 0046 §3). Lowest, net-of-fees, at the end of "
        f"{net.lowest_balance_on.isoformat()}."
    )
    md = twr.count(ReturnMethod.MODIFIED_DIETZ)
    if md:
        notes.append(
            f"The time-weighted return bridges unpriced days in {md} sub-period(s) by "
            "Modified Dietz, within a month and with no large flow inside."
        )
    notes.append(BASES_NOTE)
    if twr.disclosures.margin_interest:
        notes.append("Margin interest is a financing cost, deducted at every basis.")
    if twr.disclosures.unpriced_holdings:
        notes.append(
            "Valued at zero for want of a market price (level 5): "
            + ", ".join(twr.disclosures.unpriced_holdings)
            + "."
        )
    return tuple(notes)
