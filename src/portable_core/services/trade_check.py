"""Each recorded fill against what the market traded that day. ADR 0020 R10.

A recorded trade's price is itself a traded price on its date, which makes it
a cheap check on the ledger and the price history together. A fill outside the
day's range is a wrong price, a wrong date, a wrong instrument, or a trade
outside regular hours. Which one is a person's call, so a flag is for review
and blocks nothing (the owner's decision, 2026-10-20).

* **Stocks, ETFs and ADRs** are checked against the day's raw low and high,
  read from the provider when checked and never stored.
* **Mutual funds** trade once a day, at that day's NAV. A fill is checked
  against the price portable holds for that day, to the cent, so the check
  covers the NAV history too.
* **Options, bonds and anything else** are listed, not checked: no range for
  them is held, and a bond trades away from any close.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from enum import StrEnum

from portable_core.decimals import to_text
from portable_core.domain.enums import InstrumentType, TransactionType
from portable_core.domain.models import Instrument, Transaction
from portable_core.errors import DataUnavailableError
from portable_core.errors.kinds import E_PRICE_MISSING
from portable_core.persistence.repositories import Repositories
from portable_core.providers.base import DailyRange, DailyRangeCapability
from portable_core.services.price_history import Span, ticker_spans
from portable_core.services.replay import effective_ledger

__all__ = ["Checked", "TradeLine", "Verdict", "check"]

#: The ledger rows that record a fill at a price.
TRADES = frozenset(
    {
        TransactionType.BUY,
        TransactionType.SELL,
        TransactionType.SELL_SHORT,
        TransactionType.BUY_TO_COVER,
        TransactionType.DIVIDEND_REINVEST,
    }
)
#: Checked against the day's range.
RANGED = frozenset({InstrumentType.EQUITY, InstrumentType.ETF, InstrumentType.ADR})
#: A fund fill agrees with the NAV when both are the same to the cent.
HALF_CENT = Decimal("0.005")


class Verdict(StrEnum):
    WITHIN = "within"
    OUTSIDE = "outside"
    AT_NAV = "at_nav"
    OFF_NAV = "off_nav"
    #: Checkable, but nothing to check against: no range, or no NAV.
    NO_DATA = "no_data"
    NOT_CHECKED = "not_checked"


#: The verdicts a person should look at.
FLAGGED = frozenset({Verdict.OUTSIDE, Verdict.OFF_NAV, Verdict.NO_DATA})


@dataclass(frozen=True, slots=True)
class TradeLine:
    """One fill, and what it was checked against."""

    txn_id: int
    account: str
    trade_date: date
    txn_type: str
    symbol: str
    quantity: Decimal | None
    price: Decimal
    #: The day's range, for a stock; ``None`` where none was checked.
    low: Decimal | None
    high: Decimal | None
    #: The day's NAV, for a fund.
    nav: Decimal | None
    verdict: Verdict
    note: str | None

    @property
    def flagged(self) -> bool:
        return self.verdict in FLAGGED


@dataclass(frozen=True, slots=True)
class Checked:
    lines: tuple[TradeLine, ...]

    @property
    def flagged(self) -> tuple[TradeLine, ...]:
        return tuple(line for line in self.lines if line.flagged)

    def counts(self) -> dict[str, int]:
        counts = {str(verdict): 0 for verdict in Verdict}
        for line in self.lines:
            counts[str(line.verdict)] += 1
        return counts


def check(
    repos: Repositories,
    ranges: DailyRangeCapability,
    first: date,
    last: date,
    *,
    account_id: int | None = None,
) -> Checked:
    """Every fill from *first* to *last*, in ledger order, with its verdict.

    Reversed entries are left out: they did not happen (ADR 0021).

    Raises:
        DataUnavailableError: the provider cannot be reached or cannot do
            this. A provider with no data for one security is a verdict on
            that security's fills, not an error.
    """
    # (fill, instrument id, price): both known for every row kept.
    fills = [
        (txn, txn.instrument_id, txn.price)
        for txn in effective_ledger(repos.transactions.in_ledger_order())
        if txn.txn_type in TRADES
        and first <= txn.trade_date <= last
        and (account_id is None or txn.account_id == account_id)
        and txn.instrument_id is not None
        and txn.price is not None
    ]
    names = {account.account_id: account.name for account in repos.accounts.all()}
    instruments: dict[int, Instrument] = {}
    by_instrument: dict[int, list[Transaction]] = defaultdict(list)
    for txn, instrument_id, _ in fills:
        found = instruments.get(instrument_id) or repos.instruments.get(instrument_id)
        if found is not None:
            instruments[instrument_id] = found
            by_instrument[instrument_id].append(txn)
    days = {
        instrument_id: _ranges(repos, ranges, instruments[instrument_id], dated)
        for instrument_id, dated in sorted(by_instrument.items())
        if _ranged(instruments[instrument_id])
    }

    lines: list[TradeLine] = []
    for txn, instrument_id, price in fills:
        instrument = instruments[instrument_id]
        line = TradeLine(
            txn_id=txn.txn_id,
            account=names.get(txn.account_id, str(txn.account_id)),
            trade_date=txn.trade_date,
            txn_type=str(txn.txn_type),
            symbol=instrument.symbol,
            quantity=txn.quantity,
            price=price,
            low=None,
            high=None,
            nav=None,
            verdict=Verdict.NOT_CHECKED,
            note=None,
        )
        if instrument.instrument_type is InstrumentType.MUTUAL_FUND:
            lines.append(_at_nav(repos, line, instrument))
        elif instrument_id in days:
            found_days, why = days[instrument_id]
            lines.append(_within(line, found_days.get(txn.trade_date), why.get(txn.trade_date)))
        else:
            lines.append(_not_checked(line, instrument))
    return Checked(tuple(lines))


def _ranged(instrument: Instrument) -> bool:
    return instrument.instrument_type in RANGED and instrument.has_market_price


def _ranges(
    repos: Repositories,
    provider: DailyRangeCapability,
    instrument: Instrument,
    trades: list[Transaction],
) -> tuple[dict[date, DailyRange], dict[date, str]]:
    """The day's range for each date *instrument* traded, and why any is missing.

    Asked for under the ticker it carried on each date, from its first fill
    to its last in one request per ticker. When that request finds nothing,
    each date is asked for alone, so one date the provider cannot answer does
    not hide the rest.
    """
    dates = sorted({txn.trade_date for txn in trades})
    found: dict[date, DailyRange] = {}
    missing: dict[date, str] = {}
    for symbol, run in ticker_spans(repos, instrument, Span(dates[0], dates[-1])):
        wanted = [d for d in dates if run.start <= d <= run.end]
        if not wanted:
            continue
        try:
            found.update(
                (r.price_date, r) for r in provider.daily_ranges(symbol, wanted[0], wanted[-1])
            )
            continue
        except DataUnavailableError as exc:
            if exc.code != E_PRICE_MISSING:
                raise
        for on in wanted:
            try:
                found.update((r.price_date, r) for r in provider.daily_ranges(symbol, on, on))
            except DataUnavailableError as exc:
                if exc.code != E_PRICE_MISSING:
                    raise
                missing[on] = exc.message
    return found, missing


def _within(line: TradeLine, day: DailyRange | None, why: str | None) -> TradeLine:
    if day is None:
        return replace(
            line,
            verdict=Verdict.NO_DATA,
            note=why or f"no range for {line.symbol} on {line.trade_date.isoformat()}",
        )
    checked = replace(line, low=day.low, high=day.high)
    if day.low <= line.price <= day.high:
        return replace(checked, verdict=Verdict.WITHIN)
    if line.price > day.high:
        note = f"{to_text(line.price - day.high)} above the day's high of {to_text(day.high)}"
    else:
        note = f"{to_text(day.low - line.price)} below the day's low of {to_text(day.low)}"
    return replace(checked, verdict=Verdict.OUTSIDE, note=note)


def _at_nav(repos: Repositories, line: TradeLine, instrument: Instrument) -> TradeLine:
    nav = repos.prices.on_date(instrument.instrument_id, line.trade_date)
    if nav is None:
        return replace(
            line,
            verdict=Verdict.NO_DATA,
            note=f"no NAV held for {line.trade_date.isoformat()}",
        )
    checked = replace(line, nav=nav.price)
    if abs(line.price - nav.price) <= HALF_CENT:
        return replace(checked, verdict=Verdict.AT_NAV)
    side = "above" if line.price > nav.price else "below"
    return replace(
        checked,
        verdict=Verdict.OFF_NAV,
        note=f"{to_text(abs(line.price - nav.price))} {side} the NAV of "
        f"{to_text(nav.price)} ({nav.source})",
    )


def _not_checked(line: TradeLine, instrument: Instrument) -> TradeLine:
    if not instrument.has_market_price:
        why = "marked as having no market price"
    else:
        why = f"{str(instrument.instrument_type).replace('_', ' ')} fills are not checked"
    return replace(line, note=why)
