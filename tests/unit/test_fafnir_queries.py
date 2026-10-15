"""The fafnir adapter's queries, run against a warehouse shaped like fafnir's.

Until this file, no test executed a single query in `providers/fafnir.py`: the
SQL was checked as text and the DSN handling as configuration, and the column
names had never met a table. The fake here is SQLite with `core` and `ref`
attached as schemas, so the adapter's *own* SQL runs unmodified (bar the
placeholder style) against tables laid out as fafnir's `doc/data_dictionary.md`
(v1.5.0) states them. Its cursor hands back what psycopg would -- `date`,
`Decimal` for NUMERIC, `bool` -- so the type handling is exercised too.

ADR 0020 R5 is the reason most of these exist: a ticker can name one security
for part of a range and another for the rest, and a historical load spans
years.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from portable_core.domain.enums import InstrumentType
from portable_core.errors import DataUnavailableError, ValidationError
from portable_core.providers.fafnir import FafnirProvider

pytestmark = pytest.mark.unit

D = Decimal

#: Column types psycopg would hand back, by column name.
_DATES = {"trade_date", "valid_from", "valid_to", "ex_date", "record_date", "payment_date"}
_NUMERIC = {"close", "split_numerator", "split_denominator", "dividend_amount"}
_BOOLEAN = {"is_primary", "is_actively_trading"}

_DDL = (
    """CREATE TABLE core.security (
        security_id INTEGER PRIMARY KEY, primary_symbol TEXT NOT NULL, company_name TEXT,
        asset_type TEXT CHECK (asset_type IN ('equity', 'etf', 'fund', 'other')),
        exchange_code TEXT, currency TEXT DEFAULT 'USD', country TEXT, cik TEXT,
        isin TEXT, cusip TEXT, is_actively_trading INTEGER, delisted_date TEXT)""",
    """CREATE TABLE core.symbol_xref (
        security_id INTEGER, symbol TEXT, valid_from TEXT DEFAULT '1900-01-01',
        valid_to TEXT, is_primary INTEGER, source TEXT)""",
    """CREATE TABLE core.daily_price (
        security_id INTEGER, trade_date TEXT, open TEXT, high TEXT, low TEXT, close TEXT,
        volume INTEGER, vwap TEXT, source TEXT, ingestion_run_id INTEGER, loaded_at TEXT,
        PRIMARY KEY (security_id, trade_date))""",
    """CREATE TABLE core.corporate_action (
        corporate_action_id INTEGER PRIMARY KEY, security_id INTEGER, action_type TEXT,
        ex_date TEXT, record_date TEXT, payment_date TEXT, declaration_date TEXT,
        split_numerator TEXT, split_denominator TEXT, dividend_amount TEXT,
        currency TEXT, source TEXT, ingestion_run_id INTEGER, loaded_at TEXT)""",
    """CREATE TABLE ref.trading_calendar (
        exchange_code TEXT, trade_date TEXT, is_open INTEGER, session_note TEXT)""",
)


class _Cursor:
    def __init__(self, warehouse: _Warehouse) -> None:
        self._warehouse = warehouse
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self._warehouse.queries.append(sql)
        cursor = self._warehouse.db.execute(sql.replace("%s", "?"), params)
        names = [column[0] for column in cursor.description]
        self._rows = [
            tuple(_as_psycopg(name, value) for name, value in zip(names, row, strict=True))
            for row in cursor.fetchall()
        ]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


def _as_psycopg(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name in _DATES:
        return date.fromisoformat(value)
    if name in _NUMERIC:
        return Decimal(value)
    if name in _BOOLEAN:
        return bool(value)
    if name == "loaded_at":
        return datetime.fromisoformat(value)
    return value


class _Warehouse:
    """A psycopg-shaped connection over SQLite, holding fafnir's tables."""

    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute("ATTACH DATABASE ':memory:' AS core")
        self.db.execute("ATTACH DATABASE ':memory:' AS ref")
        for ddl in _DDL:
            self.db.execute(ddl)
        self.queries: list[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def close(self) -> None:
        self.db.close()

    # ── loading ──────────────────────────────────────────────────────────────

    def security(self, security_id: int, symbol: str, asset_type: str = "equity") -> None:
        self.db.execute(
            "INSERT INTO core.security (security_id, primary_symbol, company_name, "
            "asset_type, exchange_code, country, cusip, isin, is_actively_trading) "
            "VALUES (?, ?, ?, ?, 'NYSE', 'US', ?, ?, 1)",
            (security_id, symbol, f"{symbol} Inc.", asset_type, f"C{security_id:08d}", None),
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
            "INSERT INTO core.symbol_xref VALUES (?, ?, ?, ?, ?, 'fmp')",
            (security_id, symbol, valid_from, valid_to, int(primary)),
        )

    def bar(
        self,
        security_id: int,
        on: str,
        close: str,
        *,
        run: int | None = 42,
        source: str = "fmp",
    ) -> None:
        self.db.execute(
            "INSERT INTO core.daily_price (security_id, trade_date, open, high, low, close, "
            "volume, source, ingestion_run_id, loaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 100, ?, ?, '2026-10-01T22:15:00+00:00')",
            (security_id, on, close, close, close, close, source, run),
        )

    def dividend(self, security_id: int, ex_date: str, amount: str) -> None:
        self.db.execute(
            "INSERT INTO core.corporate_action (security_id, action_type, ex_date, "
            "payment_date, dividend_amount) VALUES (?, 'dividend', ?, ?, ?)",
            (security_id, ex_date, ex_date, amount),
        )


@pytest.fixture
def warehouse() -> Iterator[_Warehouse]:
    built = _Warehouse()
    yield built
    built.close()


@pytest.fixture
def fafnir(warehouse: _Warehouse) -> FafnirProvider:
    provider = FafnirProvider(dsn="host=fake")
    # The adapter connects lazily and caches the connection; handing it the
    # fake is the whole of the injection.
    provider._connection = warehouse
    return provider


# ── prices across a ticker's history ────────────────────────────────────────


def test_a_reused_ticker_is_priced_from_the_security_that_held_it_each_day(
    warehouse: _Warehouse, fafnir: FafnirProvider
) -> None:
    """ADR 0020 R5. The bug this replaces resolved the ticker once, as of the end.

    XYZ named security 1 through 2023, and security 2 -- which traded before
    that as ABC -- from 2024. Resolved as of the range's end, every 2023 date
    was priced from security 2's ABC bars: another issuer's prices, under the
    right ticker, with nothing to say so.
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
        (date(2023, 12, 29), D("10.00")),
        (date(2024, 1, 2), D("251.00")),
    ]
    assert prices[0].provider_ref is not None and prices[0].provider_ref.startswith(
        "security_id=1;"
    )
    assert prices[1].provider_ref is not None and prices[1].provider_ref.startswith(
        "security_id=2;"
    )


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


def test_a_price_carries_its_value_exactly_and_its_lineage(
    warehouse: _Warehouse, fafnir: FafnirProvider
) -> None:
    """PORT-GIPS-J03: the warehouse rows behind a valuation, not just the vendor."""
    warehouse.security(1, "ABC")
    warehouse.ticker("ABC", 1)
    warehouse.bar(1, "2025-06-30", "189.123456")
    warehouse.bar(1, "2025-07-01", "190.000001", run=None, source="operator")

    first, second = fafnir.eod_prices("ABC", date(2025, 6, 30), date(2025, 7, 1))

    assert first.price == D("189.123456")
    assert first.provider_ref == (
        "security_id=1;trade_date=2025-06-30;ingestion_run_id=42;source=fmp"
    )
    assert first.source == "fafnir:core.daily_price"
    assert first.valuation_level == 1
    assert first.as_of == datetime(2026, 10, 1, 22, 15, tzinfo=UTC)
    assert second.provider_ref == (
        "security_id=1;trade_date=2025-07-01;ingestion_run_id=;source=operator"
    ), "an operator bar has no run, and says so rather than 'None'"


def test_a_resolvable_range_with_no_bars_is_price_missing(
    warehouse: _Warehouse, fafnir: FafnirProvider
) -> None:
    warehouse.security(1, "ABC")
    warehouse.ticker("ABC", 1)

    with pytest.raises(DataUnavailableError, match="no fafnir prices for ABC") as refused:
        fafnir.eod_prices("ABC", date(2025, 6, 30), date(2025, 7, 1))
    assert refused.value.code == "PT-E-PRICE-MISSING"


def test_a_backwards_range_is_refused(fafnir: FafnirProvider) -> None:
    with pytest.raises(ValidationError, match="ends"):
        fafnir.eod_prices("ABC", date(2025, 7, 1), date(2025, 6, 30))


# ── corporate actions and the security master ───────────────────────────────


def test_corporate_actions_follow_the_ticker_through_its_history(
    warehouse: _Warehouse, fafnir: FafnirProvider
) -> None:
    warehouse.security(1, "XYZ")
    warehouse.security(2, "XYZ")
    warehouse.ticker("XYZ", 1, valid_to="2023-12-31")
    warehouse.ticker("XYZ", 2, valid_from="2024-01-01")
    warehouse.dividend(1, "2023-11-15", "0.25")
    warehouse.dividend(2, "2023-11-15", "9.99")  # security 2's, before it was XYZ
    warehouse.dividend(2, "2024-02-15", "0.40")
    warehouse.db.execute(
        "INSERT INTO core.corporate_action (security_id, action_type, ex_date, "
        "split_numerator, split_denominator) VALUES (2, 'split', '2024-03-01', '4', '1')"
    )

    actions = fafnir.corporate_actions("XYZ", date(2023, 1, 1), date(2024, 12, 31))

    assert [(a.action_type, a.ex_date, a.cash_amount) for a in actions] == [
        ("cash_dividend", date(2023, 11, 15), D("0.25")),
        ("cash_dividend", date(2024, 2, 15), D("0.40")),
        ("split", date(2024, 3, 1), None),
    ]
    assert (actions[2].split_numerator, actions[2].split_denominator) == (D("4"), D("1"))


def test_lookup_maps_the_master_row_as_of_the_date(
    warehouse: _Warehouse, fafnir: FafnirProvider
) -> None:
    """A fund is a fund: fafnir carries NAV-priced funds in the same tables."""
    warehouse.security(9, "FSMVX", asset_type="fund")
    warehouse.ticker("FSMVX", 9, valid_to="2023-08-31")

    found = fafnir.lookup_security("FSMVX", on=date(2023, 5, 1))

    assert found.instrument_type is InstrumentType.MUTUAL_FUND
    assert found.cusip == "C00000009"
    assert found.provider_ref == "security_id=9"
    with pytest.raises(DataUnavailableError, match="symbol history"):
        fafnir.lookup_security("FSMVX", on=date(2024, 5, 1))


def test_an_asset_type_portable_has_no_name_for_is_refused(
    warehouse: _Warehouse, fafnir: FafnirProvider
) -> None:
    """fafnir's 'other' used to become an equity -- a default with a valuation attached."""
    warehouse.security(6, "WEIRD", asset_type="other")
    warehouse.ticker("WEIRD", 6)

    with pytest.raises(DataUnavailableError, match="'other'"):
        fafnir.lookup_security("WEIRD", on=date(2025, 6, 30))


def test_every_query_names_only_tables_in_the_schema_mapping(
    warehouse: _Warehouse, fafnir: FafnirProvider
) -> None:
    """ADR 0006: a fafnir migration is a one-file change, and here is the file."""
    warehouse.security(1, "ABC")
    warehouse.ticker("ABC", 1)
    warehouse.bar(1, "2025-06-30", "1.00")
    fafnir.eod_prices("ABC", date(2025, 6, 30), date(2025, 6, 30))
    fafnir.corporate_actions("ABC", date(2025, 1, 1), date(2025, 6, 30))
    fafnir.lookup_security("ABC", on=date(2025, 6, 30))

    named = {word for query in warehouse.queries for word in query.split() if "." in word} - {
        "0",
        "",
    }
    assert named <= {"core.security", "core.symbol_xref", "core.daily_price",
                     "core.corporate_action"}  # fmt: skip


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
