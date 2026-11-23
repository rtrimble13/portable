"""`pert periods` -- the time-weighted return over every period, from one walk. ADR 0045.

Thin: it resolves the date, the account and the basis shown, calls
:func:`portable_core.services.periods.period_table`, and lays the rows out.
Every number, every clip to a track record and every refusal is decided in
the service.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

import typer

from portable_core.disclaimer import GIPS_DISCLAIMER
from portable_core.domain.periods import TrackRecord
from portable_core.domain.returns import BASES, Basis
from portable_core.errors import PortableError, UsageError
from portable_core.formatters import Column, ColumnKind, CommandResult, ReturnValue, Table
from portable_core.services.periods import (
    INCEPTION,
    RESTART,
    TERMINATION,
    PeriodRow,
    PeriodTable,
    period_table,
)
from portable_pert import state
from portable_pert.commands.twr import BASES_NOTE, BASIS_LABELS, FLOW_TIMING, disclosure_data

__all__ = ["periods"]

_DAY = timedelta(days=1)

_WHY = {
    INCEPTION: "starts at inception",
    RESTART: "starts where the track record began again",
    TERMINATION: "ends where the track record ended",
}


def periods(
    end: Annotated[
        str | None, typer.Option("--to", help="The day every period ends. Default: --as-of.")
    ] = None,
    account: Annotated[
        str | None,
        typer.Option("--account", "-a", help="One account. Default: the whole portfolio."),
    ] = None,
    basis: Annotated[
        str,
        typer.Option(
            "--basis",
            help=(
                "The basis the table shows: net_of_fees, net_of_external_costs_only, "
                "gross_of_fees or net_of_all_costs. JSON carries every basis."
            ),
        ),
    ] = str(Basis.NET_OF_FEES),
) -> None:
    """Time-weighted return over every period: to date, trailing, since inception, each year.

    Month, quarter and year to date; 1, 3, 5 and 10 years; since inception;
    and every fiscal year since inception, from the fiscal year end the
    portfolio was created with. Each row is what `pert twr` gives over its
    dates. A period under a year is never annualized; one of a year or more
    shows both. Each annual row shows its ending value and the accounts
    holding value then.

    A track record ends where nothing is held, and two are never linked:
    the periods to date and since inception are the current record's, and
    each record's years are its own. A period that cannot be measured is
    shown empty with its reason, never as zero. A 3-year row over a record
    shorter than three years is empty, not a shorter figure.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        through = state.resolve_date(end, ctx, what="--to")
        shown = _basis(basis)
        target = repos.accounts.resolve(account) if account else None
        table = period_table(
            repos,
            through=through,
            account=target,
            staleness_days=int(ctx.config.get("staleness_tolerance_days", 5)),
        )
        return _render(table, shown, portfolio=ctx.portfolio_name())

    state.dispatch(action)


def _basis(text: str) -> Basis:
    try:
        return Basis(text)
    except ValueError:
        raise UsageError(
            f"--basis must be one of {', '.join(str(b) for b in BASES)}, got {text!r}",
            remedy="Net-of-fees is the default. JSON carries every basis whichever is shown.",
            value=text,
        ) from None


# ── JSON ─────────────────────────────────────────────────────────────────────


def _value(row: PeriodRow, basis: Basis, value: Any, *, annualized: bool) -> ReturnValue:
    assert row.result is not None
    return ReturnValue(
        value=value,
        method="twr",
        basis=str(basis),
        # A period runs from the end of the day before its first day, which is
        # what the sub-year guard counts from (PORT-GIPS-B07).
        period_start=row.result.start - _DAY,
        period_end=row.result.end,
        is_annualized=annualized,
        is_supplemental=basis is Basis.NET_OF_ALL_COSTS,
        label=BASIS_LABELS[basis],
    )


def _refusal(error: PortableError | None) -> dict[str, Any] | None:
    return error.to_dict() if error is not None else None


def _row_data(row: PeriodRow) -> dict[str, Any]:
    window, result = row.window, row.result
    returns: list[dict[str, Any]] | None = None
    if result is not None:
        returns = []
        for entry, annual in zip(result.cumulative, row.annualized, strict=True):
            returns.append(
                {
                    "basis": str(entry.basis),
                    "cumulative": _value(row, entry.basis, entry.value, annualized=False),
                    "annualized": (
                        _value(row, entry.basis, annual.value, annualized=True)
                        if annual.value is not None
                        else None
                    ),
                    "not_annualized": _refusal(annual.refusal),
                }
            )
    return {
        "kind": str(window.kind),
        "label": window.label,
        "years": window.years,
        "record": row.record,
        "requested": {"from": window.first.isoformat(), "to": window.through.isoformat()},
        "start": result.start.isoformat() if result else None,
        "end": result.end.isoformat() if result else None,
        "days": result.days if result else None,
        "partial": list(row.partial),
        "returns": returns,
        "ending_value": row.ending_value,
        "accounts_holding_value": row.accounts_holding_value,
        "disclosures": disclosure_data(result.disclosures) if result else None,
        # ADR 0044 §3: each row is estimated, or not, on its own. Nothing is
        # until `--estimate` exists.
        "estimate": None,
        "refusal": _refusal(row.refusal),
    }


# ── the table ────────────────────────────────────────────────────────────────


def _render(table: PeriodTable, shown: Basis, *, portfolio: str | None) -> CommandResult:
    label = BASIS_LABELS[shown]
    notes: list[str] = []
    rows: list[dict[str, Any]] = []

    def mark(text: str) -> str:
        notes.append(text)
        return f" [{len(notes)}]"

    for row in table.summary:
        rows.append(_table_row(row, shown, mark))
    for index, record in enumerate(table.records):
        if index > 0:
            rows.append(_break_row(table.records[index - 1], record, mark))
        for row in table.fiscal_years:
            if row.record == index:
                rows.append(_table_row(row, shown, mark))

    scope = table.account or "portfolio"
    fiscal = ""
    if table.fiscal_year_end.month != 12:
        fiscal = f", fiscal year ending {table.fiscal_year_end}"
    data: dict[str, Any] = {
        "level": str(table.level),
        "account": table.account,
        "method": "twr",
        "flow_timing": FLOW_TIMING,
        "through": table.through.isoformat(),
        "fiscal_year_end": str(table.fiscal_year_end),
        "track_records": [
            {
                "inception": record.inception.isoformat(),
                "termination": record.termination.isoformat() if record.termination else None,
            }
            for record in table.records
        ],
        "summary": [_row_data(row) for row in table.summary],
        "fiscal_years": [_row_data(row) for row in table.fiscal_years],
    }
    return CommandResult(
        command="periods",
        table=Table(
            columns=(
                Column("period", "Period", ColumnKind.TEXT),
                Column("from", "From", ColumnKind.DATE),
                Column("to", "To", ColumnKind.DATE),
                Column("cumulative", f"{label}, cumulative", ColumnKind.RETURN),
                Column("annualized", f"{label}, annualized", ColumnKind.RETURN),
                Column("ending_value", "Ending value", ColumnKind.MONEY),
                Column("accounts", "Accounts", ColumnKind.INTEGER),
            ),
            rows=tuple(rows),
            title=(
                f"Time-weighted returns, {scope}, through {table.through.isoformat()}{fiscal}"
            ),
            footnotes=(*_general_notes(table), *(f"[{n}] {t}" for n, t in enumerate(notes, 1))),
        ),
        data=data,
        warnings=tuple(
            f"{row.window.label}: {row.refusal.code}: {row.refusal.message}"
            for row in table.rows
            if row.refusal is not None
        ),
        as_of=table.through,
        portfolio=portfolio,
        disclaimer=GIPS_DISCLAIMER,
        schema_ref="periods-1.0.json",
        # Each row's full disclosures are in JSON. The table's footnotes carry
        # what a reader of it needs.
        human_data=False,
    )


def _table_row(row: PeriodRow, shown: Basis, mark: Any) -> dict[str, Any]:
    window, result = row.window, row.result
    if result is None:
        assert row.refusal is not None
        return {
            "period": window.label + mark(f"{window.label}: {row.refusal.message}."),
            "from": window.first,
            "to": window.through,
            "cumulative": None,
            "annualized": None,
            "ending_value": None,
            "accounts": None,
        }
    period = window.label
    if row.partial:
        why = " and ".join(_WHY[reason] for reason in row.partial)
        period += mark(
            f"{window.label} is partial: it {why}, {result.start.isoformat()} to "
            f"{result.end.isoformat()}, of {window.first.isoformat()} to "
            f"{window.through.isoformat()} (PORT-GIPS-H02)."
        )
    annual = next(entry for entry in row.annualized if entry.basis is shown)
    if annual.refusal is not None and annual.refusal.code != "PT-E-GIPS-ANNUALIZE-SUB-YEAR":
        period += mark(f"{window.label}, not annualized: {annual.refusal.message}.")
    return {
        "period": period,
        "from": result.start,
        "to": result.end,
        "cumulative": _value(row, shown, result.return_for(shown), annualized=False),
        "annualized": (
            _value(row, shown, annual.value, annualized=True)
            if annual.value is not None
            else None
        ),
        "ending_value": row.ending_value,
        "accounts": row.accounts_holding_value,
    }


def _break_row(ended: TrackRecord, began: TrackRecord, mark: Any) -> dict[str, Any]:
    assert ended.termination is not None
    first, last = ended.termination + _DAY, began.inception - _DAY
    return {
        "period": "Nothing held"
        + mark(
            f"Nothing was held from {first.isoformat()} to {last.isoformat()}. The track "
            f"record ended on {ended.termination.isoformat()} and began again on "
            f"{began.inception.isoformat()}, and the two are never linked "
            "(PORT-GIPS-H06). The periods to date and since inception are the current "
            "record's."
        ),
        "from": first,
        "to": last,
        "cumulative": None,
        "annualized": None,
        "ending_value": None,
        "accounts": None,
    }


def _general_notes(table: PeriodTable) -> list[str]:
    """What every reader of the table needs, generated from its measured rows."""
    measured = [row.result for row in table.rows if row.result is not None]
    notes = [
        "Time-weighted, from daily sub-periods linked geometrically, with Modified "
        "Dietz across unpriced days within a month. Inflows count from the start of "
        "their day and outflows from its end.",
        "A period under a year is cumulative and never annualized (PORT-GIPS-B07). "
        "A year is counted by anniversary.",
        BASES_NOTE,
    ]
    unpriced = sorted({h for result in measured for h in result.disclosures.unpriced_holdings})
    if unpriced:
        notes.append(
            "Valued at zero for want of a market price (level 5): " + ", ".join(unpriced) + "."
        )
    if any(result.disclosures.margin_interest for result in measured):
        notes.append("Margin interest is a financing cost, deducted at every basis.")
    estimated = {day for result in measured for day in result.disclosures.estimated_days}
    if estimated:
        notes.append(f"{len(estimated)} valuation day(s) used an estimated price.")
    if any(result.disclosures.reclaimable_withholding for result in measured):
        notes.append("Reclaimable withholding tax is not accrued.")
    notes.append(
        "Each row's disclosures -- large flows, estimated and stale prices, level-5 "
        "value -- are in --format json."
    )
    return notes
