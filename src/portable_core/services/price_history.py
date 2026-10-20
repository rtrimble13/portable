"""What to ask a market data provider for, to price a history (ADR 0020).

A provider is asked for a ticker over a range of dates. For a history, both
halves of that question come from the portfolio, not from the provider:

* **When** an instrument needs a price: the days some account held it, from
  the ledger, through the same replay every other derived figure uses
  (:func:`~portable_core.services.replay.over_dates`, ADR 0021). Not the
  instruments held *today*, which leaves out everything since sold -- the very
  positions a historical return has to value (ADR 0020 R2).
* **Which ticker** it traded under on each of those days: portable's own
  symbol history. Asking for today's ticker across a rename asks about the
  days before it, when that ticker named nothing -- or named someone else.

The provider then resolves each ticker as of each day (ADR 0006); this module
decides what to ask, and whether an answer may be stored beside what another
source already said (:func:`conflicts`).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from portable_core.decimals import to_text
from portable_core.domain.models import Instrument, Price
from portable_core.errors import ValidationError
from portable_core.persistence.repositories import Repositories
from portable_core.services.replay import effective_ledger, over_dates

__all__ = [
    "Conflict",
    "OnConflict",
    "PriceRequest",
    "Span",
    "clip",
    "conflicts",
    "differences",
    "held_spans",
    "lookups_when_last_held",
    "requests_for",
    "requests_while_held",
    "ticker_spans",
]


@dataclass(frozen=True, slots=True)
class Span:
    """Consecutive days, both ends inclusive."""

    start: date
    end: date


@dataclass(frozen=True, slots=True)
class PriceRequest:
    """One question for a provider: *symbol*, from *start* to *end*."""

    instrument: Instrument
    #: The ticker the instrument carried throughout the span.
    symbol: str
    start: date
    end: date
    #: The days just before a held span, asked for so that its first day can
    #: be valued from the previous close when it is not a trading day. Nothing
    #: is wrong when a lead-in has no prices; the instrument may not have
    #: existed yet.
    lead_in: bool = False


class OnConflict(StrEnum):
    """What to do with a price another source already disagrees with."""

    #: Store nothing of that request, and fail the command (the default).
    REFUSE = "refuse"
    #: Keep the stored price; store the rest of the request.
    KEEP = "keep"
    #: Store the new price beside the old. It is newer, so valuations use it.
    REPLACE = "replace"


@dataclass(frozen=True, slots=True)
class Conflict:
    """Two sources' prices for one instrument on one day, which differ."""

    symbol: str
    stored: Price
    offered: Price

    def describe(self) -> str:
        return (
            f"{self.symbol} {self.offered.price_date.isoformat()}: {self.stored.source} "
            f"has {to_text(self.stored.price)}, {self.offered.source} has "
            f"{to_text(self.offered.price)}"
        )


def conflicts(
    repos: Repositories, symbol: str, instrument_id: int, offered: Sequence[Price]
) -> list[Conflict]:
    """The prices in *offered* that a different source already prices differently.

    The price cache keeps one price per source per day, and a valuation uses
    the one written last. Without this check, two sources that disagree -- the
    warehouse and a file filling its gaps -- would be settled by the order the
    commands happened to run in, with nothing to say a choice was made
    (`CLAUDE.md` invariant 9). Prices are compared as numbers, so ``189.12``
    and ``189.120000`` agree. A new price from the *same* source replaces its
    old one, as a final price replaces a preliminary one (``PORT-GIPS-A09``),
    and is not a conflict.
    """
    if not offered:
        return []
    stored: dict[date, list[Price]] = {}
    for price in repos.prices.series(
        instrument_id,
        min(p.price_date for p in offered),
        max(p.price_date for p in offered),
    ):
        stored.setdefault(price.price_date, []).append(price)
    return [
        Conflict(symbol, held, price)
        for price in offered
        for held in stored.get(price.price_date, ())
        if held.source != price.source and held.price != price.price
    ]


def held_spans(
    repos: Repositories, *, through: date
) -> tuple[dict[int, list[Span]], tuple[str, ...]]:
    """Per instrument id, the days some account held it at the end of the day.

    Read from derived state at the end of every date the ledger changes, by
    one replay (ADR 0021); between two such dates nothing held can change.
    Returns the spans and the replay's warnings -- ledger rows it could not
    apply, which `pt validate` reports too.
    """
    changes = sorted(
        {
            txn.trade_date
            for txn in effective_ledger(repos.transactions.in_ledger_order())
            if txn.trade_date <= through
        }
    )
    if not changes:
        return {}, ()

    def held(_on: date) -> frozenset[int]:
        return frozenset(
            leg.instrument_id
            for position in repos.positions.all(open_only=True)
            for leg in position.legs
            if leg.quantity != 0
        )

    walked = over_dates(repos, changes, held)
    spans: dict[int, list[Span]] = {}
    for position, (on, instruments) in enumerate(zip(changes, walked.results, strict=True)):
        last = (
            changes[position + 1] - timedelta(days=1)
            if position + 1 < len(changes)
            else through
        )
        for instrument_id in instruments:
            series = spans.setdefault(instrument_id, [])
            if series and series[-1].end + timedelta(days=1) == on:
                series[-1] = Span(series[-1].start, last)
            else:
                series.append(Span(on, last))
    return spans, walked.warnings


def ticker_spans(
    repos: Repositories, instrument: Instrument, span: Span
) -> list[tuple[str, Span]]:
    """The ticker *instrument* carried on each day of *span*, as runs of days.

    From portable's own symbol history. A rename records the old symbol as
    valid *through* the effective date and the new one *from* it, so on that
    one day both cover; the newer wins, as it does when resolving a symbol.

    Raises:
        ValidationError: some day has no symbol at all -- a gap in portable's
            own reference data, which no provider can be asked around.
    """
    periods = repos.instruments.symbol_history(instrument.instrument_id)
    if not periods:
        return [(instrument.symbol, span)]

    boundaries = {span.start}
    for _, valid_from, valid_to in periods:
        if span.start < valid_from <= span.end:
            boundaries.add(valid_from)
        if valid_to is not None and span.start <= valid_to < span.end:
            boundaries.add(valid_to + timedelta(days=1))
    ordered = sorted(boundaries)

    runs: list[tuple[str, Span]] = []
    for position, first in enumerate(ordered):
        last = (
            ordered[position + 1] - timedelta(days=1)
            if position + 1 < len(ordered)
            else span.end
        )
        covering = [p for p in periods if p[1] <= first and (p[2] is None or p[2] >= first)]
        if not covering:
            raise ValidationError(
                f"instrument {instrument.symbol} has no symbol on record for "
                f"{first.isoformat()}",
                remedy="Its symbol history has a gap; record the symbol it traded under.",
                instrument_id=instrument.instrument_id,
                date=first.isoformat(),
            )
        symbol = max(covering, key=lambda p: p[1])[0]
        if runs and runs[-1][0] == symbol:
            runs[-1] = (symbol, Span(runs[-1][1].start, last))
        else:
            runs.append((symbol, Span(first, last)))
    return runs


def requests_for(
    repos: Repositories, instruments: Iterable[Instrument], first: date, last: date
) -> list[PriceRequest]:
    """Each named instrument over the whole of *first*..*last*, ticker by ticker."""
    return [
        PriceRequest(instrument, symbol, run.start, run.end)
        for instrument in instruments
        for symbol, run in ticker_spans(repos, instrument, Span(first, last))
    ]


def requests_while_held(
    repos: Repositories, first: date, last: date, *, lead_in_days: int
) -> tuple[list[PriceRequest], tuple[str, ...]]:
    """Every instrument held at some point in *first*..*last*, over those days only.

    Each held stretch is preceded by a *lead_in_days* request, so a stretch
    that begins on a weekend or a holiday -- a cutover, an in-kind transfer,
    a `--from` that is not a trading day -- can be valued from the previous
    close. Ordered by symbol, then date, so output is stable.
    """
    spans, warnings = held_spans(repos, through=last)
    requests: list[PriceRequest] = []
    for instrument_id, series in spans.items():
        instrument = repos.instruments.get(instrument_id)
        if instrument is None:  # pragma: no cover -- a foreign key prevents it
            continue
        for held in clip(series, first, last):
            if lead_in_days > 0:
                lead = Span(
                    held.start - timedelta(days=lead_in_days), held.start - timedelta(days=1)
                )
                requests.extend(
                    PriceRequest(instrument, symbol, run.start, run.end, lead_in=True)
                    for symbol, run in ticker_spans(repos, instrument, lead)
                )
            requests.extend(
                PriceRequest(instrument, symbol, run.start, run.end)
                for symbol, run in ticker_spans(repos, instrument, held)
            )
    requests.sort(key=lambda r: (r.instrument.symbol, r.start, not r.lead_in))
    return requests, warnings


def clip(series: Sequence[Span], first: date, last: date) -> list[Span]:
    """The parts of *series* from *first* to *last*."""
    return [
        Span(max(span.start, first), min(span.end, last))
        for span in series
        if span.end >= first and span.start <= last
    ]


def lookups_when_last_held(
    repos: Repositories, *, through: date
) -> tuple[list[tuple[Instrument, str, date]], tuple[str, ...]]:
    """Each instrument held through *through*, with the ticker and day to look it up by.

    The day is the last one it was held -- *through* itself if it still is --
    and the ticker the one it carried that day. Looked up as of today, a ticker
    sold years ago can name another issuer, or nothing (ADR 0020 R4, R5).
    Ordered by symbol.
    """
    spans, warnings = held_spans(repos, through=through)
    lookups: list[tuple[Instrument, str, date]] = []
    for instrument_id, series in spans.items():
        instrument = repos.instruments.get(instrument_id)
        if instrument is None:  # pragma: no cover -- a foreign key prevents it
            continue
        last = series[-1].end
        ticker = ticker_spans(repos, instrument, Span(last, last))[0][0]
        lookups.append((instrument, ticker, last))
    lookups.sort(key=lambda lookup: lookup[0].symbol)
    return lookups, warnings


#: What a security-master comparison looks at. The symbol is left out: a
#: ticker that has changed since is expected to differ, and the lookup has
#: already followed it.
_COMPARED: tuple[str, ...] = ("instrument_type", "name", "exchange", "cusip", "isin", "country")


def differences(here: Instrument, there: Instrument) -> list[str]:
    """Fields *there* states differently from *here*, as ``field: here -> there``.

    A field the provider leaves empty is not a difference: absence is not a
    claim. One the portfolio leaves empty and the provider fills is, because it
    is information the local record lacks. Neither side is assumed right.
    """
    found: list[str] = []
    for field in _COMPARED:
        ours, theirs = getattr(here, field), getattr(there, field)
        if theirs is not None and ours != theirs:
            found.append(f"{field}: {ours if ours is not None else '-'} -> {theirs}")
    return found
