"""The fafnir adapter. The only module in `portable` that knows fafnir exists.

ADR 0029 records how `portable` reaches fafnir, superseding ADR 0006's price
path; `CLAUDE.md` forbids coupling to fafnir internals anywhere else, and
``tests/unit/test_layering.py`` enforces it. Every relation name is in
:data:`SCHEMA` at the top of this module and every `duk` argument list is
built here, so a fafnir change is a one-file change here.

**Two routes, one warehouse.**

* **Prices come from `duk`**, the owner's tool for sourcing them:
  ``duk -S db ph <identifier> --close --json --precision 6``. Raw closes, from
  the warehouse, never the live vendor and never ``--adj``.
* **Identity and corporate actions are read from `mart` directly**, over the
  same DSN. Which security a ticker named on each day is a question only
  ``mart.v_symbol_lookup`` answers -- `duk` resolves a ticker to whoever holds
  it *now* -- so the adapter resolves it, and names the security to `duk` by
  identifier. `portable` reads `mart` and `ref` only, never `core`: a person's
  tunnel role is a member of ``fafnir_app``, which cannot see `core` (fafnir
  ADR 0008).

`duk` is run with ``FAFNIR_DSN`` set to the DSN this adapter resolved, so the
two routes cannot read two different warehouses.

**`duk` prints floats.** Its DataFrame is floating point and its JSON rounds
to two places unless told otherwise. ``--precision 6`` matches the warehouse's
``NUMERIC(20, 6)``; the text is parsed as ``Decimal`` and rounded back to those
six places, which recovers the stored value exactly for any price below 10^9.
Anything that does not look like a six-place price that came through a float
is refused, not rounded into one (`CLAUDE.md` invariant 1).

**Two capabilities fafnir does not have**, both load-bearing:

1. **No total-return benchmark series.** The warehouse carries security prices,
   not index levels. ``PORT-GIPS-G01`` prohibits price-only benchmarks and
   requires `portable` to *refuse* rather than warn -- so this provider declares
   **no benchmark capability at all**. Synthesising a total return by adding
   dividends to a price series would produce a defensible-looking wrong number
   of exactly the kind this repository exists to prevent.
2. **No treasury curve.** fafnir's own docs say ``duk yc`` is live-only "until
   the economic-series fast-follow adds treasury data to the warehouse". So the
   risk-free rate comes from ``duk -S live yc``.

**Unadjusted prices only.** ``PORT-GIPS-A01`` requires fair value on the
measurement date and adjusted prices double-count splits. The `duk` argument
list has no ``--adj`` branch, so the mistake is not available.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
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

#: fafnir's relations, in one place: `mart` and `ref` only (fafnir ADR 0008).
#: If fafnir migrates, this mapping is what changes -- and nothing else in
#: `portable` does. Prices are not here: they come through `duk` (ADR 0029).
SCHEMA: Final[dict[str, str]] = {
    # core.security, with primary_symbol exposed as `symbol`.
    "security": "mart.v_security_profile",
    # A passthrough of core.symbol_xref: the ticker history, not a resolver.
    "symbol_xref": "mart.v_symbol_lookup",
    # fafnir migration 0033: a warehouse not yet migrated to it refuses by name.
    "corporate_action": "mart.v_corporate_action",
    "trading_calendar": "ref.trading_calendar",
}

#: The scale of fafnir's raw prices (``core.daily_price.close NUMERIC(20, 6)``),
#: and so the precision `duk` is asked to print them at.
_PRICE_PLACES: Final = 6
_PRICE_QUANTUM: Final = Decimal(1).scaleb(-_PRICE_PLACES)
#: Below this a six-place price survives a float with an error under 10^-7, so
#: rounding `duk`'s text to six places gives back exactly what is stored.
_FLOAT_EXACT_BELOW: Final = Decimal(10) ** 9
_FLOAT_NOISE: Final = Decimal("1e-7")

#: SQLSTATEs that mean "this role cannot read that relation": undefined_table,
#: insufficient_privilege. A warehouse without the drafted view, or a query
#: that strayed into `core`, says one of these.
_UNREADABLE: Final = frozenset({"42P01", "42501"})

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
    """Reads the owner's fafnir warehouse: prices through `duk`, the rest from `mart`.

    ``psycopg`` is an optional dependency (``pip install portable[fafnir]``).
    Its absence makes this provider unavailable with a clear message rather
    than an ``ImportError`` at CLI start. So does a `duk` that is not on
    ``PATH``, for the two things that come through it.
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
        ``EOD_PRICES`` and ``RISK_FREE_RATE`` are present only when `duk` is on
        ``PATH``, because that is where they come from (ADR 0029).
        """
        found = {
            Capability.SECURITY_MASTER,
            Capability.CORPORATE_ACTIONS,
            Capability.DIVIDENDS,
        }
        if shutil.which(self.duk_path):
            found.update({Capability.EOD_PRICES, Capability.RISK_FREE_RATE})
        return frozenset(found)

    def unavailable(self, capability: Capability) -> DataUnavailableError | None:
        """A `duk` that is not on PATH is the reason, and the general answer hides it."""
        if capability not in {Capability.EOD_PRICES, Capability.RISK_FREE_RATE}:
            return None
        return DataUnavailableError(
            f"fafnir supplies {capability} through duk, and `{self.duk_path}` is not on PATH",
            code=E_PROVIDER_UNAVAILABLE,
            remedy=(
                "Install fafnir's duk where portable can run it, or name it with "
                "`duk_path` in ~/.portablerc. Prices can also be loaded from a file "
                "with `pt price load --file PATH`."
            ),
            provider=self.name,
            capability=str(capability),
            duk_path=self.duk_path,
        )

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
                    "[database].dsn in ~/.dukrc. Point it at a role in fafnir_app "
                    "(fafnir ADR 0008)."
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
                    "Check the warehouse is running, the SSH tunnel to it is up, and "
                    "the DSN is right. `--offline` works from prices already cached "
                    "in the .port file."
                ),
                provider=self.name,
            ) from exc
        return self._connection

    def _query(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return list(cursor.fetchall())
        except Exception as exc:
            if getattr(exc, "sqlstate", None) not in _UNREADABLE:
                raise
            # psycopg leaves the transaction aborted after an error.
            connection.rollback()
            relation = next((name for name in SCHEMA.values() if name in sql), "a relation")
            raise DataUnavailableError(
                f"the fafnir warehouse has no {relation} this role can read",
                code=E_PROVIDER_UNAVAILABLE,
                remedy=(
                    "mart.v_corporate_action arrives with fafnir migration 0033 "
                    "(ADR 0029): run `fafnir db migrate` on the warehouse host after "
                    "deploying a fafnir that carries it. Until then, supply actions "
                    "with `--source file`."
                    if relation == SCHEMA["corporate_action"]
                    else "portable reads fafnir's mart and ref schemas only. Check the "
                    "warehouse is at fafnir migration 0020 or later and the role is a "
                    "member of fafnir_app."
                ),
                provider=self.name,
                relation=relation,
            ) from exc

    def _run_duk(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        """Run `duk` against the warehouse this adapter reads, or say why not.

        ``FAFNIR_DSN`` is what `duk` reads first, so setting it to the DSN
        resolved here keeps a ``PORTABLE_FAFNIR_DSN`` -- which `duk` has never
        heard of -- from sending the price read to a different warehouse from
        the identity read. Output is decoded as UTF-8, not the locale's code
        page (`CLAUDE.md` invariant 6).
        """
        env = dict(os.environ)
        if self.dsn:
            env["FAFNIR_DSN"] = self.dsn
        try:
            return subprocess.run(  # noqa: S603 -- fixed argv, no shell
                argv,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=_DUK_TIMEOUT_SECONDS,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DataUnavailableError(
                f"`{' '.join(argv[:4])}` could not be run: {exc}",
                code=E_PROVIDER_UNAVAILABLE,
                remedy=f"Check that `{self.duk_path} --version` works from this shell.",
                provider=self.name,
            ) from exc

    # ── security master ──────────────────────────────────────────────────────

    def _segments(self, symbol: str, start: date, end: date) -> list[_Segment]:
        """Which security *symbol* named on each day from *start* to *end*.

        Through ``mart.v_symbol_lookup``, whose periods run from ``valid_from``
        to ``valid_to`` inclusive (``NULL`` = still open). A ticker can name one
        security for part of a range and another for the rest -- a rename, a
        delisting followed by reuse -- so a range is answered segment by
        segment, never by resolving the ticker once and applying the answer to
        every date. Where two periods cover the same day, the security whose
        primary ticker it is wins, then the later period: the same rule a single
        date has always used.

        There is no fallback to the security's primary symbol. fafnir's own
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
            "SELECT symbol, company_name, asset_type, exchange_code, currency, "  # noqa: S608 -- table names come from the SCHEMA constant; all values are bound
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

    def _identifier(self, segment: _Segment, symbol: str) -> str:
        """How to name *segment*'s security to `duk`: never by ticker.

        `duk` resolves a ticker to whoever holds it *now* -- the open period
        first -- which for a reused ticker is somebody else's price history
        under the right-looking name (ADR 0020 R5). An identifier it resolves
        to one security and reads by that security's id, or refuses. ISIN,
        then CUSIP, then CIK: a CIK names an issuer, which may list several
        securities, and `duk` refuses rather than picks when it does.

        Raises:
            DataUnavailableError: ``PT-E-PRICE-MISSING`` when fafnir holds none
                of the three for the security.
        """
        rows = self._query(
            f"SELECT isin, cusip, cik FROM {SCHEMA['security']} WHERE security_id = %s",  # noqa: S608 -- table names come from the SCHEMA constant; all values are bound
            (segment.security_id,),
        )
        isin, cusip, cik = rows[0] if rows else (None, None, None)
        for scheme, value in (("isin", isin), ("cusip", cusip), ("cik", cik)):
            text = str(value or "").strip()
            if text:
                return f"{scheme}:{text}"
        raise DataUnavailableError(
            f"fafnir holds no ISIN, CUSIP or CIK for the security {symbol} named "
            f"{segment.describe()}, and duk can be asked for it only by ticker -- which "
            "names whoever holds the ticker now",
            code=E_PRICE_MISSING,
            remedy=(
                "Load those dates from a file with `pt price load --file PATH`, or add "
                "the identifier to the security in fafnir."
            ),
            symbol=symbol,
            provider=self.name,
            security_id=segment.security_id,
        )

    # ── prices ───────────────────────────────────────────────────────────────

    def eod_prices(self, symbol: str, start: date, end: date) -> list[Price]:
        """Unadjusted end-of-day closes, through ``duk -S db ph`` (ADR 0029).

        Each day is priced from the security the ticker named **that day**
        (:meth:`_segments`), and that security is named to `duk` by identifier
        (:meth:`_identifier`), so a range across a rename or a reused ticker is
        not filled with another issuer's prices.

        There is no ``adjusted=True`` parameter and no ``--adj`` in the
        argument list. ``PORT-GIPS-A01`` requires fair value on the measurement
        date, and `CLAUDE.md` requires unadjusted prices with explicit
        corporate-action transactions -- so the adjusted path is not a
        discouraged option here, it is unwritten.

        ``security_id`` and the identifier `duk` was given travel into
        ``provider_ref``, so a valuation traces to the warehouse security that
        produced it (``PORT-GIPS-J03``). The ingestion run does not: `mart`
        does not carry it.
        """
        self.require(Capability.EOD_PRICES)
        prices: list[Price] = []
        for segment in self._segments(symbol, start, end):
            named = self._identifier(segment, symbol)
            for on, close in self._closes(named, segment):
                prices.append(
                    Price(
                        instrument_id=0,
                        price_date=on,
                        price=close,
                        source=f"{self.name}:duk ph",
                        # The close is what it was on its date. When it was
                        # loaded is not visible through `mart`, and the clock
                        # is not an input (`CLAUDE.md` invariant 6).
                        as_of=datetime.combine(on, time.min, tzinfo=UTC),
                        valuation_level=1,
                        valuation_basis=ValuationBasis.EXCHANGE_CLOSE,
                        is_estimate=False,
                        provider_ref=(
                            f"security_id={segment.security_id};"
                            f"trade_date={on.isoformat()};via={named}"
                        ),
                    )
                )
        if not prices:
            raise DataUnavailableError(
                f"no fafnir prices for {symbol} between {start.isoformat()} and "
                f"{end.isoformat()}",
                code=E_PRICE_MISSING,
                remedy=(
                    "Backfill the range in fafnir, or load the prices from a file with "
                    "`pt price load --file PATH`."
                ),
                symbol=symbol,
                provider=self.name,
            )
        return prices

    def _closes(self, named: str, segment: _Segment) -> list[tuple[date, Decimal]]:
        """The raw closes `duk` reports for *named* over *segment*'s days."""
        argv = [
            self.duk_path,
            "-S",
            "db",
            "ph",
            named,
            "--start-date",
            segment.start.isoformat(),
            "--end-date",
            segment.end.isoformat(),
            "--close",
            "--json",
            "--precision",
            str(_PRICE_PLACES),
        ]
        completed = self._run_duk(argv)
        said = " ".join((completed.stderr or completed.stdout or "").split())
        if completed.returncode == 0:
            return _close_rows(completed.stdout, named, segment, self.name)
        # Exit 2 is a usage error: this duk does not take these arguments, and
        # no security will go better. "Error: ..." is duk failing outright --
        # the warehouse, the role. Anything else is about this security: not
        # found, or an identifier that matches several. Only that is answered
        # per security; the rest stops the run.
        if completed.returncode == 2 or said.startswith("Error:"):
            raise DataUnavailableError(
                f"duk failed asking for {named}: {said}",
                code=E_PROVIDER_UNAVAILABLE,
                remedy=(
                    "Run the same command by hand to see why: "
                    f"`{' '.join(argv)}`. portable needs a duk whose `ph` takes "
                    "--close, --json and --precision, reading as a fafnir_app role."
                ),
                provider=self.name,
                returncode=completed.returncode,
            )
        raise DataUnavailableError(
            f"duk could not price {named} ({segment.describe()}): {said}",
            code=E_PRICE_MISSING,
            remedy=(
                "Check the identifier in fafnir, or load those dates from a file with "
                "`pt price load --file PATH`."
            ),
            provider=self.name,
            security_id=segment.security_id,
            identifier=named,
        )

    # ── corporate actions ────────────────────────────────────────────────────

    def corporate_actions(
        self, symbol: str, start: date, end: date
    ) -> list[CorporateActionRecord]:
        """Splits and cash dividends -- which is what fafnir carries.

        Read from ``mart.v_corporate_action``, fafnir migration 0033; a
        warehouse not yet migrated to it is refused, naming it (ADR 0029).
        Spinoffs, mergers, symbol changes, and delistings are **not** fafnir
        corporate actions. `pt ca sync` says so rather than
        implying the list is complete, because a silent omission here is a
        missing basis adjustment.
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

        fafnir's own documentation says ``yc`` is live-only "until the
        economic-series fast-follow adds treasury data to the warehouse", so
        this, unlike prices, reads FMP through `duk` rather than the warehouse
        (ADR 0006).

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
        completed = self._run_duk(argv)
        if completed.returncode != 0:
            said = " ".join((completed.stderr or completed.stdout or "").split())
            raise DataUnavailableError(
                f"`{self.duk_path} yc` failed: {said}",
                code=E_PROVIDER_UNAVAILABLE,
                remedy=(
                    "duk's yield curve is live-only and needs an FMP API key "
                    "(FMP_API_KEY). Supply the rate by hand if the curve is "
                    "unavailable -- portable will not substitute a guess."
                ),
                provider=self.name,
            )

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

    def describe(self) -> str:
        if self.start == self.end:
            return f"on {self.start.isoformat()}"
        return f"from {self.start.isoformat()} to {self.end.isoformat()}"


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

#: Long enough for a multi-year price history over an SSH tunnel; a `duk` that
#: takes longer than this is stuck, not slow.
_DUK_TIMEOUT_SECONDS: Final = 300

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


def _close_rows(
    stdout: str, named: str, segment: _Segment, provider: str
) -> list[tuple[date, Decimal]]:
    """`duk ph --close --json` output as exact six-place closes, or a refusal.

    `duk` holds prices as floats and prints ten decimal places of them, so a
    stored ``189.120000`` can print as ``189.1200000003``. Below 10^9 that
    noise is under 10^-7, and rounding to the warehouse's six places gives back
    exactly what is stored -- checked across every magnitude when this was
    written. A value that is not within 10^-7 of a six-place number did not
    come from ``NUMERIC(20, 6)`` through a float, and is refused rather than
    rounded: so is anything else unexpected, because every price after it
    would be suspect too.
    """
    text = stdout.strip()
    if text.startswith("No data found"):
        # duk's sentence for a range that holds no bars: an empty answer, not
        # a malformed one. Any other sentence is refused below as not JSON.
        return []

    def refuse(why: str) -> DataUnavailableError:
        return DataUnavailableError(
            f"duk's prices for {named} ({segment.describe()}) cannot be read exactly: {why}",
            code=E_PROVIDER_UNAVAILABLE,
            remedy=(
                "portable reads `duk ph --close --json --precision 6` output: a list of "
                "{date, close} records. Check the duk version, and load these dates "
                "from a file meanwhile."
            ),
            provider=provider,
            identifier=named,
        )

    try:
        parsed = json.loads(text, parse_float=Decimal, parse_int=Decimal)
    except json.JSONDecodeError as exc:
        raise refuse("not JSON") from exc
    if not isinstance(parsed, list):
        raise refuse("not a list of records")

    rows: list[tuple[date, Decimal]] = []
    for record in parsed:
        if not isinstance(record, dict) or set(record) != {"date", "close"}:
            raise refuse(f"unexpected record {record!r}")
        try:
            on = date.fromisoformat(str(record["date"])[:10])
        except ValueError as exc:
            raise refuse(f"unexpected date {record['date']!r}") from exc
        close = record["close"]
        if not isinstance(close, Decimal) or not close.is_finite() or close <= 0:
            raise refuse(f"{on.isoformat()} has close {close!r}")
        if close >= _FLOAT_EXACT_BELOW:
            raise refuse(f"{on.isoformat()} closed at {close}, too large to survive a float")
        try:
            exact = close.quantize(_PRICE_QUANTUM)
        except InvalidOperation as exc:
            raise refuse(f"{on.isoformat()} has close {close}") from exc
        if abs(close - exact) >= _FLOAT_NOISE:
            raise refuse(f"{on.isoformat()} closed at {close}, which is not a six-place price")
        if not segment.start <= on <= segment.end:
            raise refuse(f"{on.isoformat()} is outside the dates asked for")
        if rows and on <= rows[-1][0]:
            raise refuse(f"{on.isoformat()} is out of order or repeated")
        rows.append((on, exact))
    return rows
