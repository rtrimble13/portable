"""The fafnir adapter, every read through `duk`, answered by a fake `duk`.

ADR 0032: portable has no SQL against fafnir and no database driver; it runs
`duk th`, `duk ph`, `duk ca`, `duk tc` and `duk yc`. The fake here answers those
commands the way duk does -- the same JSON keys, the same exact decimal text,
the same sentences for "nothing" and the same refusals -- from a store laid out
as fafnir's `mart` views are. fafnir pins the real output in
`test/duk/test_db_portable_reads_integration.py`; a change there is a change
here.

ADR 0020 R5 is the reason most of these exist: a ticker can name one security
for part of a range and another for the rest, and a historical load spans
years.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from portable_core.domain.enums import InstrumentType
from portable_core.errors import DataUnavailableError, ValidationError
from portable_core.providers import Capability, as_eod_provider
from portable_core.providers.fafnir import DUK_COMMANDS, FafnirProvider

pytestmark = pytest.mark.unit

D = Decimal

#: What the fake duk reads: fafnir's mart views as migrations 0020 and 0033
#: define them, and ref.trading_calendar.
_DDL = (
    """CREATE TABLE security (
        security_id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, company_name TEXT,
        asset_type TEXT CHECK (asset_type IN ('equity', 'etf', 'fund', 'other')),
        exchange_code TEXT, currency TEXT DEFAULT 'USD', country TEXT, cik TEXT,
        isin TEXT, cusip TEXT, is_actively_trading INTEGER, delisted_date TEXT)""",
    """CREATE TABLE symbol_lookup (
        symbol TEXT, security_id INTEGER, is_primary INTEGER,
        valid_from TEXT DEFAULT '1900-01-01', valid_to TEXT)""",
    """CREATE TABLE daily_price_raw (
        security_id INTEGER, trade_date TEXT, high TEXT, low TEXT, close TEXT,
        PRIMARY KEY (security_id, trade_date))""",
    """CREATE TABLE corporate_action (
        corporate_action_id INTEGER PRIMARY KEY, security_id INTEGER, action_type TEXT,
        ex_date TEXT, record_date TEXT, payment_date TEXT, declaration_date TEXT,
        split_numerator TEXT, split_denominator TEXT, dividend_amount TEXT,
        currency TEXT DEFAULT 'USD', source TEXT DEFAULT 'sharadar')""",
    """CREATE TABLE trading_calendar (
        exchange_code TEXT, trade_date TEXT, is_open INTEGER, session_note TEXT)""",
)


class _Warehouse:
    """The rows the fake duk answers from."""

    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        for ddl in _DDL:
            self.db.execute(ddl)

    def close(self) -> None:
        self.db.close()

    def security(
        self,
        security_id: int,
        symbol: str,
        asset_type: str = "equity",
        *,
        isin: str | None = None,
        cusip: str | None = "",
        cik: str | None = None,
    ) -> None:
        """A security; its CUSIP defaults to one made from its id."""
        self.db.execute(
            "INSERT INTO security (security_id, symbol, company_name, asset_type, "
            "exchange_code, country, cik, cusip, isin, is_actively_trading) "
            "VALUES (?, ?, ?, ?, 'NYSE', 'US', ?, ?, ?, 1)",
            (
                security_id,
                symbol,
                f"{symbol} Inc.",
                asset_type,
                cik,
                f"C{security_id:08d}" if cusip == "" else cusip,
                isin,
            ),
        )

    def ticker(
        self,
        symbol: str,
        security_id: int,
        valid_from: str = "1900-01-01",
        valid_to: str | None = None,
        *,
        primary: bool = True,
    ) -> None:
        self.db.execute(
            "INSERT INTO symbol_lookup VALUES (?, ?, ?, ?, ?)",
            (symbol, security_id, int(primary), valid_from, valid_to),
        )

    def bar(
        self, security_id: int, on: str, close: str, *, low: str = "", high: str = ""
    ) -> None:
        self.db.execute(
            "INSERT INTO daily_price_raw VALUES (?, ?, ?, ?, ?)",
            (security_id, on, high or close, low or close, close),
        )

    def sessions(self, exchange: str, *days: str, is_open: bool = True) -> None:
        self.db.executemany(
            "INSERT INTO trading_calendar (exchange_code, trade_date, is_open) "
            "VALUES (?, ?, ?)",
            [(exchange, on, int(is_open)) for on in days],
        )

    def dividend(self, security_id: int, ex_date: str, amount: str) -> None:
        self.db.execute(
            "INSERT INTO corporate_action (security_id, action_type, ex_date, "
            "payment_date, dividend_amount) VALUES (?, 'dividend', ?, ?, ?)",
            (security_id, ex_date, ex_date, amount),
        )


def _numeric(stored: str | None) -> str | None:
    """A NUMERIC(20, 6) as ``--exact`` prints it: ``format(value, "f")``."""
    return None if stored is None else format(D(stored).quantize(D("0.000001")), "f")


def _option(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


@dataclass
class _Duk:
    """`duk` over the fake warehouse, recording how it was called."""

    warehouse: _Warehouse
    calls: list[list[str]] = field(default_factory=list)
    envs: list[dict[str, str]] = field(default_factory=list)
    #: Replaces the answer to a subcommand: {"ph": (returncode, stdout, stderr)}.
    answers: dict[str, tuple[int, str, str]] = field(default_factory=dict)
    #: The warehouse has no mart.v_corporate_action (before fafnir 0033).
    no_action_view: bool = False

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        self.envs.append(dict(kwargs.get("env") or {}))
        assert argv[1:3] in (["-S", "db"], ["-S", "live"]), argv
        command = argv[3]
        if command in self.answers:
            code, out, err = self.answers[command]
            return subprocess.CompletedProcess(argv, code, stdout=out, stderr=err)
        code, out, err = getattr(self, f"_{command}")(argv)
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr=err)

    #: Answer "nothing found" as a duk older than fafnir's empty-JSON change did:
    #: the sentence on stdout instead of ``[]``.
    sentences: bool = False

    def _nothing(self, sentence: str) -> tuple[int, str, str]:
        """duk's "nothing found": ``[]`` with the sentence on stderr, or the old way."""
        if self.sentences:
            return 0, sentence + "\n", ""
        return 0, "[]\n", sentence + "\n"

    def ph_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[3] == "ph"]

    def _resolve(self, named: str) -> int | None:
        scheme, _, value = named.partition(":")
        assert scheme == "sid", f"portable names a security by sid:, not {named}"
        found = self.warehouse.db.execute(
            "SELECT security_id FROM security WHERE security_id = ?", (int(value),)
        ).fetchall()
        return found[0][0] if found else None

    def _th(self, argv: list[str]) -> tuple[int, str, str]:
        """`duk th ticker:A [ticker:B ...]`: each ticker's periods, in the order asked."""
        tickers = []
        for named in argv[4:]:
            if named.startswith("--"):
                break
            scheme, _, ticker = named.partition(":")
            assert scheme == "ticker", "each ticker is passed explicitly as one"
            if ticker.upper() not in tickers:
                tickers.append(ticker.upper())
        rows = []
        for ticker in tickers:
            rows += self.warehouse.db.execute(
                "SELECT x.symbol, x.security_id, x.valid_from, x.valid_to, x.is_primary, "
                "p.symbol, p.company_name, p.asset_type, p.exchange_code, p.currency, "
                "p.country, p.cik, p.isin, p.cusip, p.is_actively_trading, "
                "p.delisted_date FROM symbol_lookup x "
                "JOIN security p ON p.security_id = x.security_id "
                "WHERE upper(x.symbol) = ? ORDER BY x.valid_from, x.security_id",
                (ticker,),
            ).fetchall()
        if not rows:
            return self._nothing(f"No ticker history for {', '.join(tickers)}")
        keys = (
            "symbol", "security_id", "valid_from", "valid_to", "is_primary",
            "primary_symbol", "company_name", "asset_type", "exchange_code", "currency",
            "country", "cik", "isin", "cusip", "is_actively_trading", "delisted_date",
        )  # fmt: skip
        records = []
        for row in rows:
            record = dict(zip(keys, row, strict=True))
            record["is_primary"] = bool(record["is_primary"])
            record["is_actively_trading"] = bool(record["is_actively_trading"])
            records.append(record)
        return 0, json.dumps(records) + "\n", ""

    def _ph(self, argv: list[str]) -> tuple[int, str, str]:
        named = argv[4]
        security_id = self._resolve(named)
        if security_id is None:
            return 1, "", f"No security found for security_id {named[4:]}."
        bars = self.warehouse.db.execute(
            "SELECT trade_date, close, high, low FROM daily_price_raw "
            "WHERE security_id = ? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
            (security_id, _option(argv, "--start-date"), _option(argv, "--end-date")),
        ).fetchall()
        if not bars:
            return self._nothing(f"No data found for {named}")
        fields = ("high", "low", "close") if "--hlc" in argv else ("close",)
        assert "--exact" in argv and "--json" in argv and "--adj" not in argv
        records = [
            {
                "date": on,
                **{
                    f: _numeric(v)
                    for f, v in (("high", high), ("low", low), ("close", close))
                    if f in fields
                },
            }
            for on, close, high, low in bars
        ]
        return 0, json.dumps(records) + "\n", ""

    def _ca(self, argv: list[str]) -> tuple[int, str, str]:
        if self.no_action_view:
            return (
                1,
                "",
                "Error: this warehouse has no mart.v_corporate_action: it arrives with "
                "fafnir migration 0033 (`fafnir db migrate` on the host)",
            )
        security_id = self._resolve(argv[4])
        if security_id is None:
            return 1, "", f"No security found for security_id {argv[4][4:]}."
        assert "--exact" in argv and "--json" in argv
        rows = self.warehouse.db.execute(
            "SELECT ex_date, action_type, split_numerator, split_denominator, "
            "dividend_amount, currency, record_date, payment_date, declaration_date, "
            "corporate_action_id, security_id, source FROM corporate_action "
            "WHERE security_id = ? AND ex_date BETWEEN ? AND ? "
            "ORDER BY ex_date, action_type, corporate_action_id",
            (security_id, _option(argv, "--start-date"), _option(argv, "--end-date")),
        ).fetchall()
        if not rows:
            return self._nothing(f"No corporate actions for {argv[4]}")
        keys = (
            "ex_date", "action_type", "split_numerator", "split_denominator",
            "dividend_amount", "currency", "record_date", "payment_date",
            "declaration_date", "corporate_action_id", "security_id", "source",
        )  # fmt: skip
        numeric = {"split_numerator", "split_denominator", "dividend_amount"}
        records = []
        for row in rows:
            record = dict(zip(keys, row, strict=True))
            for key in numeric:
                record[key] = _numeric(record[key])
            record["corporate_action_id"] = str(record["corporate_action_id"])
            record["security_id"] = str(record["security_id"])
            records.append(record)
        return 0, json.dumps(records) + "\n", ""

    def _tc(self, argv: list[str]) -> tuple[int, str, str]:
        venue = argv[4] if len(argv) > 4 and not argv[4].startswith("-") else None
        if venue is None:
            rows = self.warehouse.db.execute(
                "SELECT exchange_code, min(trade_date), max(trade_date), "
                "sum(is_open), sum(1 - is_open) FROM trading_calendar "
                "GROUP BY exchange_code ORDER BY exchange_code"
            ).fetchall()
            if not rows:
                return self._nothing("No trading calendars in the warehouse")
            records = [
                {"exchange_code": code, "exchange_name": None, "first_date": first,
                 "last_date": last, "open_days": opened, "closed_days": closed}
                for code, first, last, opened, closed in rows
            ]  # fmt: skip
            return 0, json.dumps(records) + "\n", ""
        rows = self.warehouse.db.execute(
            "SELECT trade_date, is_open, session_note FROM trading_calendar "
            "WHERE upper(exchange_code) = upper(?) AND trade_date BETWEEN ? AND ? "
            "ORDER BY trade_date",
            (venue, _option(argv, "--start-date"), _option(argv, "--end-date")),
        ).fetchall()
        if not rows:
            return self._nothing(f"No trading calendar for {venue.upper()} in that range")
        records = [{"date": d, "is_open": bool(o), "session_note": n} for d, o, n in rows]
        return 0, json.dumps(records) + "\n", ""


@pytest.fixture
def warehouse() -> Iterator[_Warehouse]:
    built = _Warehouse()
    yield built
    built.close()


@pytest.fixture
def duk(warehouse: _Warehouse, monkeypatch: pytest.MonkeyPatch) -> _Duk:
    fake = _Duk(warehouse)
    monkeypatch.setattr("portable_core.providers.fafnir.shutil.which", lambda _: "/bin/duk")
    monkeypatch.setattr("portable_core.providers.fafnir.subprocess.run", fake)
    return fake


@pytest.fixture
def fafnir(duk: _Duk) -> FafnirProvider:
    del duk  # installed in place of subprocess.run; the tests that inspect it ask for it
    return FafnirProvider(dsn="host=fake")


# ── prices across a ticker's history ────────────────────────────────────────


def test_a_reused_ticker_is_priced_from_the_security_that_held_it_each_day(
    warehouse: _Warehouse, fafnir: FafnirProvider, duk: _Duk
) -> None:
    """ADR 0020 R5. The bug this replaces resolved the ticker once, as of the end.

    XYZ named security 1 through 2023, and security 2 -- which traded before
    that as ABC -- from 2024. Resolved as of the range's end, every 2023 date
    was priced from security 2's ABC bars: another issuer's prices, under the
    right ticker, with nothing to say so. `duk` resolving the ticker itself
    would do the same, so each stretch is named to it by security id.
    """
    warehouse.security(1, "XYZ")
    warehouse.security(2, "XYZ")
    warehouse.ticker("XYZ", 1, valid_to="2023-12-31")
    warehouse.ticker("ABC", 2, valid_to="2023-12-31")
    warehouse.ticker("XYZ", 2, valid_from="2024-01-01")
    warehouse.bar(1, "2023-12-29", "10.00")
    warehouse.bar(2, "2023-12-29", "250.00")  # ABC's close: not XYZ's
    warehouse.bar(2, "2024-01-02", "251.00")

    prices = fafnir.eod_prices("XYZ", date(2023, 12, 28), date(2024, 1, 3))

    assert [(p.price_date, p.price) for p in prices] == [
        (date(2023, 12, 29), D("10.000000")),
        (date(2024, 1, 2), D("251.000000")),
    ]
    assert [
        (c[4], _option(c, "--start-date"), _option(c, "--end-date")) for c in duk.ph_calls()
    ] == [
        ("sid:1", "2023-12-28", "2023-12-31"),
        ("sid:2", "2024-01-01", "2024-01-03"),
    ]
    assert prices[0].provider_ref == "security_id=1;trade_date=2023-12-29;via=sid:1"
    assert prices[1].provider_ref == "security_id=2;trade_date=2024-01-02;via=sid:2"


def test_days_the_ticker_named_nothing_are_refused_by_name(
    warehouse: _Warehouse, fafnir: FafnirProvider
) -> None:
    """A partial answer would price part of a history and leave the rest quiet.

    A rename closes the old period the day before the new one opens, so the
    new ticker names nothing before its first day -- and asking for it there is
    the wrong question, not a gap to skip over.
    """
    warehouse.security(7, "META")
    warehouse.ticker("FB", 7, valid_to="2022-06-08")
    warehouse.ticker("META", 7, valid_from="2022-06-09")
    warehouse.bar(7, "2022-06-08", "196.64")
    warehouse.bar(7, "2022-06-09", "191.24")

    with pytest.raises(DataUnavailableError) as refused:
        fafnir.eod_prices("META", date(2022, 6, 1), date(2022, 6, 10))
    assert refused.value.code == "PT-E-PRICE-MISSING"
    assert refused.value.context["unresolved"] == ["2022-06-01..2022-06-08"]

    under_the_old_ticker = fafnir.eod_prices("FB", date(2022, 6, 1), date(2022, 6, 8))
    assert [p.price for p in under_the_old_ticker] == [D("196.64")]


def test_a_ticker_with_no_history_at_all_is_refused_by_name(
    warehouse: _Warehouse, fafnir: FafnirProvider
) -> None:
    """duk's "No ticker history" is an empty answer: every day is uncovered."""
    with pytest.raises(DataUnavailableError, match="symbol history") as refused:
        fafnir.eod_prices("NOPE", date(2025, 6, 30), date(2025, 6, 30))
    assert refused.value.code == "PT-E-PRICE-MISSING"


def test_no_fallback_to_the_primary_symbol(
    warehouse: _Warehouse, fafnir: FafnirProvider
) -> None:
    """fafnir's own data dictionary: `primary_symbol` is "not an identifier"."""
    warehouse.security(3, "QQQ")  # in the master, absent from the ticker history
    warehouse.bar(3, "2025-06-30", "500.00")

    with pytest.raises(DataUnavailableError, match="symbol history"):
        fafnir.eod_prices("QQQ", date(2025, 6, 30), date(2025, 6, 30))


def test_overlapping_periods_prefer_the_securitys_primary_ticker(
    warehouse: _Warehouse, fafnir: FafnirProvider
) -> None:
    """The rule a single date always used, now applied to every day of a range."""
    warehouse.security(4, "BRK.B")
    warehouse.security(5, "BRKB")
    warehouse.ticker("BRKB", 4, primary=False)
    warehouse.ticker("BRKB", 5, valid_from="2025-01-01")
    warehouse.bar(4, "2025-06-30", "480.00")
    warehouse.bar(5, "2025-06-30", "1.00")

    prices = fafnir.eod_prices("BRKB", date(2025, 6, 30), date(2025, 6, 30))

    assert [p.price for p in prices] == [D("1.00")]


def test_a_price_is_the_stored_decimal_and_carries_its_lineage(
    warehouse: _Warehouse, fafnir: FafnirProvider
) -> None:
    """Invariant 1 and PORT-GIPS-J03: the stored value, and the security behind it.

    Through ``duk ph --exact`` a price is the warehouse's NUMERIC as text, so it
    arrives digit for digit, at any magnitude: there is no float to undo.
    """
    warehouse.security(1, "ABC", isin="US0000000001")
    warehouse.ticker("ABC", 1)
    warehouse.bar(1, "2025-06-30", "13247980.736555")
    warehouse.bar(1, "2025-07-01", "189.120000")
    warehouse.bar(1, "2025-07-02", "98765432109876.543210")  # past any float's exact range

    first, second, third = fafnir.eod_prices("ABC", date(2025, 6, 30), date(2025, 7, 2))

    assert first.price == D("13247980.736555")
    assert str(second.price) == "189.120000", "six places, as the warehouse holds it"
    assert str(third.price) == "98765432109876.543210"
    assert first.provider_ref == "security_id=1;trade_date=2025-06-30;via=sid:1"
    assert first.source == "fafnir:duk ph"
    assert first.valuation_level == 1
    assert first.as_of == datetime(2025, 6, 30, tzinfo=UTC), "the price's date, not the clock"


def test_duk_is_asked_for_raw_closes_from_the_warehouse_this_adapter_reads(
    warehouse: _Warehouse, fafnir: FafnirProvider, duk: _Duk
) -> None:
    """PORT-GIPS-A01, ADR 0032: the ticker's history, then `-S db`, never `--adj`.

    The override portable was given reaches duk as FAFNIR_DSN.
    """
    warehouse.security(1, "ABC")
    warehouse.ticker("ABC", 1)
    warehouse.bar(1, "2025-06-30", "1.00")

    fafnir.eod_prices("ABC", date(2025, 6, 30), date(2025, 6, 30))

    assert duk.calls == [
        ["duk", "-S", "db", "th", "ticker:ABC", "--json"],
        [
            "duk", "-S", "db", "ph", "sid:1",
            "--start-date", "2025-06-30", "--end-date", "2025-06-30",
            "--close", "--json", "--exact",
        ],
    ]  # fmt: skip
    assert {env["FAFNIR_DSN"] for env in duk.envs} == {"host=fake"}


def test_without_an_override_duk_reads_its_own_configuration(
    warehouse: _Warehouse, duk: _Duk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0032: portable reads no fafnir config; duk's own FAFNIR_DSN stands."""
    monkeypatch.delenv("PORTABLE_FAFNIR_DSN", raising=False)
    monkeypatch.setenv("FAFNIR_DSN", "host=duks-own")
    warehouse.security(1, "ABC")
    warehouse.ticker("ABC", 1)
    warehouse.bar(1, "2025-06-30", "1.00")

    FafnirProvider().eod_prices("ABC", date(2025, 6, 30), date(2025, 6, 30))

    assert {env["FAFNIR_DSN"] for env in duk.envs} == {"host=duks-own"}


@pytest.mark.parametrize(
    "identifiers",
    [
        {"isin": "US0378331005", "cusip": "037833100", "cik": "320193"},
        {"cusip": None, "cik": None},  # none at all: ADR 0029 refused this one
    ],
)
def test_the_security_is_named_by_its_id_whatever_identifiers_it_has(
    warehouse: _Warehouse,
    fafnir: FafnirProvider,
    duk: _Duk,
    identifiers: dict[str, Any],
) -> None:
    """A dead issuer minted with no ISIN, CUSIP or CIK is priced all the same."""
    warehouse.security(1, "AAPL", **identifiers)
    warehouse.ticker("AAPL", 1)
    warehouse.bar(1, "2025-06-30", "201.08")

    prices = fafnir.eod_prices("AAPL", date(2025, 6, 30), date(2025, 6, 30))

    assert [p.price for p in prices] == [D("201.08")]
    assert duk.ph_calls()[0][4] == "sid:1"


def test_two_classes_sharing_a_cik_are_each_priced_as_themselves(
    warehouse: _Warehouse, fafnir: FafnirProvider
) -> None:
    """By CIK, duk refused these as ambiguous (ADR 0029 §3); by id there is no choice."""
    warehouse.security(1, "GOOGL", cusip=None, cik="1652044")
    warehouse.security(2, "GOOG", cusip=None, cik="1652044")
    warehouse.ticker("GOOGL", 1)
    warehouse.ticker("GOOG", 2)
    warehouse.bar(1, "2025-06-30", "176.23")
    warehouse.bar(2, "2025-06-30", "177.39")

    assert [
        p.price for p in fafnir.eod_prices("GOOG", date(2025, 6, 30), date(2025, 6, 30))
    ] == [D("177.39")]


@pytest.mark.parametrize(
    ("command", "returncode", "stderr"),
    [
        ("ph", 1, "Error: Failed to fetch price history: permission denied for schema core"),
        ("ph", 2, "Error: No such option: --exact"),
        ("th", 2, "Usage: duk [OPTIONS] COMMAND [ARGS]... Error: No such command 'th'."),
        ("th", 1, "Error: Failed to read ticker history: connection is bad"),
    ],
)
def test_duk_failing_outright_stops_the_run(
    warehouse: _Warehouse,
    fafnir: FafnirProvider,
    duk: _Duk,
    command: str,
    returncode: int,
    stderr: str,
) -> None:
    """Not a gap in one security's history: no security would go better."""
    warehouse.security(1, "ABC")
    warehouse.ticker("ABC", 1)
    duk.answers[command] = (returncode, "", stderr)

    with pytest.raises(DataUnavailableError) as refused:
        fafnir.eod_prices("ABC", date(2025, 6, 30), date(2025, 6, 30))
    assert refused.value.code == "PT-E-PROVIDER-UNAVAILABLE"
    assert stderr in refused.value.message
    assert "sid:" in (refused.value.remedy or "")


def test_a_security_duk_cannot_find_is_that_securitys_gap(
    warehouse: _Warehouse, fafnir: FafnirProvider, duk: _Duk
) -> None:
    """About one security, so the run carries on for the others."""
    warehouse.security(1, "ABC")
    warehouse.ticker("ABC", 1)
    duk.answers["ph"] = (1, "", "No security found for security_id 1.")

    with pytest.raises(DataUnavailableError, match="No security found") as refused:
        fafnir.eod_prices("ABC", date(2025, 6, 30), date(2025, 6, 30))
    assert refused.value.code == "PT-E-PRICE-MISSING"


@pytest.mark.parametrize(
    ("stdout", "why"),
    [
        # A JSON number came through a float somewhere: the very thing --exact rules out.
        ('[{"date":"2025-06-30","close":189.12}]', "not exact decimal text"),
        ('[{"date":"2025-06-30","close":"1.89120E+2"}]', "not exact decimal text"),
        ('[{"date":"2025-06-30","close":"-1.000000"}]', "not exact decimal text"),
        ('[{"date":"2025-06-30","close":null}]', "not exact decimal text"),
        ('[{"date":"2025-06-30","close":"0.000000"}]', "has close 0.000000"),
        ('[{"date":"2025-06-30T00:00:00.000","close":"1.000000"}]', "unexpected date"),
        ('[{"date":"2025-06-30","close":"1.000000","volume":"100"}]', "unexpected record"),
        ('[{"date":"2025-07-01","close":"1.000000"}]', "outside the dates"),
        ('{"close": "1.000000"}', "not a list"),
        ("[oops", "not JSON"),
    ],
)
def test_output_that_is_not_exact_decimal_text_is_refused(
    warehouse: _Warehouse, fafnir: FafnirProvider, duk: _Duk, stdout: str, why: str
) -> None:
    """Refused, never rounded into shape: every price after it would be suspect."""
    warehouse.security(1, "ABC")
    warehouse.ticker("ABC", 1)
    duk.answers["ph"] = (0, stdout, "")

    with pytest.raises(DataUnavailableError, match="cannot be read exactly") as refused:
        fafnir.eod_prices("ABC", date(2025, 6, 30), date(2025, 6, 30))
    assert why in refused.value.message
    assert refused.value.code == "PT-E-PROVIDER-UNAVAILABLE"


@pytest.mark.parametrize(
    ("changed", "why"),
    [
        ({"is_primary": None}, "no is_primary"),  # None: the key is dropped
        ({"security_id": "1"}, "is not a security's id"),
        ({"security_id": True}, "is not a security's id"),
        ({"valid_from": "1900-01-01T00:00:00"}, "unexpected date"),
        ({"is_primary": "yes"}, "is not true or false"),
    ],
)
def test_a_ticker_history_that_cannot_be_read_is_refused(
    fafnir: FafnirProvider, duk: _Duk, changed: dict[str, Any], why: str
) -> None:
    """Which security a day belongs to is the whole question; a guess is not an answer."""
    record = {
        "symbol": "ABC", "security_id": 1, "valid_from": "1900-01-01", "valid_to": None,
        "is_primary": True, "primary_symbol": "ABC", "company_name": "ABC Inc.",
        "asset_type": "equity", "exchange_code": "NYSE", "currency": "USD", "country": "US",
        "cik": None, "isin": None, "cusip": None, "is_actively_trading": True,
        "delisted_date": None,
    }  # fmt: skip
    for key, value in changed.items():
        if value is None:
            del record[key]
        else:
            record[key] = value
    duk.answers["th"] = (0, json.dumps([record]), "")

    with pytest.raises(DataUnavailableError, match="cannot be read") as refused:
        fafnir.eod_prices("ABC", date(2025, 6, 30), date(2025, 6, 30))
    assert why in refused.value.message
    assert refused.value.code == "PT-E-PROVIDER-UNAVAILABLE"


def test_a_resolvable_range_with_no_bars_is_price_missing(
    warehouse: _Warehouse, fafnir: FafnirProvider
) -> None:
    """duk says "No data found" in a sentence; that is an empty answer, not a broken one."""
    warehouse.security(1, "ABC")
    warehouse.ticker("ABC", 1)

    with pytest.raises(DataUnavailableError, match="no fafnir prices for ABC") as refused:
        fafnir.eod_prices("ABC", date(2025, 6, 30), date(2025, 7, 1))
    assert refused.value.code == "PT-E-PRICE-MISSING"


@pytest.mark.parametrize("sentences", [False, True], ids=["empty-json", "older-duk-sentence"])
def test_nothing_found_reads_as_nothing_whichever_way_duk_says_it(
    warehouse: _Warehouse, fafnir: FafnirProvider, duk: _Duk, sentences: bool
) -> None:
    """duk prints `[]` for an empty answer; an older one printed a sentence. Both mean none.

    Neither is a malformed answer, so neither stops the run: the ticker with no
    history is refused as uncovered, a range with no bars as no prices, and a
    range with no actions or sessions is simply empty.
    """
    duk.sentences = sentences
    warehouse.security(1, "ABC")
    warehouse.ticker("ABC", 1)
    warehouse.sessions("NYSE", "2025-01-02", "2025-12-31")

    with pytest.raises(DataUnavailableError, match="symbol history") as no_history:
        fafnir.eod_prices("NOPE", date(2025, 6, 30), date(2025, 6, 30))
    assert no_history.value.code == "PT-E-PRICE-MISSING"
    with pytest.raises(DataUnavailableError, match="no fafnir prices for ABC") as no_bars:
        fafnir.eod_prices("ABC", date(2025, 6, 30), date(2025, 6, 30))
    assert no_bars.value.code == "PT-E-PRICE-MISSING"
    assert fafnir.corporate_actions("ABC", date(2025, 1, 1), date(2025, 6, 30)) == []
    calendar = fafnir.trading_calendar("NYSE", date(2025, 7, 4), date(2025, 7, 4))
    assert calendar.open_days == frozenset()


def test_a_backwards_range_is_refused(fafnir: FafnirProvider) -> None:
    with pytest.raises(ValidationError, match="ends"):
        fafnir.eod_prices("ABC", date(2025, 7, 1), date(2025, 6, 30))


def test_the_ticker_history_is_read_once_per_provider(
    warehouse: _Warehouse, fafnir: FafnirProvider, duk: _Duk
) -> None:
    """Prices, actions and the master ask about the same ticker in turn."""
    warehouse.security(1, "ABC")
    warehouse.ticker("ABC", 1)
    warehouse.bar(1, "2025-06-30", "1.00")

    fafnir.eod_prices("ABC", date(2025, 6, 30), date(2025, 6, 30))
    fafnir.corporate_actions("abc", date(2025, 1, 1), date(2025, 6, 30))
    fafnir.lookup_security("ABC", on=date(2025, 6, 30))

    assert [c[3] for c in duk.calls].count("th") == 1


def test_a_run_reads_every_tickers_history_in_one_duk_call(
    warehouse: _Warehouse, fafnir: FafnirProvider, duk: _Duk
) -> None:
    """ADR 0032 §2: one process for a portfolio, not one per ticker -- same answers."""
    for security_id, symbol in ((1, "AAA"), (2, "BBB"), (3, "CCC")):
        warehouse.security(security_id, symbol)
        warehouse.ticker(symbol, security_id)
        warehouse.bar(security_id, "2025-06-30", f"{security_id}.00")

    fafnir.prefetch(["aaa", "BBB", "CCC", "AAA", "NOPE"])
    prices = {
        s: fafnir.eod_prices(s, date(2025, 6, 30), date(2025, 6, 30)) for s in ("AAA", "BBB")
    }
    with pytest.raises(DataUnavailableError, match="symbol history"):
        fafnir.eod_prices("NOPE", date(2025, 6, 30), date(2025, 6, 30))

    th = [c for c in duk.calls if c[3] == "th"]
    assert th == [
        [
            "duk",
            "-S",
            "db",
            "th",
            "ticker:AAA",
            "ticker:BBB",
            "ticker:CCC",
            "ticker:NOPE",
            "--json",
        ]
    ], "a ticker with no history is cached as having none, not asked again"
    assert [p.price for p in prices["BBB"]] == [D("2.00")]

    fafnir.prefetch(["AAA", "BBB"])
    assert [c[3] for c in duk.calls].count("th") == 1, "nothing left to read"


def test_a_large_run_is_read_in_batches_a_command_line_can_hold(
    fafnir: FafnirProvider, duk: _Duk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows caps a command line at 32,767 characters."""
    monkeypatch.setattr("portable_core.providers.fafnir._TH_BATCH", 2)

    fafnir.prefetch(["A1", "A2", "A3", "A4", "A5"])

    assert [len(c) - 5 for c in duk.calls if c[3] == "th"] == [2, 2, 1]


def test_a_period_for_a_ticker_not_asked_for_is_refused(
    fafnir: FafnirProvider, duk: _Duk
) -> None:
    record = {
        "symbol": "ZZZ", "security_id": 9, "valid_from": "1900-01-01", "valid_to": None,
        "is_primary": True, "primary_symbol": "ZZZ", "company_name": None,
        "asset_type": "equity", "exchange_code": None, "currency": "USD", "country": None,
        "cik": None, "isin": None, "cusip": None, "is_actively_trading": True,
        "delisted_date": None,
    }  # fmt: skip
    duk.answers["th"] = (0, json.dumps([record]), "")

    with pytest.raises(DataUnavailableError, match="not asked for"):
        fafnir.prefetch(["AAA", "BBB"])


def test_prefetch_without_duk_does_nothing_and_the_question_still_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("portable_core.providers.fafnir.shutil.which", lambda _: None)
    provider = FafnirProvider(dsn="host=fake")

    provider.prefetch(["AAA"])

    with pytest.raises(DataUnavailableError, match="`duk` is not on PATH"):
        provider.eod_prices("AAA", date(2025, 6, 30), date(2025, 6, 30))


def test_without_duk_there_is_nothing_and_the_refusal_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not "fafnir cannot supply eod_prices", which would send the owner to the warehouse."""
    monkeypatch.setattr("portable_core.providers.fafnir.shutil.which", lambda _: None)
    provider = FafnirProvider(dsn="host=fake", duk_path="duk")

    with pytest.raises(DataUnavailableError, match="`duk` is not on PATH") as refused:
        as_eod_provider(provider)
    assert refused.value.code == "PT-E-PROVIDER-UNAVAILABLE"
    assert provider.capabilities == frozenset()
    for asked in (
        lambda: provider.lookup_security("ABC", on=date(2025, 6, 30)),
        lambda: provider.corporate_actions("ABC", date(2025, 1, 1), date(2025, 6, 30)),
        lambda: provider.trading_calendar("NYSE", date(2025, 1, 1), date(2025, 6, 30)),
    ):
        with pytest.raises(DataUnavailableError, match="`duk` is not on PATH"):
            asked()


# ── corporate actions and the security master ───────────────────────────────


def test_corporate_actions_follow_the_ticker_through_its_history(
    warehouse: _Warehouse, fafnir: FafnirProvider, duk: _Duk
) -> None:
    warehouse.security(1, "XYZ")
    warehouse.security(2, "XYZ")
    warehouse.ticker("XYZ", 1, valid_to="2023-12-31")
    warehouse.ticker("XYZ", 2, valid_from="2024-01-01")
    warehouse.dividend(1, "2023-11-15", "0.25")
    warehouse.dividend(2, "2023-11-15", "9.99")  # security 2's, before it was XYZ
    warehouse.dividend(2, "2024-02-15", "0.40")
    warehouse.db.execute(
        "INSERT INTO corporate_action (security_id, action_type, ex_date, "
        "split_numerator, split_denominator) VALUES (2, 'split', '2024-03-01', '4', '1')"
    )

    actions = fafnir.corporate_actions("XYZ", date(2023, 1, 1), date(2024, 12, 31))

    assert [(a.action_type, a.ex_date, a.cash_amount) for a in actions] == [
        ("cash_dividend", date(2023, 11, 15), D("0.25")),
        ("cash_dividend", date(2024, 2, 15), D("0.40")),
        ("split", date(2024, 3, 1), None),
    ]
    assert (actions[2].split_numerator, actions[2].split_denominator) == (D("4"), D("1"))
    assert str(actions[0].cash_amount) == "0.250000", "the stored decimal, not a float"
    assert actions[0].pay_date == date(2023, 11, 15)
    assert actions[0].provider_ref == "corporate_action_id=1"
    assert [c[4] for c in duk.calls if c[3] == "ca"] == ["sid:1", "sid:2"]


@pytest.mark.parametrize(
    ("field_", "value", "why"),
    [
        ("dividend_amount", 0.25, "not exact decimal text"),
        ("dividend_amount", "2.5E-1", "not exact decimal text"),
        ("ex_date", "2023-11-15T00:00:00", "unexpected date"),
        ("ex_date", "2024-11-15", "outside the dates"),
        ("corporate_action_id", "1.0", "not exact decimal text"),
    ],
)
def test_corporate_actions_that_are_not_exact_are_refused(
    warehouse: _Warehouse,
    fafnir: FafnirProvider,
    duk: _Duk,
    field_: str,
    value: Any,
    why: str,
) -> None:
    """A dividend through a float is a basis adjustment off by noise: refused."""
    warehouse.security(1, "XYZ")
    warehouse.ticker("XYZ", 1)
    record = {
        "ex_date": "2023-11-15", "action_type": "dividend", "split_numerator": None,
        "split_denominator": None, "dividend_amount": "0.250000", "currency": "USD",
        "record_date": None, "payment_date": None, "declaration_date": None,
        "corporate_action_id": "1", "security_id": "1", "source": "sharadar",
    }  # fmt: skip
    duk.answers["ca"] = (0, json.dumps([{**record, field_: value}]), "")

    with pytest.raises(DataUnavailableError, match="cannot be read") as refused:
        fafnir.corporate_actions("XYZ", date(2023, 1, 1), date(2023, 12, 31))
    assert why in refused.value.message


def test_a_warehouse_without_the_corporate_action_view_says_which_migration_adds_it(
    warehouse: _Warehouse, fafnir: FafnirProvider, duk: _Duk
) -> None:
    """A warehouse not yet migrated to fafnir 0033 has none; duk says so, and the run stops."""
    duk.no_action_view = True
    warehouse.security(1, "XYZ")
    warehouse.ticker("XYZ", 1)

    with pytest.raises(DataUnavailableError, match=r"mart\.v_corporate_action") as refused:
        fafnir.corporate_actions("XYZ", date(2024, 1, 1), date(2024, 12, 31))
    assert refused.value.code == "PT-E-PROVIDER-UNAVAILABLE"
    assert "fafnir migration 0033" in refused.value.message


def test_lookup_maps_the_master_row_as_of_the_date(
    warehouse: _Warehouse, fafnir: FafnirProvider
) -> None:
    """A fund is a fund: fafnir carries NAV-priced funds in the same tables."""
    warehouse.security(9, "FSMVX", asset_type="fund")
    warehouse.ticker("FSMVX", 9, valid_to="2023-08-31")

    found = fafnir.lookup_security("FSMVX", on=date(2023, 5, 1))

    assert found.instrument_type is InstrumentType.MUTUAL_FUND
    assert found.cusip == "C00000009"
    assert (found.exchange, found.country, found.currency) == ("NYSE", "US", "USD")
    assert found.name == "FSMVX Inc."
    assert found.is_active is True
    assert found.provider_ref == "security_id=9"
    assert found.source == "fafnir:duk th"
    with pytest.raises(DataUnavailableError, match="symbol history"):
        fafnir.lookup_security("FSMVX", on=date(2024, 5, 1))


def test_lookup_answers_with_the_security_the_ticker_named_that_day(
    warehouse: _Warehouse, fafnir: FafnirProvider
) -> None:
    """The master record of whoever held the ticker then, not whoever holds it now."""
    warehouse.security(1, "XYZ", asset_type="etf")
    warehouse.security(2, "NEWCO")
    warehouse.ticker("XYZ", 1, valid_to="2023-12-31")
    warehouse.ticker("XYZ", 2, valid_from="2024-01-01")

    assert fafnir.lookup_security("XYZ", on=date(2023, 6, 30)).instrument_type is (
        InstrumentType.ETF
    )
    then_now = fafnir.lookup_security("XYZ", on=date(2024, 6, 30))
    assert (then_now.symbol, then_now.provider_ref) == ("NEWCO", "security_id=2")


def test_an_asset_type_portable_has_no_name_for_is_refused(
    warehouse: _Warehouse, fafnir: FafnirProvider
) -> None:
    """fafnir's 'other' used to become an equity -- a default with a valuation attached."""
    warehouse.security(6, "WEIRD", asset_type="other")
    warehouse.ticker("WEIRD", 6)

    with pytest.raises(DataUnavailableError, match="'other'"):
        fafnir.lookup_security("WEIRD", on=date(2025, 6, 30))


def test_every_read_is_a_known_duk_command_against_the_warehouse(
    warehouse: _Warehouse, fafnir: FafnirProvider, duk: _Duk
) -> None:
    """ADR 0032: nothing reaches fafnir but `duk`, and `duk` reads the warehouse."""
    warehouse.security(1, "ABC")
    warehouse.ticker("ABC", 1)
    warehouse.bar(1, "2025-06-30", "1.00")
    warehouse.sessions("NYSE", "2025-06-30")
    fafnir.eod_prices("ABC", date(2025, 6, 30), date(2025, 6, 30))
    fafnir.daily_ranges("ABC", date(2025, 6, 30), date(2025, 6, 30))
    fafnir.corporate_actions("ABC", date(2025, 1, 1), date(2025, 6, 30))
    fafnir.lookup_security("ABC", on=date(2025, 6, 30))
    fafnir.trading_calendar("NYSE", date(2025, 6, 30), date(2025, 6, 30))

    assert {c[3] for c in duk.calls} == {"th", "ph", "ca", "tc"}
    assert {c[3] for c in duk.calls} <= set(DUK_COMMANDS)
    assert all(c[1:3] == ["-S", "db"] for c in duk.calls)


# ── the day's range, through duk ─────────────────────────────────────────────


def test_r10_a_days_range_comes_through_duk_hlc_exactly(
    warehouse: _Warehouse, fafnir: FafnirProvider, duk: _Duk
) -> None:
    warehouse.security(1, "ABC", isin="US0000000001")
    warehouse.ticker("ABC", 1)
    warehouse.bar(1, "2025-06-30", "13247980.736555", low="13000000.5", high="13300000.25")

    (day,) = fafnir.daily_ranges("ABC", date(2025, 6, 30), date(2025, 6, 30))

    assert (day.price_date, day.low, day.high, day.close) == (
        date(2025, 6, 30),
        D("13000000.500000"),
        D("13300000.250000"),
        D("13247980.736555"),
    )
    assert day.source == "fafnir:duk ph;security_id=1;via=sid:1"
    assert "--hlc" in duk.calls[-1]
    assert "--adj" not in duk.calls[-1]
    assert fafnir.has(Capability.DAILY_RANGE)


def test_a_bar_whose_low_is_above_its_high_is_refused(
    warehouse: _Warehouse, fafnir: FafnirProvider, duk: _Duk
) -> None:
    warehouse.security(1, "ABC")
    warehouse.ticker("ABC", 1)
    duk.answers["ph"] = (
        0,
        '[{"date":"2025-06-30","high":"1.000000","low":"2.000000","close":"1.500000"}]',
        "",
    )

    with pytest.raises(DataUnavailableError, match="a low above its high"):
        fafnir.daily_ranges("ABC", date(2025, 6, 30), date(2025, 6, 30))


def test_without_duk_there_is_no_daily_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("portable_core.providers.fafnir.shutil.which", lambda _: None)

    assert not FafnirProvider(dsn="host=x").has(Capability.DAILY_RANGE)


# ── the trading calendar, through duk tc ─────────────────────────────────────


def test_r9_the_calendar_is_the_venues_sessions_and_its_whole_span(
    warehouse: _Warehouse, fafnir: FafnirProvider, duk: _Duk
) -> None:
    """fafnir seeds open days only, and marks an unforeseen closure ``is_open``
    false (its migration 0022): neither is a session, and the span is all the
    venue's calendar holds, not just the days asked for."""
    warehouse.sessions("NYSE", "2025-01-02", "2025-01-03", "2025-01-06", "2025-01-08")
    warehouse.sessions("NYSE", "2025-01-09", is_open=False)  # a national day of mourning
    warehouse.sessions("NYSE", "2025-12-31")
    warehouse.sessions("NASDAQ", "2025-01-07")

    calendar = fafnir.trading_calendar("nyse", date(2025, 1, 3), date(2025, 1, 9))

    assert calendar.exchange == "NYSE"
    assert calendar.open_days == {date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 8)}
    assert (calendar.first, calendar.last) == (date(2025, 1, 2), date(2025, 12, 31))
    assert calendar.source == "fafnir:duk tc"
    assert fafnir.has(Capability.TRADING_CALENDAR)
    assert ["duk", "-S", "db", "tc", "--json"] in duk.calls
    assert ["duk", "-S", "db", "tc", "NYSE", "--start-date", "2025-01-03",
            "--end-date", "2025-01-09", "--json"] in duk.calls  # fmt: skip


@pytest.mark.parametrize("venue", ["OTC", None])
def test_r9_a_venue_with_no_calendar_is_judged_by_nasdaqs(
    warehouse: _Warehouse, fafnir: FafnirProvider, venue: str | None
) -> None:
    """As fafnir's own ingestion judges it (CALENDAR_FALLBACK_EXCHANGE)."""
    warehouse.sessions("NASDAQ", "2025-01-02", "2025-01-03")

    calendar = fafnir.trading_calendar(venue, date(2025, 1, 1), date(2025, 1, 31))

    assert calendar.exchange == "NASDAQ"
    assert calendar.open_days == {date(2025, 1, 2), date(2025, 1, 3)}


def test_a_venue_with_a_calendar_but_no_days_in_the_range_has_no_sessions(
    warehouse: _Warehouse, fafnir: FafnirProvider
) -> None:
    """duk's "No trading calendar ... in that range" is an empty answer inside a span."""
    warehouse.sessions("NYSE", "2025-01-02", "2025-12-31")

    calendar = fafnir.trading_calendar("NYSE", date(2025, 7, 4), date(2025, 7, 4))

    assert calendar.open_days == frozenset()
    assert calendar.first == date(2025, 1, 2)


def test_no_calendar_at_all_is_refused(fafnir: FafnirProvider) -> None:
    with pytest.raises(
        DataUnavailableError, match="no trading calendar for OTC, nor for NASDAQ"
    ):
        fafnir.trading_calendar("OTC", date(2025, 1, 1), date(2025, 1, 31))


# ── the risk-free rate, through duk ──────────────────────────────────────────


def _duk(monkeypatch: pytest.MonkeyPatch, stdout: str) -> list[list[str]]:
    """Stand in for `duk`: found on PATH, answering with *stdout*."""
    calls: list[list[str]] = []

    def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("portable_core.providers.fafnir.shutil.which", lambda _: "/bin/duk")
    monkeypatch.setattr("portable_core.providers.fafnir.subprocess.run", run)
    return calls


_CURVE = json.dumps(
    [
        {"date": "2025-06-26T00:00:00.000", "month1": 4.31, "month3": 4.35, "year10": 4.25},
        {"date": "2025-06-27T00:00:00.000", "month1": 4.30, "month3": 4.3412, "year10": 4.28},
    ]
)


def test_the_rate_is_the_one_observed_on_the_date_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It used to read the live curve and label it with whatever date was asked."""
    calls = _duk(monkeypatch, _CURVE)

    rate = FafnirProvider(dsn="host=fake").risk_free_rate(date(2025, 6, 27), tenor="3M")

    assert rate.rate == D("4.3412"), "parsed as Decimal from the JSON text, not via a float"
    assert rate.as_of == date(2025, 6, 27)
    assert "month3" in rate.series_name
    assert calls[0][calls[0].index("--end-date") + 1] == "2025-06-27"


def test_a_date_with_no_observation_is_refused_naming_the_nearest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Saturday's rate is not Friday's, unless a person says it is."""
    _duk(monkeypatch, _CURVE)

    with pytest.raises(DataUnavailableError) as refused:
        FafnirProvider(dsn="host=fake").risk_free_rate(date(2025, 6, 28), tenor="10Y")
    assert refused.value.context["nearest_earlier"] == "2025-06-27"


def test_a_single_undated_curve_is_not_read_as_dated(monkeypatch: pytest.MonkeyPatch) -> None:
    """duk's one-observation shape carries maturity dates, not the observation date."""
    _duk(
        monkeypatch,
        json.dumps([{"index": "month3", "years": 0.25, "date": "2025-09-27", "par_rate": 4.3}]),
    )

    with pytest.raises(DataUnavailableError, match="no 3M treasury yield observed"):
        FafnirProvider(dsn="host=fake").risk_free_rate(date(2025, 6, 27), tenor="3M")


@pytest.mark.parametrize(
    ("tenor", "column"),
    [("3M", "month3"), ("1y", "year1"), ("10Y", "year10"), ("month6", "month6")],
)
def test_tenors_map_to_the_curves_columns(
    monkeypatch: pytest.MonkeyPatch, tenor: str, column: str
) -> None:
    """'3M' used to be looked up as 'month3m', so no tenor was ever found."""
    curve = json.dumps(
        [{"date": "2025-06-27", column: 1.5}, {"date": "2025-06-26", column: 1.4}]
    )
    _duk(monkeypatch, curve)

    assert FafnirProvider(dsn="host=fake").risk_free_rate(
        date(2025, 6, 27), tenor=tenor
    ).rate == (D("1.5"))


def test_an_unknown_tenor_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _duk(monkeypatch, _CURVE)
    with pytest.raises(ValidationError, match="unknown tenor"):
        FafnirProvider(dsn="host=fake").risk_free_rate(date(2025, 6, 27), tenor="3W")
