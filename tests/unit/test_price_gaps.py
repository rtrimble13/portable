"""Which held trading days have no price. ADR 0020 R3, R9.

Before: a missing price surfaced only when `pt value` found one account-day
incomplete, one day at a time. The staleness tolerance hid most of them behind
the day before. And nothing asked a calendar, so a holiday and a missing
Tuesday looked the same.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from portable_core.cli import context
from portable_core.domain.enums import InstrumentType, TransactionType
from portable_core.domain.models import Account, Instrument, Price
from portable_core.persistence.repositories import Repositories
from portable_core.providers import Capability, MarketDataProvider, TradingCalendar
from portable_core.services import price_gaps
from portable_core.services.price_gaps import Gap, Uncovered
from portable_core.services.replay import ReplayEngine
from portable_pt.app import app
from tests.conftest import append

pytestmark = pytest.mark.unit

D = Decimal

#: January 2024. Monday the 1st and Monday the 15th are holidays.
HOLIDAYS = {date(2024, 1, 1), date(2024, 1, 15)}


def _sessions(first: date, last: date) -> frozenset[date]:
    days = (first + timedelta(days=n) for n in range((last - first).days + 1))
    return frozenset(d for d in days if d.weekday() < 5 and d not in HOLIDAYS)


class _Calendars(MarketDataProvider):
    """A provider that knows calendars and nothing else."""

    name = "calendars"

    def __init__(self, *, first: date = date(2024, 1, 1), last: date = date(2024, 12, 31)):
        self.first, self.last = first, last
        self.asked: list[str | None] = []

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.TRADING_CALENDAR})

    def trading_calendar(self, exchange: str | None, start: date, end: date) -> TradingCalendar:
        self.asked.append(exchange)
        venue = exchange if exchange in {"NYSE", "NASDAQ"} else "NASDAQ"
        lo, hi = max(start, self.first), min(end, self.last)
        return TradingCalendar(
            exchange=venue,
            open_days=_sessions(lo, hi) if lo <= hi else frozenset(),
            first=self.first,
            last=self.last,
            source="test",
        )


def _price(repos: Repositories, instrument: Instrument, *days: date, source: str = "a") -> None:
    for on in days:
        repos.prices.add(
            Price(
                instrument_id=instrument.instrument_id,
                price_date=on,
                price=D("100.00"),
                source=source,
                as_of=datetime.combine(on, datetime.min.time(), tzinfo=UTC),
            )
        )


def _hold(
    repos: Repositories, account: Account, instrument: Instrument, buy: date, sell: date
) -> None:
    append(repos, account.account_id, TransactionType.BUY, buy,
           instrument_id=instrument.instrument_id, quantity=D("10"), price=D("100.00"),
           gross_amount=D("1000.00"), net_cash_effect=D("-1000.00"))  # fmt: skip
    append(repos, account.account_id, TransactionType.SELL, sell,
           instrument_id=instrument.instrument_id, quantity=D("10"), price=D("100.00"),
           gross_amount=D("1000.00"), net_cash_effect=D("1000.00"))  # fmt: skip


@pytest.fixture
def held(repos: Repositories, taxable_account: Account, aapl: Instrument) -> Repositories:
    """AAPL held at the close of Wednesday 3 January through Thursday 11 January."""
    append(repos, taxable_account.account_id, TransactionType.DEPOSIT, date(2024, 1, 2),
           net_cash_effect=D("100000.00"))  # fmt: skip
    _hold(repos, taxable_account, aapl, date(2024, 1, 3), date(2024, 1, 12))
    assert ReplayEngine(repos).rebuild().warnings == ()
    return repos


def _report(repos: Repositories, calendars: _Calendars | None = None) -> price_gaps.Coverage:
    return price_gaps.report(
        repos, calendars or _Calendars(), date(2024, 1, 1), date(2024, 1, 31)
    )


def test_r9_unpriced_sessions_are_gaps_and_weekends_are_not(
    held: Repositories, aapl: Instrument
) -> None:
    _price(held, aapl, date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 8), date(2024, 1, 11))

    coverage = _report(held)

    assert coverage.gaps == (
        Gap("AAPL", "NASDAQ", date(2024, 1, 5), date(2024, 1, 5), 1),
        Gap("AAPL", "NASDAQ", date(2024, 1, 9), date(2024, 1, 10), 2),
    ), "Saturday and Sunday the 6th and 7th join nothing: they are not sessions"
    assert (coverage.sessions, coverage.priced, coverage.unpriced) == (7, 4, 3)
    assert coverage.instruments == 1


def test_r9_a_holiday_is_not_a_gap_and_does_not_split_one(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    append(repos, taxable_account.account_id, TransactionType.DEPOSIT, date(2024, 1, 2),
           net_cash_effect=D("100000.00"))  # fmt: skip
    _hold(repos, taxable_account, aapl, date(2024, 1, 11), date(2024, 1, 18))
    ReplayEngine(repos).rebuild()
    _price(repos, aapl, date(2024, 1, 11), date(2024, 1, 17))

    assert _report(repos).gaps == (
        Gap("AAPL", "NASDAQ", date(2024, 1, 12), date(2024, 1, 16), 2),
    ), "Friday the 12th and Tuesday the 16th, across the weekend and the 15th"


def test_a_price_from_any_source_counts(held: Repositories, aapl: Instrument) -> None:
    days = sorted(_sessions(date(2024, 1, 3), date(2024, 1, 11)))
    _price(held, aapl, *days[:4], source="fafnir:duk ph")
    _price(held, aapl, *days[4:], source="file:prices.csv")

    coverage = _report(held)

    assert coverage.gaps == ()
    assert coverage.priced == coverage.sessions == 7


def test_days_outside_the_calendar_are_unchecked_not_gaps(
    held: Repositories, aapl: Instrument
) -> None:
    """No row outside a calendar's span means unknown, not closed (fafnir 0022)."""
    coverage = _report(held, _Calendars(first=date(2024, 1, 8)))

    assert coverage.uncovered == (
        Uncovered("AAPL", "NASDAQ", date(2024, 1, 3), date(2024, 1, 7)),
    )
    assert coverage.gaps == (Gap("AAPL", "NASDAQ", date(2024, 1, 8), date(2024, 1, 11), 4),)


def test_the_range_clips_what_is_checked(held: Repositories, aapl: Instrument) -> None:
    coverage = price_gaps.report(held, _Calendars(), date(2024, 1, 9), date(2024, 1, 31))

    assert coverage.gaps == (Gap("AAPL", "NASDAQ", date(2024, 1, 9), date(2024, 1, 11), 3),)


def test_each_venue_is_judged_by_its_own_calendar_asked_for_once(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    ids = [
        repos.instruments.add(
            Instrument(
                instrument_id=0,
                symbol=symbol,
                instrument_type=InstrumentType.EQUITY,
                exchange=venue,
            )
        )
        for symbol, venue in (("IBM", "NYSE"), ("KO", "NYSE"), ("VFIAX", None))
    ]
    append(repos, taxable_account.account_id, TransactionType.DEPOSIT, date(2024, 1, 2),
           net_cash_effect=D("100000.00"))  # fmt: skip
    for instrument_id in ids:
        found = repos.instruments.get(instrument_id)
        assert found is not None
        _hold(repos, taxable_account, found, date(2024, 1, 3), date(2024, 1, 5))
    ReplayEngine(repos).rebuild()
    calendars = _Calendars()

    coverage = _report(repos, calendars)

    assert calendars.asked == ["NYSE", None], (
        "by symbol: IBM, then KO from the cache, then VFIAX"
    )
    assert coverage.calendars == ((None, "NASDAQ"), ("NYSE", "NYSE"))
    assert [(g.symbol, g.calendar) for g in coverage.gaps] == [
        ("IBM", "NYSE"),
        ("KO", "NYSE"),
        ("VFIAX", "NASDAQ"),
    ]


def test_adr_0028_an_instrument_with_no_market_price_is_listed_not_checked(
    held: Repositories, aapl: Instrument
) -> None:
    held.instruments.set_market_price(aapl.instrument_id, has_market_price=False)

    coverage = _report(held)

    assert coverage.unquoted == ("AAPL",)
    assert coverage.gaps == ()
    assert coverage.instruments == 0


def test_nothing_held_reports_nothing(held: Repositories) -> None:
    coverage = price_gaps.report(held, _Calendars(), date(2024, 3, 1), date(2024, 3, 31))

    assert (coverage.gaps, coverage.sessions, coverage.instruments) == ((), 0, 0)


# ── the command, in-process with a calendar provider ─────────────────────────


@pytest.fixture
def book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(context, "build_provider", lambda *_a, **_k: _Calendars())
    port = str(tmp_path / "g.port")
    for args in (
        ["init", port, "--name", "G", "--inception", "2024-01-02"],
        ["--port", port, "account", "add", "--name", "B", "--type", "tax-deferred",
         "--opened", "2024-01-02", "--relief-method", "fifo"],
        ["--port", port, "cash", "deposit", "-a", "B", "--amount", "10000",
         "--date", "2024-01-02"],
        ["--port", port, "instrument", "add", "AAPL", "--type", "equity"],
        ["--port", port, "buy", "AAPL", "-a", "B", "--qty", "10", "--price", "100.00",
         "--date", "2024-01-03"],
        ["--port", port, "sell", "AAPL", "-a", "B", "--qty", "10", "--price", "100.00",
         "--date", "2024-01-12"],
        ["--port", port, "price", "set", "AAPL", "--date", "2024-01-03",
         "--price", "100.00"],
    ):  # fmt: skip
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 0, result.output
    return port


def test_pt_price_gaps_reports_from_the_ledgers_first_date(book: str) -> None:
    result = CliRunner().invoke(
        app,
        ["--format", "json", "--port", book, "--source", "fafnir", "--as-of", "2024-01-31",
         "price", "gaps"],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["from"] == "2024-01-02"
    assert data["rows"] == [
        {"symbol": "AAPL", "calendar": "NASDAQ", "from": "2024-01-04", "to": "2024-01-11",
         "sessions": 6},
    ]  # fmt: skip
    assert (data["sessions"], data["priced"], data["unpriced"]) == (7, 1, 6)
    assert data["calendars"] == [{"venue": None, "calendar": "NASDAQ"}]


def test_pt_price_gaps_without_a_calendar_is_refused(book: str) -> None:
    result = CliRunner().invoke(
        app, ["--format", "json", "--port", book, "--offline", "price", "gaps"]
    )

    assert result.exit_code == 5, result.output
