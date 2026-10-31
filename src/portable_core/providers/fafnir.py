"""The fafnir adapter. The only module in `portable` that knows fafnir exists.

`portable` reaches fafnir through `duk` and nothing else (ADR 0032, which
completes ADR 0029). There is no database driver, no SQL and no relation name
here: every read is a `duk` command, and every `duk` argument list is built in
this module, so a fafnir change is a one-file change here.
``tests/unit/test_layering.py`` holds both lines -- fafnir confined to this
module, and no SQL in it.

* Which security a ticker named on each day, and that security's master
  record: ``duk -S db th ticker:<T> --json``.
* End-of-day closes, unadjusted: ``duk -S db ph sid:<id> --close --json --exact``.
* A day's high, low and close, to check fills: ``... ph sid:<id> --hlc ...``.
* Splits and cash dividends: ``duk -S db ca sid:<id> --json --exact``.
* Which venues have a calendar, and their spans: ``duk -S db tc --json``; a
  venue's sessions: ``duk -S db tc <venue> --json``.
* The risk-free rate: ``duk -S live yc --json``.

**The ticker is resolved here, the security named to `duk` by its id.** `duk`
resolves a bare ticker to whoever holds it *now*, which for a reused ticker is
another issuer's history under the right-looking name (ADR 0020 R5). So the
adapter reads the ticker's history (`duk th`), decides day by day which
security it named -- the rule is :meth:`FafnirProvider._segments` -- and asks
for each stretch by ``sid:<security_id>``, which `duk` reads as exactly that
security or refuses.

**Exact, end to end.** ``--exact`` has `duk` print the warehouse's stored
``NUMERIC`` values as decimal text, JSON strings that no reader can round, so
no price, ratio or dividend passes through a float on its way here
(`CLAUDE.md` invariant 1). Anything that is not such text is refused, not read.
A `duk` too old for these commands stops the run with its usage error.

**Two capabilities fafnir does not have**, both load-bearing:

1. **No total-return benchmark series.** The warehouse carries security prices,
   not index levels. ``PORT-GIPS-G01`` prohibits price-only benchmarks and
   requires `portable` to *refuse* rather than warn -- so this provider declares
   **no benchmark capability at all**. Synthesising a total return by adding
   dividends to a price series would produce a defensible-looking wrong number
   of exactly the kind this repository exists to prevent.
2. **No treasury curve.** fafnir's own docs say ``duk yc`` reads FMP in every
   mode, the warehouse holding no treasury data. So the risk-free rate comes
   from ``duk -S live yc``.

**Unadjusted prices only.** ``PORT-GIPS-A01`` requires fair value on the
measurement date and adjusted prices double-count splits. The `duk` argument
list has no ``--adj`` branch, so the mistake is not available.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import partial
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
    DailyRange,
    MarketDataProvider,
    RiskFreeRate,
    TradingCalendar,
)

__all__ = ["DUK_COMMANDS", "FafnirProvider", "resolve_dsn"]

#: The `duk` subcommands this adapter runs, and what each answers. A `duk` that
#: lacks one exits 2 with a usage error, which stops the run naming it.
DUK_COMMANDS: Final[dict[str, str]] = {
    "th": "ticker history: which security a ticker named, and when",
    "ph": "price history: raw daily bars, as stored",
    "ca": "corporate actions: splits and cash dividends",
    "tc": "trading calendar: venues, spans and sessions",
    "yc": "the treasury curve, from FMP",
}

#: The venue whose calendar judges a security whose own venue has none: fafnir's
#: ``CALENDAR_FALLBACK_EXCHANGE``, so portable finds the gaps fafnir would.
_CALENDAR_FALLBACK: Final = "NASDAQ"

#: Everything fafnir supplies, all of it through `duk`, so all of it only when
#: `duk` can be run.
_THROUGH_DUK: Final = frozenset(
    {
        Capability.SECURITY_MASTER,
        Capability.EOD_PRICES,
        Capability.DAILY_RANGE,
        Capability.CORPORATE_ACTIONS,
        Capability.DIVIDENDS,
        Capability.TRADING_CALENDAR,
        Capability.RISK_FREE_RATE,
    }
)

#: The `duk ph` flag that prints each set of bar fields.
_FIELD_FLAGS: Final[dict[tuple[str, ...], str]] = {
    ("close",): "--close",
    ("high", "low", "close"): "--hlc",
}

#: How ``duk ph --exact`` prints a stored price: ``format(value, "f")`` of a
#: ``NUMERIC``, so digits and at most one point, never a sign or an exponent.
_EXACT_PRICE: Final = re.compile(r"[0-9]+(\.[0-9]+)?")
#: ``duk ca --exact``'s numbers: the same text, a sign allowed (a stored value is
#: read as stored; whether it is plausible is the corporate-action engine's call).
_EXACT_DECIMAL: Final = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_EXACT_INTEGER: Final = re.compile(r"[0-9]+")

#: What a successful `duk` prints instead of JSON when there is nothing to show:
#: "No data found for X", "No corporate actions for X", "No ticker history for X".
_NOTHING: Final = "No "

#: portable's own override of the warehouse `duk` reads. `duk` otherwise reads
#: its own configuration (``FAFNIR_DSN``, then ``~/.dukrc``), and that is the
#: whole of how the warehouse is found: portable reads no fafnir config file.
_DSN_ENV: Final = "PORTABLE_FAFNIR_DSN"


def resolve_dsn(explicit: str | None = None) -> str | None:
    """The warehouse portable's `duk` should read, when it is not duk's own.

    ``explicit`` (``fafnir_dsn`` in portable's config), else
    ``PORTABLE_FAFNIR_DSN``, else None: `duk` then reads its own configuration.
    The value is a secret -- it carries a password -- so it travels in the
    child's environment only, and is never logged or put in an error
    (bootstrap §6.3).
    """
    if explicit:
        return explicit
    return os.environ.get(_DSN_ENV) or None


class FafnirProvider(MarketDataProvider):
    """Reads the owner's fafnir warehouse, every read through `duk` (ADR 0032).

    A `duk` that is not on ``PATH`` (or at ``duk_path``) makes every capability
    unavailable, and every refusal says that is why.
    """

    name = "fafnir"

    def __init__(self, dsn: str | None = None, *, duk_path: str = "duk") -> None:
        self.dsn = resolve_dsn(dsn)
        self.duk_path = duk_path
        # Read once per provider, per ticker and for the venue list: a run asks
        # about the same ticker for prices, actions and the master in turn.
        self._history: dict[str, list[_Period]] = {}
        self._venues: dict[str, tuple[date, date]] | None = None

    @property
    def capabilities(self) -> frozenset[Capability]:
        """What fafnir can actually supply: everything here, through `duk`.

        Note the absences, and see the module docstring: **no BENCHMARKS** (no
        total-return index series exists in the warehouse, and
        ``PORT-GIPS-G01`` says refuse rather than approximate) and no FX.
        """
        if shutil.which(self.duk_path):
            return _THROUGH_DUK
        return frozenset()

    def unavailable(self, capability: Capability) -> DataUnavailableError | None:
        """A `duk` that is not on PATH is the reason, and the general answer hides it."""
        if capability not in _THROUGH_DUK:
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

    # ── running duk ──────────────────────────────────────────────────────────

    def _run_duk(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        """Run `duk`, against portable's own warehouse override if there is one.

        Output is decoded as UTF-8, not the locale's code page (`CLAUDE.md`
        invariant 6; the owner works on Windows).
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

    def _duk_records(
        self, argv: list[str], *, asking: str, about_one_security: bool = True
    ) -> list[dict[str, Any]]:
        """What a `duk ... --json` call printed, as records, or a refusal.

        Exit 2 is a usage error: this `duk` does not take these arguments, and
        no security will go better. ``Error: ...`` is `duk` failing outright --
        the warehouse, the role, the DSN. Both stop the run. Anything else is
        about the security asked for (``No security found for sid:7.``), and is
        answered as that security's gap when ``about_one_security``.
        """
        completed = self._run_duk(argv)
        said = " ".join((completed.stderr or completed.stdout or "").split())
        if completed.returncode != 0:
            if completed.returncode == 2 or said.startswith("Error:") or not about_one_security:
                raise DataUnavailableError(
                    f"duk failed asking for {asking}: {said}",
                    code=E_PROVIDER_UNAVAILABLE,
                    remedy=(
                        f"Run the same command by hand to see why: `{' '.join(argv)}`. "
                        "portable needs a duk with `th`, `ca` and `tc`, whose `ph` and "
                        "`ca` take --exact and which names a security by `sid:` "
                        "(`duk --help` lists the commands), reading as a fafnir_app role."
                    ),
                    provider=self.name,
                    returncode=completed.returncode,
                )
            raise DataUnavailableError(
                f"duk could not answer for {asking}: {said}",
                code=E_PRICE_MISSING,
                remedy=(
                    "Check the security in fafnir (`duk th`), or load those dates from a "
                    "file with `pt price load --file PATH`."
                ),
                provider=self.name,
            )
        text = completed.stdout.strip()
        if text.startswith(_NOTHING):
            return []  # duk's sentence for an empty answer, not a malformed one
        try:
            parsed = json.loads(text, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            raise self._unreadable(asking, "not JSON") from exc
        if not isinstance(parsed, list) or not all(isinstance(r, dict) for r in parsed):
            raise self._unreadable(asking, "not a list of records")
        return parsed

    def _unreadable(self, asking: str, why: str) -> DataUnavailableError:
        return DataUnavailableError(
            f"duk's answer for {asking} cannot be read: {why}",
            code=E_PROVIDER_UNAVAILABLE,
            remedy=(
                "portable reads the JSON `duk th`, `ca --exact`, `tc` and `ph --exact` "
                "print; check the duk version, and load these dates from a file "
                "meanwhile."
            ),
            provider=self.name,
        )

    # ── which security a ticker named ────────────────────────────────────────

    def _ticker_history(self, symbol: str) -> list[_Period]:
        """Every period of *symbol*'s history, from ``duk th``, read once.

        The ticker is passed as ``ticker:<T>`` so `duk` never reads it as
        anything else. Periods run from ``valid_from`` to ``valid_to``
        inclusive (``None`` = still open).
        """
        key = symbol.strip().upper()
        if key in self._history:
            return self._history[key]
        asking = f"the ticker history of {key}"
        argv = [self.duk_path, "-S", "db", "th", f"ticker:{key}", "--json"]
        periods = [
            _Period.read(record, lambda why: self._unreadable(asking, why))
            for record in self._duk_records(argv, asking=asking, about_one_security=False)
        ]
        self._history[key] = periods
        return periods

    def _segments(self, symbol: str, start: date, end: date) -> list[_Segment]:
        """Which security *symbol* named on each day from *start* to *end*.

        From the ticker's history (``duk th``). A ticker can name one security
        for part of a range and another for the rest -- a rename, a delisting
        followed by reuse -- so a range is answered segment by segment, never by
        resolving the ticker once and applying the answer to every date. Where
        two periods cover the same day, the security whose primary ticker it is
        wins, then the later period: the same rule a single date has always
        used.

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
        periods = [
            p
            for p in self._ticker_history(symbol)
            if p.valid_from <= end and (p.valid_to is None or p.valid_to >= start)
        ]

        # Between two consecutive boundaries the set of covering periods cannot
        # change, so each stretch is decided by its first day.
        boundaries = {start}
        for period in periods:
            if start < period.valid_from <= end:
                boundaries.add(period.valid_from)
            if period.valid_to is not None and start <= period.valid_to < end:
                boundaries.add(period.valid_to + timedelta(days=1))
        ordered = sorted(boundaries)

        segments: list[_Segment] = []
        uncovered: list[str] = []
        for position, first in enumerate(ordered):
            last = (
                ordered[position + 1] - timedelta(days=1)
                if position + 1 < len(ordered)
                else end
            )
            covering = [
                p
                for p in periods
                if p.valid_from <= first and (p.valid_to is None or p.valid_to >= first)
            ]
            if not covering:
                uncovered.append(
                    first.isoformat()
                    if first == last
                    else f"{first.isoformat()}..{last.isoformat()}"
                )
                continue
            security_id = max(covering, key=lambda p: (p.is_primary, p.valid_from)).security_id
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

    def lookup_security(self, symbol: str, *, on: date) -> Instrument:
        """The security *symbol* named on *on*, as fafnir describes it.

        The date is required. Defaulting it to today made every lookup a
        question about the present, asked with no date in sight (`CLAUDE.md`
        invariant 6), and answered for a historical instrument with whoever
        holds its ticker now.
        """
        self.require(Capability.SECURITY_MASTER)
        security_id = self._segments(symbol, on, on)[0].security_id
        master = next(
            p.master for p in self._ticker_history(symbol) if p.security_id == security_id
        )
        asset_type = master.get("asset_type")
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
            symbol=str(master.get("primary_symbol") or symbol.upper()),
            instrument_type=instrument_type,
            name=master.get("company_name"),
            currency=str(master.get("currency") or "USD"),
            exchange=master.get("exchange_code"),
            cusip=master.get("cusip"),
            isin=master.get("isin"),
            country=master.get("country"),
            is_active=bool(master.get("is_actively_trading")),
            source=f"{self.name}:duk th",
            provider_ref=f"security_id={security_id}",
        )

    # ── prices ───────────────────────────────────────────────────────────────

    def eod_prices(self, symbol: str, start: date, end: date) -> list[Price]:
        """Unadjusted end-of-day closes, through ``duk -S db ph sid:<id>``.

        Each day is priced from the security the ticker named **that day**
        (:meth:`_segments`), named to `duk` by its ``security_id``, so a range
        across a rename or a reused ticker is not filled with another issuer's
        prices.

        There is no ``adjusted=True`` parameter and no ``--adj`` in the
        argument list. ``PORT-GIPS-A01`` requires fair value on the measurement
        date, and `CLAUDE.md` requires unadjusted prices with explicit
        corporate-action transactions -- so the adjusted path is not a
        discouraged option here, it is unwritten.

        ``security_id`` travels into ``provider_ref``, so a valuation traces to
        the warehouse security that produced it (``PORT-GIPS-J03``). The
        ingestion run does not: `duk` reads `mart`, which does not carry it.
        """
        self.require(Capability.EOD_PRICES)
        prices: list[Price] = []
        for segment in self._segments(symbol, start, end):
            named = segment.named
            for on, bar in self._bars(named, segment, ("close",)):
                close = bar["close"]
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

    def daily_ranges(self, symbol: str, start: date, end: date) -> list[DailyRange]:
        """Each day's raw high, low and close, through ``duk -S db ph --hlc``.

        The same route as :meth:`eod_prices`, day by day from the security
        the ticker named that day, and as exact. Read when asked and never
        stored: a range is a check on a fill, not a price to value at.
        """
        self.require(Capability.DAILY_RANGE)
        ranges: list[DailyRange] = []
        for segment in self._segments(symbol, start, end):
            named = segment.named
            ranges.extend(
                DailyRange(
                    price_date=on,
                    low=bar["low"],
                    high=bar["high"],
                    close=bar["close"],
                    source=f"{self.name}:duk ph;security_id={segment.security_id};via={named}",
                )
                for on, bar in self._bars(named, segment, ("high", "low", "close"))
            )
        if not ranges:
            raise DataUnavailableError(
                f"no fafnir prices for {symbol} between {start.isoformat()} and "
                f"{end.isoformat()}",
                code=E_PRICE_MISSING,
                remedy="Backfill the range in fafnir.",
                symbol=symbol,
                provider=self.name,
            )
        return ranges

    def _bars(
        self, named: str, segment: _Segment, fields: tuple[str, ...]
    ) -> list[tuple[date, dict[str, Decimal]]]:
        """The raw bars `duk` reports for *named* over *segment*'s days."""
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
            _FIELD_FLAGS[fields],
            "--json",
            "--exact",
        ]
        completed = self._run_duk(argv)
        said = " ".join((completed.stderr or completed.stdout or "").split())
        if completed.returncode == 0:
            return _bar_rows(completed.stdout, named, segment, self.name, fields)
        # Exit 2 is a usage error: this duk does not take these arguments, and
        # no security will go better. "Error: ..." is duk failing outright --
        # the warehouse, the role. Anything else is about this security. Only
        # that is answered per security; the rest stops the run.
        if completed.returncode == 2 or said.startswith("Error:"):
            raise DataUnavailableError(
                f"duk failed asking for {named}: {said}",
                code=E_PROVIDER_UNAVAILABLE,
                remedy=(
                    "Run the same command by hand to see why: "
                    f"`{' '.join(argv)}`. portable needs a duk whose `ph` takes "
                    f"{_FIELD_FLAGS[fields]}, --json and --exact, and names a security "
                    "by `sid:`, reading as a fafnir_app role."
                ),
                provider=self.name,
                returncode=completed.returncode,
            )
        raise DataUnavailableError(
            f"duk could not price {named} ({segment.describe()}): {said}",
            code=E_PRICE_MISSING,
            remedy=(
                "Check the security in fafnir (`duk th`), or load those dates from a "
                "file with `pt price load --file PATH`."
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

        Through ``duk ca sid:<id> --exact``, segment by segment like prices, the
        ratios and amounts as the warehouse's decimal text. A warehouse without
        fafnir migration 0033 has no corporate actions to read, and `duk`'s
        refusal says so. Spinoffs, mergers, symbol changes, and delistings are
        **not** fafnir corporate actions. `pt ca sync` says so rather than
        implying the list is complete, because a silent omission here is a
        missing basis adjustment.
        """
        self.require(Capability.CORPORATE_ACTIONS)
        found: list[CorporateActionRecord] = []
        for segment in self._segments(symbol, start, end):
            asking = f"the corporate actions of {segment.named} ({segment.describe()})"
            argv = [
                self.duk_path,
                "-S",
                "db",
                "ca",
                segment.named,
                "--start-date",
                segment.start.isoformat(),
                "--end-date",
                segment.end.isoformat(),
                "--json",
                "--exact",
            ]
            for record in self._duk_records(argv, asking=asking):
                found.append(
                    _action(
                        record,
                        symbol.upper(),
                        segment,
                        lambda why, asking=asking: self._unreadable(asking, why),
                    )
                )
        return found

    # ── trading calendar ─────────────────────────────────────────────────────

    def _calendars(self) -> dict[str, tuple[date, date]]:
        """Every venue with a calendar and the span it holds, from ``duk tc``."""
        if self._venues is not None:
            return self._venues
        asking = "the trading calendars"
        argv = [self.duk_path, "-S", "db", "tc", "--json"]
        venues: dict[str, tuple[date, date]] = {}
        for record in self._duk_records(argv, asking=asking, about_one_security=False):
            code = record.get("exchange_code")
            first, last = record.get("first_date"), record.get("last_date")
            if not isinstance(code, str) or not code:
                raise self._unreadable(asking, f"a venue with no code: {record!r}")
            venues[code.strip().upper()] = (
                _iso_date(first, lambda why: self._unreadable(asking, why)),
                _iso_date(last, lambda why: self._unreadable(asking, why)),
            )
        self._venues = venues
        return venues

    def trading_calendar(self, exchange: str | None, start: date, end: date) -> TradingCalendar:
        """*exchange*'s sessions from *start* to *end*, or NASDAQ's when it has none.

        fafnir seeds open days only: a weekend or a recurring holiday has no
        row, and a closure no rule predicts is a row with ``is_open`` false
        (fafnir migration 0022). So no row means closed inside the calendar's
        span and unknown outside it, and the span -- everything the venue's
        calendar holds, not just the range asked for -- comes from ``duk tc``'s
        venue list. A venue with no calendar is judged by NASDAQ's, as fafnir's
        own ingestion judges it; `duk` itself never falls back.

        Raises:
            DataUnavailableError: neither the venue nor NASDAQ has a calendar.
        """
        self.require(Capability.TRADING_CALENDAR)
        venues = self._calendars()
        asked = exchange.strip().upper() if exchange else None
        for code in dict.fromkeys(c for c in (asked, _CALENDAR_FALLBACK) if c):
            span = venues.get(code)
            if span is None:
                continue
            asking = f"{code}'s sessions"
            argv = [
                self.duk_path,
                "-S",
                "db",
                "tc",
                code,
                "--start-date",
                start.isoformat(),
                "--end-date",
                end.isoformat(),
                "--json",
            ]
            refuse = partial(self._unreadable, asking)
            open_days: set[date] = set()
            for record in self._duk_records(argv, asking=asking, about_one_security=False):
                day = _iso_date(record.get("date"), refuse)
                if not isinstance(record.get("is_open"), bool):
                    raise refuse(f"{day.isoformat()} has no is_open")
                if record["is_open"]:
                    open_days.add(day)
            return TradingCalendar(
                exchange=code,
                open_days=frozenset(open_days),
                first=span[0],
                last=span[1],
                source=f"{self.name}:duk tc",
            )
        named = asked or "a security with no venue"
        also = "" if asked == _CALENDAR_FALLBACK else f", nor for {_CALENDAR_FALLBACK}"
        raise DataUnavailableError(
            f"the fafnir warehouse has no trading calendar for {named}{also}",
            code=E_PROVIDER_UNAVAILABLE,
            remedy=(
                "`duk tc` lists the venues the warehouse has a calendar for; they are "
                "filled by `fafnir db seed` on the warehouse host. Without a calendar, "
                "a missing price cannot be told from a holiday."
            ),
            provider=self.name,
            exchange=asked,
        )

    # ── risk-free rate, via duk ──────────────────────────────────────────────

    def risk_free_rate(self, on: date, *, tenor: str = "3M") -> RiskFreeRate:
        """The treasury par yield for *tenor*, observed on *on*, through `duk yc`.

        fafnir's own documentation says ``yc`` reads FMP in every mode, the
        warehouse holding no treasury data, so this, unlike prices, reads FMP
        through `duk` rather than the warehouse (ADR 0006).

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


@dataclass(frozen=True, slots=True)
class _Segment:
    """A stretch of days on which a ticker named one security."""

    security_id: int
    start: date
    end: date

    @property
    def named(self) -> str:
        """How the security is named to `duk`: by its id, never by ticker."""
        return f"sid:{self.security_id}"

    def describe(self) -> str:
        if self.start == self.end:
            return f"on {self.start.isoformat()}"
        return f"from {self.start.isoformat()} to {self.end.isoformat()}"


#: The master-record fields of a `duk th` row, kept with its period.
_MASTER_FIELDS: Final = (
    "primary_symbol",
    "company_name",
    "asset_type",
    "exchange_code",
    "currency",
    "country",
    "cik",
    "isin",
    "cusip",
    "is_actively_trading",
)


@dataclass(frozen=True, slots=True)
class _Period:
    """One period of a ticker's history, and the security it named."""

    security_id: int
    valid_from: date
    valid_to: date | None
    is_primary: bool
    master: dict[str, Any]

    @classmethod
    def read(cls, record: dict[str, Any], refuse: Any) -> _Period:
        """A `duk th --json` record, checked field by field or refused whole."""
        missing = [
            k
            for k in ("security_id", "valid_from", "valid_to", "is_primary")
            if k not in record
        ]
        missing += [k for k in _MASTER_FIELDS if k not in record]
        if missing:
            raise refuse(f"a period with no {', '.join(missing)}: {record!r}")
        security_id = record["security_id"]
        if (
            isinstance(security_id, bool)
            or not isinstance(security_id, int)
            or security_id <= 0
        ):
            raise refuse(f"security_id {security_id!r} is not a security's id")
        if not isinstance(record["is_primary"], bool):
            raise refuse(f"is_primary {record['is_primary']!r} is not true or false")
        valid_to = record["valid_to"]
        return cls(
            security_id=security_id,
            valid_from=_iso_date(record["valid_from"], refuse),
            valid_to=None if valid_to is None else _iso_date(valid_to, refuse),
            is_primary=record["is_primary"],
            master={k: record[k] for k in _MASTER_FIELDS},
        )


def _iso_date(value: Any, refuse: Any) -> date:
    """``YYYY-MM-DD`` text as a date, or a refusal: `duk` prints no other form."""
    try:
        parsed = date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.isoformat() != value:
        raise refuse(f"unexpected date {value!r}")
    return parsed


def _exact(
    value: Any, field: str, refuse: Any, *, pattern: re.Pattern[str] = _EXACT_DECIMAL
) -> Decimal | None:
    """An ``--exact`` number: None, or decimal text read straight into a Decimal."""
    if value is None:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise refuse(f"{field} {value!r} is not exact decimal text")
    return from_text(value)


def _action(
    record: dict[str, Any], symbol: str, segment: _Segment, refuse: Any
) -> CorporateActionRecord:
    """A `duk ca --json --exact` record as a corporate action, or a refusal."""
    kind = record.get("action_type")
    if not isinstance(kind, str) or not kind:
        raise refuse(f"an action with no type: {record!r}")
    ex_date = _iso_date(record.get("ex_date"), refuse)
    if not segment.start <= ex_date <= segment.end:
        raise refuse(f"{ex_date.isoformat()} is outside the dates asked for")
    action_id = _exact(
        record.get("corporate_action_id"), "corporate_action_id", refuse, pattern=_EXACT_INTEGER
    )
    record_date, pay_date = record.get("record_date"), record.get("payment_date")
    return CorporateActionRecord(
        symbol=symbol,
        action_type="cash_dividend" if kind == "dividend" else kind,
        ex_date=ex_date,
        record_date=_iso_date(record_date, refuse) if record_date is not None else None,
        pay_date=_iso_date(pay_date, refuse) if pay_date is not None else None,
        split_numerator=_exact(record.get("split_numerator"), "split_numerator", refuse),
        split_denominator=_exact(record.get("split_denominator"), "split_denominator", refuse),
        cash_amount=_exact(record.get("dividend_amount"), "dividend_amount", refuse),
        provider_ref=f"corporate_action_id={action_id}",
    )


#: fafnir's `asset_type` values, mapped to `portable`'s instrument types.
#: fafnir's fourth value, 'other', is deliberately absent: it is refused.
_INSTRUMENT_TYPES: Final[dict[str, InstrumentType]] = {
    "equity": InstrumentType.EQUITY,
    "etf": InstrumentType.ETF,
    "fund": InstrumentType.MUTUAL_FUND,
}


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


def _bar_rows(
    stdout: str, named: str, segment: _Segment, provider: str, fields: tuple[str, ...]
) -> list[tuple[date, dict[str, Decimal]]]:
    """`duk ph --json --exact` output as the warehouse's own decimals, or a refusal.

    Every price arrives as the text of the stored ``NUMERIC``, a JSON string,
    and is read straight into a ``Decimal``: nothing is rounded, because
    nothing came through a float. Anything else is refused, not interpreted,
    because every price after it would be suspect too: a JSON number (a `duk`
    not printing exactly), text that is not a plain positive decimal, a record
    of the wrong shape, a date out of range or order, a low above its high.
    """
    text = stdout.strip()
    if text.startswith("No data found"):
        # duk's sentence for a range that holds no bars: an empty answer, not
        # a malformed one. Any other sentence is refused below as not JSON.
        return []
    shape = "{" + ", ".join(("date", *fields)) + "}"

    def refuse(why: str) -> DataUnavailableError:
        return DataUnavailableError(
            f"duk's prices for {named} ({segment.describe()}) cannot be read exactly: {why}",
            code=E_PROVIDER_UNAVAILABLE,
            remedy=(
                f"portable reads `duk ph {_FIELD_FLAGS[fields]} --json --exact` "
                f"output: a list of {shape} records whose prices are decimal text. "
                "Check the duk version, and load these dates from a file meanwhile."
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

    rows: list[tuple[date, dict[str, Decimal]]] = []
    for record in parsed:
        if not isinstance(record, dict) or set(record) != {"date", *fields}:
            raise refuse(f"unexpected record {record!r}")
        stated = record["date"]
        try:
            on = date.fromisoformat(stated) if isinstance(stated, str) else None
        except ValueError:
            on = None
        if on is None or on.isoformat() != stated:
            raise refuse(f"unexpected date {stated!r}")
        bar: dict[str, Decimal] = {}
        for field in fields:
            value = record[field]
            if not isinstance(value, str) or not _EXACT_PRICE.fullmatch(value):
                raise refuse(f"{on.isoformat()} has {field} {value!r}, not exact decimal text")
            price = from_text(value)
            if price <= 0:
                raise refuse(f"{on.isoformat()} has {field} {value}")
            bar[field] = price
        if "low" in bar and bar["low"] > bar["high"]:
            raise refuse(f"{on.isoformat()} has a low above its high")
        if not segment.start <= on <= segment.end:
            raise refuse(f"{on.isoformat()} is outside the dates asked for")
        if rows and on <= rows[-1][0]:
            raise refuse(f"{on.isoformat()} is out of order or repeated")
        rows.append((on, bar))
    return rows
