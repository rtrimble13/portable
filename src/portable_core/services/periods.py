"""Multi-period returns: the rows of a period table, from one walk. ADR 0045.

`pert periods` is a thin layer over :func:`period_table`. Every row is a
window of one walk over the level's history (§1), so each is, value for value
and refusal for refusal, what `pert twr` gives over the same dates. Two
departures, both in §2:

* **A row is clipped to its track record** (§4, ``PORT-GIPS-H06``). A
  period-to-date row or a fiscal year that begins before the record does
  starts with it, and one that runs past its end stops there, labelled
  partial (``PORT-GIPS-H02``). Clipped like this, a row never spans a break.
* **A fixed-length row is never shortened.** A "3-year" figure over two
  years is a different number under the same label, so a trailing row the
  record does not cover is null, with its reason
  (`PT-E-PERIOD-OUTSIDE-RECORD`).

A row that cannot be measured is null with its refusal, never zero, and the
rest of the table stands (§3).
"""

from __future__ import annotations

import decimal
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from portable_core.domain.enums import FlowLevel
from portable_core.domain.models import Account
from portable_core.domain.periods import (
    FiscalYearEnd,
    PeriodKind,
    TrackRecord,
    Window,
    at_least_a_year,
    last_month_end_before,
    shift_years,
    years,
)
from portable_core.domain.returns import Basis, TimeWeightedReturn
from portable_core.errors import GipsRefusalError, PortableError, ValidationError
from portable_core.errors.kinds import (
    E_FISCAL_YEAR_END,
    E_GIPS_ANNUALIZE_SUB_YEAR,
    E_PERIOD_OUTSIDE_RECORD,
    E_RETURN_TOTAL_LOSS,
)
from portable_core.persistence.repositories import Repositories
from portable_core.services import returns
from portable_core.services.returns import RETURN_CONTEXT, Walk, track_records

__all__ = [
    "TRAILING_YEARS",
    "Annualized",
    "PeriodRow",
    "PeriodTable",
    "annualize",
    "fiscal_year_end",
    "period_table",
]

#: The trailing periods a table shows, in years (issue 5).
TRAILING_YEARS: Final = (1, 3, 5, 10)

_ONE: Final = Decimal(1)
_DAY: Final = timedelta(days=1)

#: Why a measured period is shorter than its window (PORT-GIPS-H02).
INCEPTION: Final = "inception"
RESTART: Final = "restart"
TERMINATION: Final = "termination"


@dataclass(frozen=True, slots=True)
class Annualized:
    """One basis's annualised rate, or why it has none."""

    basis: Basis
    value: Decimal | None
    refusal: PortableError | None


@dataclass(frozen=True, slots=True)
class PeriodRow:
    """One row: the window asked for, and what was measured over it, or why nothing was."""

    window: Window
    #: The track record the row measures, by index; None when it measures none.
    record: int | None
    result: TimeWeightedReturn | None
    refusal: PortableError | None
    #: Why the measured period is shorter than the window, in order: its start
    #: moved to `inception` or a `restart`, its end to a `termination`.
    partial: tuple[str, ...]
    annualized: tuple[Annualized, ...]
    #: At the measured period's last day (PORT-GIPS-H03).
    ending_value: Decimal | None
    accounts_holding_value: int | None


@dataclass(frozen=True, slots=True)
class PeriodTable:
    """Every row of `pert periods`, and the track records they are clipped to."""

    level: FlowLevel
    account: str | None
    through: date
    fiscal_year_end: FiscalYearEnd
    records: tuple[TrackRecord, ...]
    #: Month, quarter and year to date; the trailing years; since inception.
    summary: tuple[PeriodRow, ...]
    #: Every fiscal year of every record, oldest first (PORT-GIPS-H01).
    fiscal_years: tuple[PeriodRow, ...]

    @property
    def rows(self) -> tuple[PeriodRow, ...]:
        return self.summary + self.fiscal_years


# ── the table ────────────────────────────────────────────────────────────────


def period_table(
    repos: Repositories,
    *,
    through: date,
    account: Account | None = None,
    staleness_days: int,
) -> PeriodTable:
    """Every period of *account*, or of the portfolio, ending on *through*. ADR 0045.

    Raises before any row for a fiscal year end that is not a month end, and
    for no ledger row on or before *through*. When no row can be measured,
    raises the first row's refusal, as `pert twr` over it would (§3).
    """
    fiscal = fiscal_year_end(repos)
    walked = returns.walk(
        repos, through=through, account=account, staleness_days=staleness_days
    )
    records = track_records(walked)
    table = PeriodTable(
        level=walked.level,
        account=account.name if account is not None else None,
        through=through,
        fiscal_year_end=fiscal,
        records=records,
        summary=tuple(_summary(walked, records, fiscal, through)),
        fiscal_years=tuple(_fiscal_years(walked, records, fiscal, through)),
    )
    if not any(row.result is not None for row in table.rows):
        first = next(row.refusal for row in table.rows if row.refusal is not None)
        raise first
    return table


def fiscal_year_end(repos: Repositories) -> FiscalYearEnd:
    """The portfolio's stored fiscal year end, refused unless a month end. ADR 0045 §2."""
    stored = repos.meta.get("fiscal_year_end")
    try:
        return FiscalYearEnd.parse(stored if stored is not None else "")
    except ValueError as exc:
        raise ValidationError(
            f"the portfolio's fiscal year end {exc}"
            if stored is not None
            else "the portfolio has no fiscal year end",
            code=E_FISCAL_YEAR_END,
            remedy=(
                "Annual and quarterly periods are derived from it (PORT-GIPS-A04), so "
                "none can be shown. It is set by `pt init`, which now accepts only the "
                "last day of a month, and no command changes it afterwards."
            ),
            value=stored,
        ) from None


def _summary(
    walked: Walk, records: Sequence[TrackRecord], fiscal: FiscalYearEnd, through: date
) -> Iterator[PeriodRow]:
    calendar_year = fiscal.month == 12
    current = len(records) - 1 if records else None
    for kind, label, after in (
        (PeriodKind.MONTH_TO_DATE, "Month to date", last_month_end_before(through)),
        (
            PeriodKind.QUARTER_TO_DATE,
            "Quarter to date",
            fiscal.last_quarter_end_before(through),
        ),
        (
            PeriodKind.YEAR_TO_DATE,
            "Year to date" if calendar_year else "Fiscal year to date",
            fiscal.last_end_before(through),
        ),
    ):
        yield _clipped(walked, records, current, Window(kind, label, after, through))
    for count in TRAILING_YEARS:
        label = f"{count} year" if count == 1 else f"{count} years"
        window = Window(
            PeriodKind.TRAILING, label, shift_years(through, -count), through, count
        )
        yield _trailing(walked, records, current, window)
    began = records[current].inception if current is not None else walked.anchor + _DAY
    yield _clipped(
        walked,
        records,
        current,
        Window(PeriodKind.SINCE_INCEPTION, "Since inception", began - _DAY, through),
    )


def _fiscal_years(
    walked: Walk, records: Sequence[TrackRecord], fiscal: FiscalYearEnd, through: date
) -> Iterator[PeriodRow]:
    """Each record's fiscal years, clipped to it. PORT-GIPS-H01, H02, A04.

    Complete years only, for a record still running: the year holding
    *through* is the year-to-date row. A record that ended has its last,
    partial year too, which no later day changes.
    """
    for index, record in enumerate(records):
        last = record.termination if record.termination is not None else through
        year_end = fiscal.in_year(record.inception.year)
        if year_end < record.inception:
            year_end = fiscal.in_year(record.inception.year + 1)
        while True:
            year_after = fiscal.in_year(year_end.year - 1)
            if year_after >= last or (record.termination is None and year_end > through):
                break
            label = str(year_end.year) if fiscal.month == 12 else f"FY{year_end.year}"
            window = Window(PeriodKind.FISCAL_YEAR, label, year_after, year_end)
            yield _measure(
                walked,
                window,
                max(window.first, record.inception),
                min(year_end, last),
                record=index,
            )
            year_end = fiscal.in_year(year_end.year + 1)


def _clipped(
    walked: Walk, records: Sequence[TrackRecord], current: int | None, window: Window
) -> PeriodRow:
    """A period-to-date row, or since inception, measured on the current record only."""
    if current is None:
        return _measure(walked, window, window.first, window.through, record=None)
    record = records[current]
    start = max(window.first, record.inception)
    end = window.through
    if record.termination is not None:
        end = min(end, record.termination)
    if start > end:
        # Wholly after the record ended: measured as asked, and refused as
        # holding nothing, as `pert twr` over it is.
        return _measure(walked, window, window.first, window.through, record=None)
    return _measure(walked, window, start, end, record=current)


def _trailing(
    walked: Walk, records: Sequence[TrackRecord], current: int | None, window: Window
) -> PeriodRow:
    """A fixed-length row: the whole window inside the current record, or null."""
    if current is not None:
        record = records[current]
        ended = record.termination is not None and record.termination < window.through
        if window.first < record.inception or ended:
            return _null(window, current, _outside_record(window, record))
    return _measure(walked, window, window.first, window.through, record=current)


def _measure(
    walked: Walk, window: Window, start: date, end: date, *, record: int | None
) -> PeriodRow:
    try:
        result = returns.window(walked, start=start, end=end)
    except PortableError as refusal:
        return _null(window, record, refusal)
    partial: list[str] = []
    if result.start > window.first:
        partial.append(INCEPTION if not record else RESTART)
    if result.end < window.through:
        partial.append(TERMINATION)
    held = [
        snapshot
        for (account_id, on), snapshot in walked.stored.items()
        if on == result.end and walked.begun[account_id] <= result.end
    ]
    with decimal.localcontext(RETURN_CONTEXT):
        ending = sum((snapshot.ending_market_value for snapshot in held), Decimal(0))
    return PeriodRow(
        window=window,
        record=record,
        result=result,
        refusal=None,
        partial=tuple(partial),
        annualized=tuple(_annualized(result)),
        ending_value=ending,
        accounts_holding_value=sum(1 for s in held if s.ending_market_value != 0),
    )


def _null(window: Window, record: int | None, refusal: PortableError) -> PeriodRow:
    return PeriodRow(window, record, None, refusal, (), (), None, None)


def _annualized(result: TimeWeightedReturn) -> Iterator[Annualized]:
    for entry in result.cumulative:
        try:
            value = annualize(entry.value, after=result.start - _DAY, through=result.end)
        except PortableError as refusal:
            yield Annualized(entry.basis, None, refusal)
        else:
            yield Annualized(entry.basis, value, None)


def _outside_record(window: Window, record: TrackRecord) -> ValidationError:
    if window.first < record.inception:
        why = f"starts before the track record began, on {record.inception.isoformat()}"
    else:
        assert record.termination is not None
        why = f"runs past the end of the track record, on {record.termination.isoformat()}"
    name = f"{window.years}-year" if window.years is not None else window.label.lower()
    return ValidationError(
        f"the {name} period from {window.first.isoformat()} to "
        f"{window.through.isoformat()} {why}",
        code=E_PERIOD_OUTSIDE_RECORD,
        remedy=(
            "A fixed-length period is shown only when the record covers all of it. "
            "Over fewer days its figure is a different number under the same label. "
            "Since inception covers the record."
        ),
        first=window.first.isoformat(),
        last=window.through.isoformat(),
        inception=record.inception.isoformat(),
        termination=record.termination.isoformat() if record.termination else None,
    )


# ── annualising ──────────────────────────────────────────────────────────────


def annualize(cumulative: Decimal, *, after: date, through: date) -> Decimal:
    """The annualised rate of *cumulative*, from the end of *after* to the end of *through*.

    ADR 0045 §5: ``exp(ln(1 + R) / N) - 1``, where N counts whole anniversaries
    and the part year left over against the year it falls in
    (:func:`~portable_core.domain.periods.years`). It is computed in
    :data:`RETURN_CONTEXT`. ``Decimal.ln`` and ``Decimal.exp`` are correctly
    rounded, which a non-integral ``**`` is not guaranteed to be, so every
    step's rounding is defined and so are the bytes (invariant 6). Over
    exactly one year the rate is the cumulative return itself. Computed
    through ``exp`` and ``ln``, it could differ from it in the last digit.

    Raises:
        GipsRefusalError: the period is under a year (``PORT-GIPS-B07``).
            Annualising it is prohibited outright, not discouraged.
        ValidationError: the cumulative return is -100% or below. No rate
            compounds to nothing, and none is invented.
    """
    if not at_least_a_year(after, through):
        raise GipsRefusalError(
            f"cannot annualize the return from {after.isoformat()} to "
            f"{through.isoformat()}: the period is shorter than a year",
            requirement="PORT-GIPS-B07",
            code=E_GIPS_ANNUALIZE_SUB_YEAR,
            remedy=(
                "Present the cumulative return for the period instead. Annualizing "
                "a sub-year return is prohibited outright, not merely discouraged."
            ),
            after=after.isoformat(),
            through=through.isoformat(),
        )
    length = years(after, through)
    with decimal.localcontext(RETURN_CONTEXT):
        growth = _ONE + cumulative
        if growth <= 0:
            raise ValidationError(
                f"the return from {after.isoformat()} to {through.isoformat()} is "
                f"{cumulative}, and a loss of everything has no annualized rate",
                code=E_RETURN_TOTAL_LOSS,
                remedy="Present the cumulative return. Nothing is clamped or estimated.",
                cumulative=str(cumulative),
            )
        if (length.whole, length.days) == (1, 0):
            return cumulative
        count = Decimal(length.whole) + Decimal(length.days) / Decimal(length.of)
        return (growth.ln() / count).exp() - _ONE
