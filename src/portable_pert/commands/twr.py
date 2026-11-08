"""`pert twr` -- the time-weighted return. ADR 0040 §14.

Thin: it resolves the dates and the account, calls
:func:`portable_core.services.returns.time_weighted_return`, and lays the
result out. Every number is decided in the service.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

import typer

from portable_core.disclaimer import GIPS_DISCLAIMER
from portable_core.domain.returns import BASES, Basis, ReturnMethod, TimeWeightedReturn
from portable_core.formatters import Column, ColumnKind, CommandResult, ReturnValue, Table
from portable_core.services.returns import time_weighted_return
from portable_pert import state

__all__ = ["twr"]

#: ADR 0040 §3, answer 1. Recorded on every result as the method.
FLOW_TIMING = "inflows at the start of the day, outflows at the end"

_LABELS = {
    Basis.NET_OF_FEES: "Net-of-fees",
    Basis.NET_OF_EXTERNAL_COSTS_ONLY: "Net-of-external-costs-only",
    Basis.GROSS_OF_FEES: "Gross-of-fees",
    Basis.NET_OF_ALL_COSTS: "Net of all costs (supplemental)",
}


def twr(
    start: Annotated[
        str | None, typer.Option("--from", help="First day. Default: inception.")
    ] = None,
    end: Annotated[str | None, typer.Option("--to", help="Last day. Default: --as-of.")] = None,
    account: Annotated[
        str | None,
        typer.Option("--account", "-a", help="One account. Default: the whole portfolio."),
    ] = None,
    daily: Annotated[bool, typer.Option("--daily", help="Also emit every sub-period.")] = False,
) -> None:
    """Time-weighted return, cumulative, at every return basis.

    Chain-linked from the stored valuation snapshots (`pt value`): daily where
    a day can be valued, Modified Dietz across a gap of unpriced days within a
    month. Net-of-fees leads. Nothing is annualized here. A missing or
    incomplete day, a large flow inside a gap, or a day with no return policy
    is refused, never interpolated.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        last = state.resolve_date(end, ctx, what="--to")
        first = state.resolve_date(start, ctx, what="--from") if start else None
        target = repos.accounts.resolve(account) if account else None
        result = time_weighted_return(
            repos,
            start=first,
            end=last,
            account=target,
            staleness_days=int(ctx.config.get("staleness_tolerance_days", 5)),
        )
        return _render(result, daily=daily, portfolio=ctx.portfolio_name())

    state.dispatch(action)


def _value(result: TimeWeightedReturn, basis: Basis) -> ReturnValue:
    return ReturnValue(
        value=result.return_for(basis),
        method="twr",
        basis=str(basis),
        # A period runs from the end of the day before its first day, which is
        # what the sub-year guard counts from (PORT-GIPS-B07).
        period_start=result.start - timedelta(days=1),
        period_end=result.end,
        is_supplemental=basis is Basis.NET_OF_ALL_COSTS,
        label=_LABELS[basis],
    )


def _render(result: TimeWeightedReturn, *, daily: bool, portfolio: str | None) -> CommandResult:
    disclosures = result.disclosures
    combined = not disclosures.external_fees
    shown = [
        basis for basis in BASES if not (combined and basis is Basis.NET_OF_EXTERNAL_COSTS_ONLY)
    ]
    rows = [
        {
            "basis": (
                "Gross-of-fees = net-of-external-costs-only"
                if combined and basis is Basis.GROSS_OF_FEES
                else _LABELS[basis]
            ),
            "return": _value(result, basis),
        }
        for basis in shown
    ]

    notes = _notes(result)
    data: dict[str, Any] = {
        "level": str(result.level),
        "account": result.account,
        "method": "twr",
        "flow_timing": FLOW_TIMING,
        "period": {
            "requested_start": result.requested_start.isoformat(),
            "start": result.start.isoformat(),
            "end": result.end.isoformat(),
            "inception": result.inception.isoformat(),
            "days": result.days,
            "is_annualized": False,
        },
        "returns": [_value(result, basis) for basis in BASES],
        "gross_equals_net_of_external_costs_only": combined,
        "subperiods": {
            "daily": result.count(ReturnMethod.DAILY),
            "modified_dietz": result.count(ReturnMethod.MODIFIED_DIETZ),
            "idle_days": [day.isoformat() for day in result.idle_days],
        },
        "disclosures": {
            "large_flow_days": [d.isoformat() for d in disclosures.large_flow_days],
            "estimated_days": [d.isoformat() for d in disclosures.estimated_days],
            "stale_price_days": [d.isoformat() for d in disclosures.stale_price_days],
            "level5_value": disclosures.level5_value,
            "level5_share": disclosures.level5_share,
            "unpriced_holdings": list(disclosures.unpriced_holdings),
            "margin_interest_deducted_at_every_basis": disclosures.margin_interest,
            "reclaimable_withholding_not_accrued": disclosures.reclaimable_withholding,
            "notes": list(notes),
        },
    }
    if daily:
        data["series"] = [
            {
                "after": sub.after.isoformat(),
                "through": sub.through.isoformat(),
                "method": str(sub.method),
                "beginning_value": sub.beginning_value,
                "ending_value": sub.ending_value,
                "flows": [
                    {"date": f.on.isoformat(), "amount": f.amount, "is_large": f.is_large}
                    for f in sub.flows
                ],
                "returns": {str(entry.basis): entry.value for entry in sub.returns},
            }
            for sub in result.subperiods
        ]

    scope = result.account or "portfolio"
    return CommandResult(
        command="twr",
        table=Table(
            columns=(
                Column("basis", "Basis", ColumnKind.TEXT),
                Column("return", "Return", ColumnKind.RETURN),
            ),
            rows=tuple(rows),
            title=(
                f"Time-weighted return, {scope}, {result.start.isoformat()} to "
                f"{result.end.isoformat()} ({result.days} days, cumulative)"
            ),
            footnotes=notes,
        ),
        data=data,
        as_of=result.end,
        portfolio=portfolio,
        disclaimer=GIPS_DISCLAIMER,
        schema_ref="twr-1.0.json",
    )


def _notes(result: TimeWeightedReturn) -> tuple[str, ...]:
    """What a reader needs beside the number, generated from the result."""
    disclosures = result.disclosures
    notes = [
        "Time-weighted, from daily sub-periods linked geometrically. Inflows count "
        "from the start of their day and outflows from its end. Not annualized.",
    ]
    md = result.count(ReturnMethod.MODIFIED_DIETZ)
    if md:
        notes.append(
            f"{md} sub-period(s) bridge unpriced days by Modified Dietz in calendar "
            "days, within a month and with no large flow inside."
        )
    if result.start != result.requested_start:
        notes.append(
            f"Starts at inception, {result.inception.isoformat()}, after the requested "
            f"{result.requested_start.isoformat()}."
        )
    # PORT-GIPS-B03: say whether the large-flow threshold bound, because a
    # reader cannot otherwise tell.
    large = len(disclosures.large_flow_days)
    if md:
        notes.append(
            "Every large cash flow fell on a valuation date: a gap with one inside is "
            f"refused ({large} day(s) with a large flow)."
        )
    else:
        notes.append(
            "Valued daily, so every cash flow fell on a valuation date and the "
            f"large-flow threshold did not bind ({large} day(s) with a large flow)."
        )
    notes.append(
        "Transaction costs and fund expenses inside NAV are deducted at every basis; "
        "net-of-fees deducts custody and other internal management costs; "
        "gross-of-fees adds back external management fees too. Other administrative "
        "costs reduce only the supplemental net-of-all-costs figure."
    )
    if disclosures.margin_interest:
        notes.append("Margin interest is a financing cost, deducted at every basis.")
    if disclosures.unpriced_holdings:
        notes.append(
            "Valued at zero for want of a market price (level 5): "
            + ", ".join(disclosures.unpriced_holdings)
            + "."
        )
    if disclosures.estimated_days:
        notes.append(
            f"{len(disclosures.estimated_days)} valuation day(s) used an estimated price."
        )
    if disclosures.reclaimable_withholding:
        notes.append("Reclaimable withholding tax is not accrued.")
    return tuple(notes)
