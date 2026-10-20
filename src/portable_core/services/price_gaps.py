"""Which held trading days have no price: the coverage report. ADR 0020 R3, R9.

A valuation tolerates a price a few days old, the staleness tolerance. That is
right for ongoing use and wrong for judging a history, because it hides a
missing day behind the one before it, and a gap surfaced only as one
incomplete account-day at a time. This report asks the stricter question
once, across the whole history: for every day an account held an
instrument, was the market open, and if so, is there a price for that day?

"Was the market open" is the venue's trading calendar, not the weekday: a
holiday is not a gap, and a missing Tuesday is (R9). A calendar covers a span
of dates; outside it nothing is known, so those days are reported as
uncovered rather than counted either way. Any source's price counts: the
question is whether the day can be valued, not where the price came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from portable_core.domain.models import Instrument
from portable_core.persistence.repositories import Repositories
from portable_core.providers.base import TradingCalendar, TradingCalendarCapability
from portable_core.services.price_history import Span, clip, held_spans

__all__ = ["Coverage", "Gap", "Uncovered", "report"]


@dataclass(frozen=True, slots=True)
class Gap:
    """Consecutive sessions an instrument was held with no price for any of them."""

    symbol: str
    #: The venue whose calendar said these were sessions.
    calendar: str
    start: date
    end: date
    sessions: int

    def describe(self) -> str:
        noun = "session" if self.sessions == 1 else "sessions"
        return f"{self.symbol} {_span(self.start, self.end)}: {self.sessions} {noun} unpriced"


@dataclass(frozen=True, slots=True)
class Uncovered:
    """Days an instrument was held that its calendar does not reach."""

    symbol: str
    calendar: str
    start: date
    end: date

    def describe(self) -> str:
        return (
            f"{self.symbol} {_span(self.start, self.end)}: outside the {self.calendar} "
            "calendar, so not checked"
        )


@dataclass(frozen=True, slots=True)
class Coverage:
    """The report: what was checked, and every run of sessions with no price."""

    gaps: tuple[Gap, ...]
    uncovered: tuple[Uncovered, ...]
    #: Held, and marked as having no market price: valued at zero at level 5,
    #: so never a gap (ADR 0028).
    unquoted: tuple[str, ...]
    #: Instruments checked, held sessions checked, and how many had a price.
    instruments: int
    sessions: int
    priced: int
    #: Each venue on record (``None`` for none) and the calendar it was judged
    #: by, which differs when the venue has no calendar of its own.
    calendars: tuple[tuple[str | None, str], ...]
    #: Ledger rows the replay could not apply, which `pt validate` reports too.
    warnings: tuple[str, ...]

    @property
    def unpriced(self) -> int:
        return self.sessions - self.priced


def report(
    repos: Repositories, calendars: TradingCalendarCapability, first: date, last: date
) -> Coverage:
    """Every held session from *first* to *last* with no price, as runs.

    Held means at the end of the day, read from the ledger by one replay, as
    `pt price load` reads it (ADR 0020 R2). Ordered by symbol, then date.

    Raises:
        DataUnavailableError: the provider has no calendar for a venue, nor
            the one it falls back to.
    """
    spans, warnings = held_spans(repos, through=last)
    held: list[tuple[Instrument, list[Span]]] = []
    for instrument_id, series in spans.items():
        instrument = repos.instruments.get(instrument_id)
        clipped = clip(series, first, last)
        if instrument is not None and clipped:
            held.append((instrument, clipped))
    held.sort(key=lambda pair: (pair[0].symbol, pair[0].instrument_id))

    by_venue: dict[str | None, TradingCalendar] = {}
    gaps: list[Gap] = []
    uncovered: list[Uncovered] = []
    unquoted: list[str] = []
    sessions = priced = 0
    for instrument, series in held:
        if not instrument.has_market_price:
            unquoted.append(instrument.symbol)
            continue
        if instrument.exchange not in by_venue:
            by_venue[instrument.exchange] = calendars.trading_calendar(
                instrument.exchange, first, last
            )
        calendar = by_venue[instrument.exchange]
        for span in series:
            uncovered.extend(
                Uncovered(instrument.symbol, calendar.exchange, part.start, part.end)
                for part in _outside(span, calendar)
            )
            open_days = sorted(d for d in calendar.open_days if span.start <= d <= span.end)
            dated = {
                p.price_date
                for p in repos.prices.series(instrument.instrument_id, span.start, span.end)
            }
            sessions += len(open_days)
            priced += sum(1 for d in open_days if d in dated)
            gaps.extend(_runs(instrument.symbol, calendar.exchange, open_days, dated))

    return Coverage(
        gaps=tuple(gaps),
        uncovered=tuple(uncovered),
        unquoted=tuple(unquoted),
        instruments=len(held) - len(unquoted),
        sessions=sessions,
        priced=priced,
        calendars=tuple(
            (venue, c.exchange)
            for venue, c in sorted(by_venue.items(), key=lambda pair: pair[0] or "")
        ),
        warnings=warnings,
    )


def _runs(symbol: str, calendar: str, open_days: list[date], dated: set[date]) -> list[Gap]:
    """Unpriced sessions, grouped where no priced session falls between them."""
    runs: list[Gap] = []
    start: date | None = None
    previous: date | None = None
    count = 0
    for day in [*open_days, None]:
        if day is not None and day not in dated:
            if start is None:
                start = day
            previous, count = day, count + 1
            continue
        if start is not None and previous is not None:
            runs.append(Gap(symbol, calendar, start, previous, count))
        start, previous, count = None, None, 0
    return runs


def _outside(span: Span, calendar: TradingCalendar) -> list[Span]:
    """The parts of *span* before the calendar's first day or after its last."""
    parts: list[Span] = []
    if span.start < calendar.first:
        parts.append(Span(span.start, min(span.end, calendar.first - timedelta(days=1))))
    if span.end > calendar.last:
        parts.append(Span(max(span.start, calendar.last + timedelta(days=1)), span.end))
    return parts


def _span(start: date, end: date) -> str:
    return start.isoformat() if start == end else f"{start.isoformat()} to {end.isoformat()}"
