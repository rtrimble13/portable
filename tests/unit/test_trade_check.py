"""Each recorded fill against what the market traded that day. ADR 0020 R10.

The owner's decision (2026-10-20): a stock's fill within the day's low and
high, read when checked and never stored; a fund's at the day's NAV to the
cent; options and bonds listed, not checked. Flags are for review.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from portable_core.cli import context
from portable_core.domain.enums import InstrumentType, TransactionType
from portable_core.domain.models import Account, Instrument, Price
from portable_core.errors import DataUnavailableError
from portable_core.errors.kinds import E_PRICE_MISSING, E_PROVIDER_UNAVAILABLE
from portable_core.persistence.repositories import Repositories
from portable_core.providers import Capability, DailyRange, MarketDataProvider
from portable_core.services import trade_check
from portable_core.services.trade_check import Verdict
from portable_pt.app import app
from tests.conftest import append

pytestmark = pytest.mark.unit

D = Decimal
MON, TUE, WED = date(2024, 1, 8), date(2024, 1, 9), date(2024, 1, 10)


class _Ranges(MarketDataProvider):
    """Daily ranges for a few symbols. A request spanning a *broken* date
    finds nothing at all, as fafnir refuses a range across a day its ticker
    named no security."""

    name = "ranges"

    def __init__(
        self,
        days: dict[tuple[str, date], tuple[str, str]],
        *,
        broken: frozenset[date] = frozenset(),
        down: bool = False,
    ) -> None:
        self.days, self.broken, self.down = days, broken, down
        self.asked: list[tuple[str, date, date]] = []

    @property
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.DAILY_RANGE})

    def daily_ranges(self, symbol: str, start: date, end: date) -> list[DailyRange]:
        self.asked.append((symbol, start, end))
        if self.down:
            raise DataUnavailableError("the tunnel is down", code=E_PROVIDER_UNAVAILABLE)
        if any(start <= day <= end for day in self.broken):
            raise DataUnavailableError(
                f"{symbol} does not name a security on every date", code=E_PRICE_MISSING
            )
        found = [
            DailyRange(on, D(low), D(high), D(high), "test")
            for (name, on), (low, high) in sorted(self.days.items())
            if name == symbol and start <= on <= end
        ]
        if not found:
            raise DataUnavailableError(f"no prices for {symbol}", code=E_PRICE_MISSING)
        return found


def _instrument(repos: Repositories, symbol: str, kind: InstrumentType) -> Instrument:
    found = repos.instruments.get(
        repos.instruments.add(Instrument(instrument_id=0, symbol=symbol, instrument_type=kind))
    )
    assert found is not None
    return found


def _fill(
    repos: Repositories, account: Account, instrument: Instrument, on: date, price: str
) -> int:
    return append(
        repos, account.account_id, TransactionType.BUY, on,
        instrument_id=instrument.instrument_id, quantity=D("10"), price=D(price),
        gross_amount=D(price) * 10, net_cash_effect=-D(price) * 10,
    )  # fmt: skip


def _verdicts(checked: trade_check.Checked) -> list[tuple[str, date, Verdict]]:
    return [(line.symbol, line.trade_date, line.verdict) for line in checked.lines]


def test_r10_a_stock_fill_is_checked_against_the_days_range(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    for on, price in ((MON, "100.00"), (TUE, "102.50"), (WED, "97.99")):
        _fill(repos, taxable_account, aapl, on, price)
    ranges = _Ranges(
        {
            ("AAPL", MON): ("99.00", "101.00"),
            ("AAPL", TUE): ("99.00", "102.00"),
            ("AAPL", WED): ("98.00", "100.00"),
        }
    )

    checked = trade_check.check(repos, ranges, MON, WED)

    assert _verdicts(checked) == [
        ("AAPL", MON, Verdict.WITHIN),
        ("AAPL", TUE, Verdict.OUTSIDE),
        ("AAPL", WED, Verdict.OUTSIDE),
    ]
    assert [line.note for line in checked.flagged] == [
        "0.50 above the day's high of 102.00",
        "0.01 below the day's low of 98.00",
    ]
    assert ranges.asked == [("AAPL", MON, WED)], "one request for all of a ticker's fills"


def test_r10_the_range_is_inclusive_at_both_ends(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    _fill(repos, taxable_account, aapl, MON, "99.00")
    _fill(repos, taxable_account, aapl, MON, "101.00")

    checked = trade_check.check(repos, _Ranges({("AAPL", MON): ("99.00", "101.00")}), MON, MON)

    assert [line.verdict for line in checked.lines] == [Verdict.WITHIN, Verdict.WITHIN]


def test_r10_a_date_the_provider_cannot_answer_does_not_hide_the_rest(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    for on in (MON, TUE, WED):
        _fill(repos, taxable_account, aapl, on, "100.00")
    ranges = _Ranges(
        {("AAPL", MON): ("99.00", "101.00"), ("AAPL", WED): ("99.00", "101.00")},
        broken=frozenset({TUE}),
    )

    checked = trade_check.check(repos, ranges, MON, WED)

    assert _verdicts(checked) == [
        ("AAPL", MON, Verdict.WITHIN),
        ("AAPL", TUE, Verdict.NO_DATA),
        ("AAPL", WED, Verdict.WITHIN),
    ]
    assert checked.lines[1].note == "AAPL does not name a security on every date"


def test_a_provider_that_cannot_be_reached_stops_the_check(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    _fill(repos, taxable_account, aapl, MON, "100.00")

    with pytest.raises(DataUnavailableError, match="tunnel is down"):
        trade_check.check(repos, _Ranges({}, down=True), MON, MON)


def _nav(repos: Repositories, instrument: Instrument, on: date, value: str) -> None:
    repos.prices.add(
        Price(
            instrument_id=instrument.instrument_id,
            price_date=on,
            price=D(value),
            source="file:navs.csv",
            as_of=datetime.combine(on, datetime.min.time(), tzinfo=UTC),
        )
    )


def test_r10_a_fund_fill_is_checked_against_the_nav_to_the_cent(
    repos: Repositories, taxable_account: Account
) -> None:
    fund = _instrument(repos, "VFIAX", InstrumentType.MUTUAL_FUND)
    _nav(repos, fund, MON, "25.43")
    _nav(repos, fund, TUE, "25.50")
    _fill(repos, taxable_account, fund, MON, "25.4349")  # 25.43 to the cent
    _fill(repos, taxable_account, fund, TUE, "25.49")
    _fill(repos, taxable_account, fund, WED, "25.60")
    ranges = _Ranges({})

    checked = trade_check.check(repos, ranges, MON, WED)

    assert _verdicts(checked) == [
        ("VFIAX", MON, Verdict.AT_NAV),
        ("VFIAX", TUE, Verdict.OFF_NAV),
        ("VFIAX", WED, Verdict.NO_DATA),
    ]
    assert checked.lines[1].note == "0.01 below the NAV of 25.50 (file:navs.csv)"
    assert checked.lines[2].note == "no NAV held for 2024-01-10"
    assert ranges.asked == [], "a fund is not asked a range"


def test_r10_options_bonds_and_unquoted_instruments_are_listed_not_checked(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    bond = _instrument(repos, "T 4.5 2034", InstrumentType.BOND)
    _fill(repos, taxable_account, bond, MON, "99.50")
    repos.instruments.set_market_price(aapl.instrument_id, has_market_price=False)
    _fill(repos, taxable_account, aapl, MON, "1.00")

    checked = trade_check.check(repos, _Ranges({}), MON, MON)

    assert [(line.verdict, line.note) for line in checked.lines] == [
        (Verdict.NOT_CHECKED, "bond fills are not checked"),
        (Verdict.NOT_CHECKED, "marked as having no market price"),
    ]
    assert checked.flagged == ()


def test_a_reversed_fill_did_not_happen_and_is_not_checked(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    wrong = _fill(repos, taxable_account, aapl, MON, "150.00")
    append(repos, taxable_account.account_id, TransactionType.REVERSAL, TUE,
           instrument_id=aapl.instrument_id, quantity=D("10"), price=D("150.00"),
           net_cash_effect=D("1500.00"), reverses_txn_id=wrong)  # fmt: skip
    _fill(repos, taxable_account, aapl, MON, "100.00")

    checked = trade_check.check(repos, _Ranges({("AAPL", MON): ("99.00", "101.00")}), MON, WED)

    assert [(line.price, line.verdict) for line in checked.lines] == [
        (D("100.00"), Verdict.WITHIN)
    ]


def test_the_account_and_dates_narrow_what_is_checked(
    repos: Repositories, taxable_account: Account, ira_account: Account, aapl: Instrument
) -> None:
    _fill(repos, taxable_account, aapl, MON, "100.00")
    _fill(repos, ira_account, aapl, TUE, "100.00")
    _fill(repos, taxable_account, aapl, WED, "100.00")
    ranges = _Ranges({("AAPL", d): ("99.00", "101.00") for d in (MON, TUE, WED)})

    checked = trade_check.check(repos, ranges, MON, TUE, account_id=taxable_account.account_id)

    assert [(line.account, line.trade_date) for line in checked.lines] == [("Brokerage", MON)]


# ── the command, in-process with a range provider ────────────────────────────


@pytest.fixture
def book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    ranges = _Ranges({("AAPL", date(2024, 1, 3)): ("99.00", "101.00")})
    monkeypatch.setattr(context, "build_provider", lambda *_a, **_k: ranges)
    port = str(tmp_path / "t.port")
    for args in (
        ["init", port, "--name", "T", "--inception", "2024-01-02"],
        ["--port", port, "account", "add", "--name", "B", "--type", "tax-deferred",
         "--opened", "2024-01-02", "--relief-method", "fifo"],
        ["--port", port, "cash", "deposit", "-a", "B", "--amount", "10000",
         "--date", "2024-01-02"],
        ["--port", port, "instrument", "add", "AAPL", "--type", "equity"],
        ["--port", port, "buy", "AAPL", "-a", "B", "--qty", "10", "--price", "100.00",
         "--date", "2024-01-03"],
        ["--port", port, "buy", "AAPL", "-a", "B", "--qty", "10", "--price", "105.00",
         "--date", "2024-01-03"],
    ):  # fmt: skip
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 0, result.output
    return port


def test_pt_trade_check_flags_for_review_and_exits_0(book: str) -> None:
    result = CliRunner().invoke(
        app,
        ["--format", "json", "--port", book, "-S", "fafnir", "--as-of", "2024-01-31",
         "trade", "check"],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    data = envelope["data"]
    assert data["from"] == "2024-01-02"
    assert [(r["price"], r["verdict"]) for r in data["rows"]] == [
        ("100.00", "within"),
        ("105.00", "outside"),
    ]
    assert data["flagged"] == 1
    assert data["counts"]["outside"] == 1


def test_pt_trade_check_without_a_range_provider_is_refused(book: str) -> None:
    result = CliRunner().invoke(
        app, ["--format", "json", "--port", book, "--offline", "trade", "check"]
    )

    assert result.exit_code == 5, result.output
