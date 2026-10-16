"""The fafnir adapter. The only module in `portable` that knows fafnir exists.

ADR 0006 records the decision and the reasoning; `CLAUDE.md` forbids coupling
to fafnir internals anywhere else, and ``tests/unit/test_layering.py`` enforces
it. Every table and column name is in :data:`SCHEMA` at the top of this module,
so a fafnir migration is a one-file change here.

**Two capabilities fafnir does not have**, both load-bearing:

1. **No total-return benchmark series.** The warehouse carries security prices,
   not index levels. ``PORT-GIPS-G01`` prohibits price-only benchmarks and
   requires `portable` to *refuse* rather than warn -- so this provider declares
   **no benchmark capability at all**. Synthesising a total return by adding
   ``core.corporate_action`` dividends to a price series would produce a
   defensible-looking wrong number of exactly the kind this repository exists to
   prevent. Benchmarks come from :class:`FileProvider` with an explicit
   ``return_type``.
2. **No treasury curve.** fafnir's own docs say ``duk yc`` is live-only "until
   the economic-series fast-follow adds treasury data to the warehouse". So the
   risk-free rate shells out to `duk`, which **is** the documented interface for
   it -- and for nothing else.

**Unadjusted prices only.** ``core.daily_price`` is the raw fact table;
``mart.v_daily_price_adjusted`` is deliberately not read by the valuation path,
because ``PORT-GIPS-A01`` requires fair value on the measurement date and
adjusted prices double-count splits. The SQL that would read the adjusted view
is not written, so the mistake is not available.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from portable_core.decimals import from_text
from portable_core.domain.enums import InstrumentType, ValuationBasis
from portable_core.domain.models import Instrument, Price
from portable_core.errors import DataUnavailableError, ValidationError
from portable_core.errors.kinds import (
    E_PRICE_MISSING,
    E_PROVIDER_UNAVAILABLE,
)
from portable_core.providers.base import (
    Capability,
    CorporateActionRecord,
    MarketDataProvider,
    RiskFreeRate,
)

__all__ = ["SCHEMA", "FafnirProvider", "resolve_dsn"]

#: fafnir's schema, in one place. Confirmed against its `doc/data_dictionary.md`.
#: If fafnir migrates, this mapping is what changes -- and nothing else in
#: `portable` does.
SCHEMA: Final[dict[str, str]] = {
    "security": "core.security",
    "symbol_xref": "core.symbol_xref",
    # The UNADJUSTED daily fact table. Never the adjusted mart view.
    "daily_price": "core.daily_price",
    "corporate_action": "core.corporate_action",
    "trading_calendar": "ref.trading_calendar",
}

#: DSN resolution order, highest first. `portable`'s own variable wins, then
#: fafnir's, then fafnir's config files -- so somebody who already has `duk`
#: working needs no new configuration (ADR 0006).
_DSN_ENV_ORDER: Final[tuple[str, ...]] = ("PORTABLE_FAFNIR_DSN", "FAFNIR_DSN")
#: fafnir's own config filenames, resolved against the home directory when
#: needed. NOT at import: ``Path.home()`` raises on Windows with no
#: ``USERPROFILE``, and doing this at module scope made the module unimportable
#: there. See :func:`portable_core.config.user_config_path`.
_DSN_FILENAMES: Final[tuple[str, ...]] = (".dukrc", ".fafnirrc")


def _dsn_files() -> tuple[Path, ...]:
    """fafnir's config files, or an empty tuple when there is no home."""
    try:
        home = Path.home()
    except (RuntimeError, OSError):
        return ()
    return tuple(home / name for name in _DSN_FILENAMES)


def resolve_dsn(explicit: str | None = None) -> str | None:
    """Find a fafnir DSN without ever reading a secret from `portable`'s config.

    Bootstrap §6.3: never read secrets from a config file when an environment
    variable is available, and never log them. The environment is checked
    first for exactly that reason, and the value is never echoed anywhere --
    `portable config show` redacts it.
    """
    if explicit:
        return explicit
    for name in _DSN_ENV_ORDER:
        value = os.environ.get(name)
        if value:
            return value
    for path in _dsn_files():
        if not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                loaded = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        database = loaded.get("database")
        if isinstance(database, dict) and database.get("dsn"):
            return str(database["dsn"])
    return None


class FafnirProvider(MarketDataProvider):
    """Reads the owner's fafnir warehouse directly over psycopg.

    ``psycopg`` is an optional dependency (``pip install portable[fafnir]``).
    Its absence makes this provider unavailable with a clear message rather
    than an ``ImportError`` at CLI start.
    """

    name = "fafnir"

    def __init__(
        self,
        dsn: str | None = None,
        *,
        duk_path: str = "duk",
        connect_timeout: int = 10,
    ) -> None:
        self.dsn = resolve_dsn(dsn)
        self.duk_path = duk_path
        self.connect_timeout = connect_timeout
        self._connection: Any = None

    @property
    def capabilities(self) -> frozenset[Capability]:
        """What fafnir can actually supply.

        Note the two absences, and see the module docstring: **no BENCHMARKS**
        (no total-return index series exists in the warehouse, and
        ``PORT-GIPS-G01`` says refuse rather than approximate) and no FX.
        ``RISK_FREE_RATE`` is present only when `duk` is on PATH, because that
        is where it comes from.
        """
        found = {
            Capability.SECURITY_MASTER,
            Capability.EOD_PRICES,
            Capability.CORPORATE_ACTIONS,
            Capability.DIVIDENDS,
        }
        if shutil.which(self.duk_path):
            found.add(Capability.RISK_FREE_RATE)
        return frozenset(found)

    # ── connection ───────────────────────────────────────────────────────────

    def _connect(self) -> Any:
        if self._connection is not None:
            return self._connection

        try:
            import psycopg
        except ImportError as exc:
            raise DataUnavailableError(
                "the fafnir provider needs psycopg, which is not installed",
                code=E_PROVIDER_UNAVAILABLE,
                remedy=(
                    "Install it with `pip install 'portable[fafnir]'`, or use "
                    "`--source file` with a price file, or `--offline` to work from "
                    "prices already cached in the .port file."
                ),
                provider=self.name,
            ) from exc

        if not self.dsn:
            raise DataUnavailableError(
                "no fafnir DSN found",
                code=E_PROVIDER_UNAVAILABLE,
                remedy=(
                    "Set PORTABLE_FAFNIR_DSN or FAFNIR_DSN, or configure "
                    "[database].dsn in ~/.dukrc. Point it at the least-privilege "
                    "fafnir_app role."
                ),
                provider=self.name,
                checked=[*_DSN_ENV_ORDER, *(str(p) for p in _dsn_files())],
            )

        try:
            self._connection = psycopg.connect(self.dsn, connect_timeout=self.connect_timeout)
        except Exception as exc:  # psycopg raises a family of these
            # The DSN is NOT included in the message or the context: it carries
            # a password (bootstrap §6.3).
            raise DataUnavailableError(
                f"cannot reach the fafnir warehouse: {type(exc).__name__}",
                code=E_PROVIDER_UNAVAILABLE,
                remedy=(
                    "Check the warehouse is running and the DSN is right. "
                    "`--offline` works from prices already cached in the .port file."
                ),
                provider=self.name,
            ) from exc
        return self._connection

    def _query(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        connection = self._connect()
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())

    # ── security master ──────────────────────────────────────────────────────

    def _segments(self, symbol: str, start: date, end: date) -> list[_Segment]:
        """Which security *symbol* named on each day from *start* to *end*.

        Through ``core.symbol_xref``, whose periods run from ``valid_from`` to
        ``valid_to`` inclusive (``NULL`` = still open). A ticker can name one
        security for part of a range and another for the rest -- a rename, a
        delisting followed by reuse -- so a range is answered segment by
        segment, never by resolving the ticker once and applying the answer to
        every date. Where two periods cover the same day, the security whose
        primary ticker it is wins, then the later period: the same rule a single
        date has always used.

        There is no fallback to ``core.security.primary_symbol``. fafnir's own
        resolver uses it to say who holds a ticker *now*; its data dictionary
        says the column is "not an identifier", and here the question is always
        who held it *then*.

        Raises:
            DataUnavailableError: some day in the range resolves to no security.
                The uncovered spans are named; a partial answer would price part
                of a history and leave the rest to look like a quiet market.
        """
        if end < start:
            raise ValidationError(
                f"the range for {symbol} ends ({end.isoformat()}) before it starts "
                f"({start.isoformat()})",
                remedy="Check the order of the dates.",
                symbol=symbol,
            )
        rows = self._query(
            "SELECT security_id, valid_from, valid_to, is_primary "  # noqa: S608 -- table names come from the SCHEMA constant; all values are bound
            f"FROM {SCHEMA['symbol_xref']} "
            "WHERE upper(symbol) = upper(%s) AND valid_from <= %s "
            "AND (valid_to IS NULL OR valid_to >= %s) ORDER BY valid_from",
            (symbol, end.isoformat(), start.isoformat()),
        )
        periods = [
            (
                int(security_id),
                _as_date(valid_from),
                _as_date(valid_to) if valid_to is not None else None,
                bool(is_primary),
            )
            for security_id, valid_from, valid_to, is_primary in rows
        ]

        # Between two consecutive boundaries the set of covering periods cannot
        # change, so each stretch is decided by its first day.
        boundaries = {start}
        for _, valid_from, valid_to, _ in periods:
            if start < valid_from <= end:
                boundaries.add(valid_from)
            if valid_to is not None and start <= valid_to < end:
                boundaries.add(valid_to + timedelta(days=1))
        ordered = sorted(boundaries)

        segments: list[_Segment] = []
        uncovered: list[str] = []
        for position, first in enumerate(ordered):
            last = (
                ordered[position + 1] - timedelta(days=1)
                if position + 1 < len(ordered)
                else end
            )
            covering = [p for p in periods if p[1] <= first and (p[2] is None or p[2] >= first)]
            if not covering:
                uncovered.append(
                    first.isoformat()
                    if first == last
                    else f"{first.isoformat()}..{last.isoformat()}"
                )
                continue
            security_id = max(covering, key=lambda p: (p[3], p[1]))[0]
            if segments and segments[-1].security_id == security_id:
                segments[-1] = _Segment(security_id, segments[-1].start, last)
            else:
                segments.append(_Segment(security_id, first, last))

        if uncovered:
            raise DataUnavailableError(
                f"{symbol} does not name a security in fafnir's symbol history on "
                f"every date from {start.isoformat()} to {end.isoformat()}",
                code=E_PRICE_MISSING,
                remedy=(
                    "Narrow the range to the dates the ticker was in use -- under a "
                    "former ticker, ask for that one -- or supply the uncovered dates "
                    "with `--source file`."
                ),
                symbol=symbol,
                provider=self.name,
                unresolved=uncovered,
            )
        return segments

    def _security_id(self, symbol: str, on: date) -> int:
        """The security *symbol* named on *on* -- the one-day case of the above.

        The date is required. Defaulting it to today made every lookup a
        question about the present, asked with no date in sight (`CLAUDE.md`
        invariant 6), and answered for a historical instrument with whoever
        holds its ticker now.
        """
        return self._segments(symbol, on, on)[0].security_id

    def lookup_security(self, symbol: str, *, on: date) -> Instrument:
        security_id = self._security_id(symbol, on)
        rows = self._query(
            "SELECT primary_symbol, company_name, asset_type, exchange_code, currency, "  # noqa: S608 -- table names come from the SCHEMA constant; all values are bound
            f"country, cusip, isin, is_actively_trading FROM {SCHEMA['security']} "
            "WHERE security_id = %s",
            (security_id,),
        )
        primary, name, asset_type, exchange, currency, country, cusip, isin, active = rows[0]
        instrument_type = _INSTRUMENT_TYPES.get(str(asset_type))
        if instrument_type is None:
            # fafnir's asset_type also has 'other'. Calling that an equity would
            # be a plausible default, and the type decides how it is valued.
            raise DataUnavailableError(
                f"fafnir classifies {symbol} as {asset_type!r}, which portable has no "
                "instrument type for",
                code=E_PRICE_MISSING,
                remedy=f"Add it by hand with `pt instrument add {symbol} --type TYPE`.",
                symbol=symbol,
                provider=self.name,
                asset_type=str(asset_type),
            )
        return Instrument(
            instrument_id=0,
            symbol=str(primary),
            instrument_type=instrument_type,
            name=name,
            currency=str(currency or "USD"),
            exchange=exchange,
            cusip=cusip,
            isin=isin,
            country=country,
            is_active=bool(active),
            source=f"{self.name}:{SCHEMA['security']}",
            provider_ref=f"security_id={security_id}",
        )

    # ── prices ───────────────────────────────────────────────────────────────

    def eod_prices(self, symbol: str, start: date, end: date) -> list[Price]:
        """Unadjusted end-of-day closes from ``core.daily_price``.

        There is no ``adjusted=True`` parameter and no adjusted-view branch.
        ``PORT-GIPS-A01`` requires fair value on the measurement date, and
        `CLAUDE.md` requires unadjusted prices with explicit corporate-action
        transactions -- so the adjusted path is not a discouraged option here,
        it is unwritten.

        Each day is priced from the security the ticker named **that day**
        (:meth:`_segments`), so a range across a rename or a reused ticker is
        not filled with another issuer's prices. ``security_id`` and
        ``ingestion_run_id`` travel into ``provider_ref``, so a valuation traces
        to the warehouse rows that produced it (``PORT-GIPS-J03``).
        """
        prices: list[Price] = []
        for segment in self._segments(symbol, start, end):
            rows = self._query(
                "SELECT trade_date, close, source, ingestion_run_id, loaded_at "  # noqa: S608 -- table names come from the SCHEMA constant; all values are bound
                f"FROM {SCHEMA['daily_price']} "
                "WHERE security_id = %s AND trade_date BETWEEN %s AND %s "
                "ORDER BY trade_date",
                (segment.security_id, segment.start.isoformat(), segment.end.isoformat()),
            )
            for trade_date, close, source, run_id, loaded_at in rows:
                prices.append(
                    Price(
                        instrument_id=0,
                        price_date=_as_date(trade_date),
                        # str() then from_text(): psycopg returns Decimal for
                        # NUMERIC, but going through the canonical text form
                        # means a driver change cannot quietly introduce a
                        # float here.
                        price=from_text(str(close)),
                        source=f"{self.name}:{SCHEMA['daily_price']}",
                        as_of=_as_datetime(loaded_at),
                        valuation_level=1,
                        valuation_basis=ValuationBasis.EXCHANGE_CLOSE,
                        is_estimate=False,
                        provider_ref=(
                            f"security_id={segment.security_id};"
                            f"trade_date={_as_date(trade_date).isoformat()};"
                            # NULL on a bar an operator re-dated or re-scaled
                            # (fafnir's `source = 'operator'`); said so rather
                            # than rendered as the string "None".
                            f"ingestion_run_id={'' if run_id is None else run_id};"
                            f"source={source}"
                        ),
                    )
                )
        if not prices:
            raise DataUnavailableError(
                f"no fafnir prices for {symbol} between {start.isoformat()} and "
                f"{end.isoformat()}",
                code=E_PRICE_MISSING,
                remedy=(
                    "Backfill the range in fafnir, or supply the prices with `--source file`."
                ),
                symbol=symbol,
                provider=self.name,
            )
        return prices

    # ── corporate actions ────────────────────────────────────────────────────

    def corporate_actions(
        self, symbol: str, start: date, end: date
    ) -> list[CorporateActionRecord]:
        """Splits and cash dividends -- which is what fafnir carries.

        Spinoffs, mergers, symbol changes, and delistings are **not** in
        ``core.corporate_action``. `pt ca sync` says so rather than implying the
        list is complete, because a silent omission here is a missing basis
        adjustment.
        """
        rows: list[tuple[Any, ...]] = []
        for segment in self._segments(symbol, start, end):
            rows.extend(
                self._query(
                    "SELECT action_type, ex_date, record_date, payment_date, "  # noqa: S608 -- table names come from the SCHEMA constant; all values are bound
                    "split_numerator, split_denominator, dividend_amount, corporate_action_id "
                    f"FROM {SCHEMA['corporate_action']} "
                    "WHERE security_id = %s AND ex_date BETWEEN %s AND %s ORDER BY ex_date",
                    (segment.security_id, segment.start.isoformat(), segment.end.isoformat()),
                )
            )
        return [
            CorporateActionRecord(
                symbol=symbol.upper(),
                action_type="cash_dividend" if str(kind) == "dividend" else str(kind),
                ex_date=_as_date(ex_date),
                record_date=_as_date(record_date) if record_date else None,
                pay_date=_as_date(pay_date) if pay_date else None,
                split_numerator=from_text(str(num)) if num is not None else None,
                split_denominator=from_text(str(den)) if den is not None else None,
                cash_amount=from_text(str(amount)) if amount is not None else None,
                provider_ref=f"corporate_action_id={action_id}",
            )
            for kind, ex_date, record_date, pay_date, num, den, amount, action_id in rows
        ]

    # ── risk-free rate, via duk ──────────────────────────────────────────────

    def risk_free_rate(self, on: date, *, tenor: str = "3M") -> RiskFreeRate:
        """The treasury par yield for *tenor*, observed on *on*, through `duk yc`.

        This is the one place `portable` shells out to fafnir's CLI, and it is
        the right call: fafnir's own documentation says ``yc`` is live-only
        "until the economic-series fast-follow adds treasury data to the
        warehouse", so there is no table to query and `duk` **is** the
        documented interface (ADR 0006).

        The curve is asked for over a window ending on *on*, so `duk` returns
        one dated row per observation, and the row dated *on* is the answer.
        There is no nearest-date fallback: a rate observed on another day,
        labelled with this one, is a wrong number with a plausible face. Asking
        for the single date instead would not do -- `duk` then returns one
        curve with no observation date to check.

        Numbers are parsed as ``Decimal`` straight from the JSON text, never
        through a float (`CLAUDE.md` invariant 1). The rate is as published:
        percent per annum.

        The series is named specifically in the result because
        ``PORT-GIPS-F05`` requires the *name* of the risk-free rate to be
        disclosed. "The risk-free rate" is not an acceptable disclosure.
        """
        self.require(Capability.RISK_FREE_RATE)
        key = _tenor_key(tenor)
        window = (on - timedelta(days=_CURVE_WINDOW_DAYS)).isoformat()
        argv = [
            self.duk_path,
            "-S",
            "live",
            "yc",
            "--start-date",
            window,
            "--end-date",
            on.isoformat(),
            "--json",
        ]
        try:
            completed = subprocess.run(  # noqa: S603 -- fixed argv, no shell
                argv, capture_output=True, text=True, check=True, timeout=60
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DataUnavailableError(
                f"`{self.duk_path} yc` failed: {exc}",
                code=E_PROVIDER_UNAVAILABLE,
                remedy=(
                    "duk's yield curve is live-only and needs an FMP API key "
                    "(FMP_API_KEY). Supply the rate by hand if the curve is "
                    "unavailable -- portable will not substitute a guess."
                ),
                provider=self.name,
            ) from exc

        observations = _curve_rows(completed.stdout)
        found = observations.get(on)
        rate = found.get(key) if found is not None else None
        if rate is None:
            earlier = sorted(d for d, row in observations.items() if d < on and key in row)
            raise DataUnavailableError(
                f"no {tenor} treasury yield observed on {on.isoformat()}",
                code=E_PRICE_MISSING,
                remedy=(
                    "A curve is published on business days only. Ask for the date it "
                    "was observed, if that is the rate you mean"
                    + (
                        f" -- the nearest earlier is {earlier[-1].isoformat()}."
                        if earlier
                        else "."
                    )
                ),
                tenor=tenor,
                provider=self.name,
                nearest_earlier=earlier[-1].isoformat() if earlier else None,
            )
        return RiskFreeRate(
            series_name=f"US Treasury par yield curve, {tenor} ({key}), via FMP",
            tenor=tenor,
            rate=rate,
            as_of=on,
            source=" ".join(argv),
        )

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


@dataclass(frozen=True, slots=True)
class _Segment:
    """A stretch of days on which a ticker named one security."""

    security_id: int
    start: date
    end: date


#: fafnir's `asset_type` values, mapped to `portable`'s instrument types.
#: fafnir's fourth value, 'other', is deliberately absent: it is refused.
_INSTRUMENT_TYPES: Final[dict[str, InstrumentType]] = {
    "equity": InstrumentType.EQUITY,
    "etf": InstrumentType.ETF,
    "fund": InstrumentType.MUTUAL_FUND,
}


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.combine(_as_date(value), datetime.min.time(), tzinfo=UTC)


#: How far back the curve is requested, so that `duk` returns dated rows. Two
#: weeks always spans more than one business day.
_CURVE_WINDOW_DAYS: Final = 14

_TENOR_UNITS: Final[dict[str, str]] = {"M": "month", "Y": "year"}


def _tenor_key(tenor: str) -> str:
    """``3M`` -> ``month3``, ``10Y`` -> ``year10``: the curve's own column names.

    The curve's names are accepted as given too. Anything else is refused
    rather than looked up loosely: a tenor that matched the wrong column would
    be a rate for the wrong maturity.
    """
    text = tenor.strip()
    lowered = text.lower()
    if lowered.startswith(("month", "year")) and lowered[-1:].isdigit():
        return lowered
    unit = _TENOR_UNITS.get(text[-1:].upper())
    if unit is None or not text[:-1].isdigit():
        raise ValidationError(
            f"unknown tenor {tenor!r}",
            remedy="Give a tenor such as 1M, 3M, 6M, 1Y, 10Y or 30Y.",
            tenor=tenor,
        )
    return f"{unit}{int(text[:-1])}"


def _curve_rows(stdout: str) -> dict[date, dict[str, Decimal]]:
    """`duk yc --json` output as ``{observation date: {column: rate}}``.

    Only the dated, one-row-per-observation shape is read. `duk` answers a
    window holding a single observation with a different shape whose ``date``
    column is a maturity estimate, not the observation date, so it is refused
    rather than read as if it were dated.
    """
    try:
        parsed = json.loads(stdout, parse_float=Decimal, parse_int=Decimal)
    except json.JSONDecodeError:
        return {}  # `duk` prints a sentence, not JSON, when FMP has nothing
    if not isinstance(parsed, list):
        return {}
    rows: dict[date, dict[str, Decimal]] = {}
    for record in parsed:
        if not isinstance(record, dict) or "years" in record or "date" not in record:
            return {}
        rates = {
            str(column): value
            for column, value in record.items()
            if column != "date" and isinstance(value, Decimal)
        }
        rows[date.fromisoformat(str(record["date"])[:10])] = rates
    return rows
