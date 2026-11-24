"""`pert twr` -- the time-weighted return. ADR 0040 §14.

Thin: it resolves the dates and the account, calls
:func:`portable_core.services.returns.time_weighted_return`, and lays the
result out. Every number is decided in the service.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

import typer

from portable_core.disclaimer import GIPS_DISCLAIMER
from portable_core.domain.returns import (
    BASES,
    Basis,
    Disclosures,
    Estimate,
    ReturnMethod,
    TimeWeightedReturn,
)
from portable_core.errors import UsageError
from portable_core.formatters import (
    Column,
    ColumnKind,
    CommandResult,
    ReturnValue,
    Table,
    rate,
)
from portable_core.services.returns import time_weighted_return
from portable_pert import state

__all__ = [
    "BASES_NOTE",
    "BASIS_LABELS",
    "ESTIMATE_HELP",
    "FLOW_TIMING",
    "disclosure_data",
    "estimate_data",
    "estimate_limit",
    "estimate_note",
    "twr",
]

#: ADR 0040 §3, answer 1. Recorded on every result as the method.
FLOW_TIMING = "inflows at the start of the day, outflows at the end"

#: How each basis is named in a table, beside its value (PORT-GIPS-H04).
BASIS_LABELS = {
    Basis.NET_OF_FEES: "Net-of-fees",
    Basis.NET_OF_EXTERNAL_COSTS_ONLY: "Net-of-external-costs-only",
    Basis.GROSS_OF_FEES: "Gross-of-fees",
    Basis.NET_OF_ALL_COSTS: "Net of all costs (supplemental)",
}


#: What each basis deducts (PORT-GIPS-D01, ADR 0040 §8), said beside every return.
BASES_NOTE = (
    "Transaction costs and fund expenses inside NAV are deducted at every basis; "
    "net-of-fees deducts custody and other internal management costs; "
    "gross-of-fees adds back external management fees too. Other administrative "
    "costs reduce only the supplemental net-of-all-costs figure."
)


#: ADR 0044 §4: the limit is required with the flag, and has no default.
ESTIMATE_HELP = (
    "Value a day that cannot be priced at the last price known for each holding it "
    "lacks, where the measured return is refused for it, carrying at most MAX_SHARE "
    "of the day's value (0.05 is 5%). The result is marked ESTIMATED and is not part "
    "of the track record. Without it, nothing is estimated."
)


def estimate_limit(text: str | None) -> Decimal | None:
    """`--estimate MAX_SHARE`: a fraction above zero and at most one. ADR 0044 §4."""
    if text is None:
        return None
    try:
        limit = Decimal(text)
    except InvalidOperation:
        limit = None
    if limit is None or not limit.is_finite() or not Decimal(0) < limit <= Decimal(1):
        raise UsageError(
            f"--estimate must be a share of a day's value above 0 and at most 1, got {text!r}",
            remedy="Give it as a fraction: --estimate 0.05 carries at most 5% of a day.",
            value=text,
        )
    return limit


def estimate_note(estimate: Estimate) -> str:
    """The sentence an estimated result carries, in its footnotes and on stderr.

    ADR 0044 §2. The age is in it, not only in JSON: the limit bounds how much
    was carried, and only the age shows how long it had to drift.
    """
    peak = estimate.peak
    return (
        f"ESTIMATED, not measured, and not part of the track record: "
        f"{len(estimate.days)} day(s) valued with {estimate.holdings} carried "
        f"holding(s), at most {rate(peak.share)} of a day's value, on "
        f"{peak.on.isoformat()}, against a limit of {rate(estimate.limit)}. The oldest "
        f"price carried was {estimate.oldest_age_days} days old (ADR 0044)."
    )


def estimate_data(estimate: Estimate | None) -> dict[str, Any] | None:
    """What was estimated, for JSON: every day and every carried holding. ADR 0044 §2."""
    if estimate is None:
        return None
    peak = estimate.peak
    return {
        "limit": estimate.limit,
        "peak": {"date": peak.on.isoformat(), "share": peak.share},
        "holdings": estimate.holdings,
        "oldest_age_days": estimate.oldest_age_days,
        "days": [
            {
                "date": day.on.isoformat(),
                "value": day.value,
                "share": day.share,
                "holdings": [
                    {
                        "account": holding.account,
                        "symbol": holding.symbol,
                        "quantity": holding.quantity,
                        "price": holding.price,
                        "last_known_price": holding.last_known,
                        "price_date": holding.price_date.isoformat(),
                        "age_days": holding.age_days,
                        "source": str(holding.source),
                        "source_id": holding.source_id,
                        "lowered_by": holding.lowered_by,
                        "value": holding.value,
                        "share": holding.share,
                    }
                    for holding in day.holdings
                ],
            }
            for day in estimate.days
        ],
    }


def disclosure_data(disclosures: Disclosures) -> dict[str, Any]:
    """What a return's report must say beside it (ADR 0040 §10), for JSON."""
    return {
        "large_flow_days": [d.isoformat() for d in disclosures.large_flow_days],
        "estimated_days": [d.isoformat() for d in disclosures.estimated_days],
        "stale_price_days": [d.isoformat() for d in disclosures.stale_price_days],
        "level5_value": disclosures.level5_value,
        "level5_share": disclosures.level5_share,
        "unpriced_holdings": list(disclosures.unpriced_holdings),
        "margin_interest_deducted_at_every_basis": disclosures.margin_interest,
        "reclaimable_withholding_not_accrued": disclosures.reclaimable_withholding,
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
    estimate: Annotated[
        str | None, typer.Option("--estimate", metavar="MAX_SHARE", help=ESTIMATE_HELP)
    ] = None,
) -> None:
    """Time-weighted return, cumulative, at every return basis.

    Chain-linked from the stored valuation snapshots (`pt value`): daily where
    a day can be valued, Modified Dietz across a gap of unpriced days within a
    month. Net-of-fees leads. Nothing is annualized here. A missing or
    incomplete day, a large flow inside a gap, or a day with no return policy
    is refused, never interpolated.

    A track record ends where nothing is held, and two are never linked. A
    period across the gap is refused, as is one with nothing held at all. A
    period that starts or ends in it is moved to the record, and says so.

    A day is measured only with prices at most five days old. With
    --estimate, a day refused for a missing or older price is valued at the
    last price known instead, if the period needs it, and the result is
    marked ESTIMATED.
    """

    def action() -> CommandResult:
        limit = estimate_limit(estimate)
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
            estimate=limit,
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
        is_estimated=result.is_estimated,
        label=BASIS_LABELS[basis],
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
                else BASIS_LABELS[basis]
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
            "requested_end": result.requested_end.isoformat(),
            "inception": result.inception.isoformat(),
            "days": result.days,
            "is_annualized": False,
            "nothing_held_before": _span(result.nothing_held_before),
            "nothing_held_after": _span(result.nothing_held_after),
        },
        "returns": [_value(result, basis) for basis in BASES],
        "gross_equals_net_of_external_costs_only": combined,
        "subperiods": {
            "daily": result.count(ReturnMethod.DAILY),
            "modified_dietz": result.count(ReturnMethod.MODIFIED_DIETZ),
        },
        # ADR 0044 §2: null unless a carried price was needed under --estimate.
        "estimate": estimate_data(result.estimate),
        "disclosures": {**disclosure_data(disclosures), "notes": list(notes)},
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
                "is_estimated": sub.is_estimated,
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
                (
                    "ESTIMATED time-weighted return"
                    if result.is_estimated
                    else "Time-weighted return"
                )
                + f", {scope}, {result.start.isoformat()} to "
                f"{result.end.isoformat()} ({result.days} days, cumulative)"
            ),
            footnotes=notes,
        ),
        data=data,
        warnings=(estimate_note(result.estimate),) if result.estimate is not None else (),
        as_of=result.requested_end,
        portfolio=portfolio,
        disclaimer=GIPS_DISCLAIMER,
        schema_ref="twr-2.0.json",
    )


def _span(days: tuple[date, date] | None) -> dict[str, str] | None:
    if days is None:
        return None
    first, last = days
    return {"from": first.isoformat(), "to": last.isoformat()}


def _notes(result: TimeWeightedReturn) -> tuple[str, ...]:
    """What a reader needs beside the number, generated from the result."""
    disclosures = result.disclosures
    notes = [
        "Time-weighted, from daily sub-periods linked geometrically. Inflows count "
        "from the start of their day and outflows from its end. Not annualized.",
    ]
    if result.estimate is not None:
        notes.insert(0, estimate_note(result.estimate))
    md = result.count(ReturnMethod.MODIFIED_DIETZ)
    if md:
        notes.append(
            f"{md} sub-period(s) bridge unpriced days by Modified Dietz in calendar "
            "days, within a month and with no large flow inside."
        )
    # ADR 0045 §4: a track record ends where nothing is held, and the period
    # is moved to the record rather than linked across the gap (PORT-GIPS-H06).
    if result.nothing_held_before is not None:
        first, last = result.nothing_held_before
        notes.append(
            f"Starts on {result.start.isoformat()}, where the track record begins again: "
            f"nothing was held from {first.isoformat()} to {last.isoformat()}. The "
            f"requested start was {result.requested_start.isoformat()}."
        )
    elif result.start != result.requested_start:
        notes.append(
            f"Starts at inception, {result.inception.isoformat()}, after the requested "
            f"{result.requested_start.isoformat()}."
        )
    if result.nothing_held_after is not None:
        first, last = result.nothing_held_after
        notes.append(
            f"Ends on {result.end.isoformat()}, where the track record ended: nothing was "
            f"held from {first.isoformat()} to {last.isoformat()}, the requested end."
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
    notes.append(BASES_NOTE)
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
