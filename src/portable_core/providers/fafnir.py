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
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from portable_core.decimals import from_text
from portable_core.domain.enums import InstrumentType, ValuationBasis
from portable_core.domain.models import Instrument, Price
from portable_core.errors import DataUnavailableError
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
_DSN_FILES: Final[tuple[Path, ...]] = (
    Path.home() / ".dukrc",
    Path.home() / ".fafnirrc",
)


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
    for path in _DSN_FILES:
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
                checked=[*_DSN_ENV_ORDER, *(str(p) for p in _DSN_FILES)],
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

    def _security_id(self, symbol: str, on: date | None = None) -> int:
        """Resolve a ticker to a ``security_id``, **as of a date**.

        Through ``core.symbol_xref`` rather than ``core.security.primary_symbol``,
        because fafnir's own data dictionary says the primary symbol is "**not**
        an identifier". A ticker that has been reassigned resolves to whichever
        security held it on the trade date, which is the only answer that does
        not silently rewrite history.
        """
        as_of = (on or date.today()).isoformat()  # noqa: DTZ011 -- a date, not a timestamp
        rows = self._query(
            f"SELECT security_id FROM {SCHEMA['symbol_xref']} "  # noqa: S608 -- table names come from the SCHEMA constant; all values are bound
            "WHERE upper(symbol) = upper(%s) AND valid_from <= %s "
            "AND (valid_to IS NULL OR valid_to >= %s) "
            "ORDER BY is_primary DESC, valid_from DESC LIMIT 1",
            (symbol, as_of, as_of),
        )
        if not rows:
            rows = self._query(
                f"SELECT security_id FROM {SCHEMA['security']} "  # noqa: S608 -- table names come from the SCHEMA constant; all values are bound
                "WHERE upper(primary_symbol) = upper(%s) LIMIT 1",
                (symbol,),
            )
        if not rows:
            raise DataUnavailableError(
                f"{symbol} is not in the fafnir security master"
                + (f" as of {on.isoformat()}" if on else ""),
                code=E_PRICE_MISSING,
                remedy="Load it into fafnir first, or price it by hand.",
                symbol=symbol,
                provider=self.name,
            )
        return int(rows[0][0])

    def lookup_security(self, symbol: str, *, on: date | None = None) -> Instrument:
        security_id = self._security_id(symbol, on)
        rows = self._query(
            "SELECT primary_symbol, company_name, asset_type, exchange_code, currency, "  # noqa: S608 -- table names come from the SCHEMA constant; all values are bound
            f"country, cusip, isin, is_actively_trading FROM {SCHEMA['security']} "
            "WHERE security_id = %s",
            (security_id,),
        )
        primary, name, asset_type, exchange, currency, country, cusip, isin, active = rows[0]
        return Instrument(
            instrument_id=0,
            symbol=str(primary),
            instrument_type=_INSTRUMENT_TYPES.get(str(asset_type), InstrumentType.EQUITY),
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

        ``ingestion_run_id`` travels into ``provider_ref`` so a valuation traces
        to the warehouse load that produced it (``PORT-GIPS-J03``).
        """
        security_id = self._security_id(symbol, end)
        rows = self._query(
            "SELECT trade_date, close, source, ingestion_run_id, loaded_at "  # noqa: S608 -- table names come from the SCHEMA constant; all values are bound
            f"FROM {SCHEMA['daily_price']} "
            "WHERE security_id = %s AND trade_date BETWEEN %s AND %s "
            "ORDER BY trade_date",
            (security_id, start.isoformat(), end.isoformat()),
        )
        if not rows:
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

        prices: list[Price] = []
        for trade_date, close, source, run_id, loaded_at in rows:
            prices.append(
                Price(
                    instrument_id=0,
                    price_date=_as_date(trade_date),
                    # str() then from_text(): psycopg returns Decimal for
                    # NUMERIC, but going through the canonical text form means
                    # a driver change cannot quietly introduce a float here.
                    price=from_text(str(close)),
                    source=f"{self.name}:{SCHEMA['daily_price']}",
                    as_of=_as_datetime(loaded_at),
                    valuation_level=1,
                    valuation_basis=ValuationBasis.EXCHANGE_CLOSE,
                    is_estimate=False,
                    provider_ref=(
                        f"security_id={security_id};"
                        f"trade_date={_as_date(trade_date).isoformat()};"
                        f"ingestion_run_id={run_id};source={source}"
                    ),
                )
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
        security_id = self._security_id(symbol, end)
        rows = self._query(
            "SELECT action_type, ex_date, record_date, payment_date, "  # noqa: S608 -- table names come from the SCHEMA constant; all values are bound
            "split_numerator, split_denominator, dividend_amount, corporate_action_id "
            f"FROM {SCHEMA['corporate_action']} "
            "WHERE security_id = %s AND ex_date BETWEEN %s AND %s ORDER BY ex_date",
            (security_id, start.isoformat(), end.isoformat()),
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
        """The treasury curve, through `duk yc`.

        This is the one place `portable` shells out to fafnir's CLI, and it is
        the right call: fafnir's own documentation says ``yc`` is live-only
        "until the economic-series fast-follow adds treasury data to the
        warehouse", so there is no table to query and `duk` **is** the
        documented interface (ADR 0006).

        The series is named specifically in the result because
        ``PORT-GIPS-F05`` requires the *name* of the risk-free rate to be
        disclosed. "The risk-free rate" is not an acceptable disclosure.
        """
        self.require(Capability.RISK_FREE_RATE)
        try:
            completed = subprocess.run(  # noqa: S603 -- fixed argv, no shell
                [self.duk_path, "-S", "live", "yc", "--json"],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
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

        curve = json.loads(completed.stdout)
        rate = _tenor_from_curve(curve, tenor)
        if rate is None:
            raise DataUnavailableError(
                f"the {tenor} tenor is not in the yield curve from `duk yc`",
                code=E_PRICE_MISSING,
                remedy="Choose a tenor the curve carries.",
                tenor=tenor,
                provider=self.name,
            )
        return RiskFreeRate(
            series_name="US Treasury constant maturity yield curve",
            tenor=tenor,
            rate=rate,
            as_of=on,
            source=f"{self.duk_path} -S live yc",
        )

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


#: fafnir's `asset_type` values, mapped to `portable`'s instrument types.
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


def _tenor_from_curve(curve: Any, tenor: str) -> Decimal | None:
    """Pull one tenor out of whatever shape `duk yc --json` returned.

    Tolerant of both a mapping and a list of records, because the exact shape
    is `duk`'s to change and this adapter is the only thing that should have to
    care.
    """
    wanted = tenor.strip().lower()
    if isinstance(curve, dict):
        for key, value in curve.items():
            if str(key).strip().lower() in {wanted, f"month{wanted}", f"year{wanted}"}:
                return from_text(str(value))
        rows = curve.get("rows") or curve.get("data")
        if isinstance(rows, list):
            return _tenor_from_curve(rows, tenor)
    elif isinstance(curve, list):
        for row in curve:
            if not isinstance(row, dict):
                continue
            label = str(row.get("tenor") or row.get("maturity") or "").strip().lower()
            if label == wanted:
                raw = row.get("rate") if "rate" in row else row.get("yield")
                if raw is not None:
                    return from_text(str(raw))
    return None
