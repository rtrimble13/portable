"""The file provider: local CSV and JSON with a documented schema.

**A first-class citizen, not a test double** (bootstrap §6.4). It is how
benchmarks get into a portfolio at all -- see the note on ``BENCHMARK`` below --
how the examples run with no warehouse, and how anybody without a Postgres
instance uses `portable` fully.

The schemas are in `docs/market-data.md` and are validated here rather than
assumed: a price file with a missing column should fail on the file, naming the
line, not four commands later on a valuation.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from portable_core.decimals import from_text
from portable_core.domain.enums import InstrumentType, ValuationBasis
from portable_core.domain.models import Instrument, Price
from portable_core.errors import DataUnavailableError, UsageError
from portable_core.errors.kinds import E_PRICE_MISSING, E_USAGE
from portable_core.providers.base import (
    BenchmarkSeries,
    Capability,
    CorporateActionRecord,
    MarketDataProvider,
)

__all__ = ["FileProvider"]

_PRICE_COLUMNS = {"symbol", "date", "price"}
_ACTION_COLUMNS = {"symbol", "action_type", "ex_date"}


class FileProvider(MarketDataProvider):
    """Reads prices, corporate actions, and benchmarks from local files.

    Args:
        price_file: CSV or JSON. Required columns ``symbol``, ``date``,
            ``price``; optional ``source``, ``valuation_level``,
            ``valuation_basis``, ``is_estimate``, ``currency``.
        action_file: CSV or JSON. Required ``symbol``, ``action_type``,
            ``ex_date``.
        benchmark_file: JSON only. Each series **must** declare
            ``return_type``; see :meth:`benchmark_series`.
    """

    name = "file"

    def __init__(
        self,
        *,
        price_file: Path | str | None = None,
        action_file: Path | str | None = None,
        benchmark_file: Path | str | None = None,
    ) -> None:
        self.price_file = Path(price_file) if price_file else None
        self.action_file = Path(action_file) if action_file else None
        self.benchmark_file = Path(benchmark_file) if benchmark_file else None
        self._prices: dict[str, list[Price]] | None = None

    @property
    def capabilities(self) -> frozenset[Capability]:
        """Only what the caller actually supplied a file for.

        Declaring a capability with no file behind it would produce an empty
        result that reads as "no prices exist" rather than "you did not tell me
        where they are".
        """
        found: set[Capability] = set()
        if self.price_file is not None:
            found.add(Capability.EOD_PRICES)
        if self.action_file is not None:
            found.update({Capability.CORPORATE_ACTIONS, Capability.DIVIDENDS})
        if self.benchmark_file is not None:
            found.add(Capability.BENCHMARKS)
        return frozenset(found)

    # ── loading ──────────────────────────────────────────────────────────────

    @staticmethod
    def _rows(path: Path, required: set[str]) -> Iterator[dict[str, Any]]:
        if not path.is_file():
            raise UsageError(
                f"file not found: {path}",
                code=E_USAGE,
                remedy="Check the path passed to --price-file / --action-file.",
                path=str(path),
            )
        text = path.read_text(encoding="utf-8")

        if path.suffix.lower() == ".json":
            loaded = json.loads(text)
            rows = loaded if isinstance(loaded, list) else loaded.get("rows", [])
        else:
            rows = list(csv.DictReader(text.splitlines()))

        for line, row in enumerate(rows, start=2):
            missing = required - set(row)
            if missing:
                raise UsageError(
                    f"{path}:{line}: missing column(s) {', '.join(sorted(missing))}",
                    code=E_USAGE,
                    remedy=(
                        "See docs/market-data.md for the file schema. Required "
                        f"columns: {', '.join(sorted(required))}."
                    ),
                    path=str(path),
                    line=line,
                )
            yield row

    def _load_prices(self) -> dict[str, list[Price]]:
        if self._prices is not None:
            return self._prices
        if self.price_file is None:
            self._prices = {}
            return self._prices

        by_symbol: dict[str, list[Price]] = {}
        for row in self._rows(self.price_file, _PRICE_COLUMNS):
            symbol = str(row["symbol"]).strip().upper()
            try:
                price_value = from_text(str(row["price"]).strip())
                price_date = date.fromisoformat(str(row["date"]).strip())
            except ValueError as exc:
                raise UsageError(
                    f"{self.price_file}: {exc} in row for {symbol}",
                    code=E_USAGE,
                    remedy="Dates are YYYY-MM-DD; prices are plain decimals.",
                    symbol=symbol,
                ) from exc

            by_symbol.setdefault(symbol, []).append(
                Price(
                    # Resolved by the caller, which owns the instrument master.
                    instrument_id=0,
                    price_date=price_date,
                    price=price_value,
                    source=f"file:{self.price_file.name}",
                    as_of=datetime.combine(price_date, datetime.min.time(), tzinfo=UTC),
                    valuation_level=int(row.get("valuation_level") or 1),
                    valuation_basis=ValuationBasis(
                        row.get("valuation_basis") or "exchange_close"
                    ),
                    is_estimate=str(row.get("is_estimate") or "0").lower()
                    in {"1", "true", "yes"},
                    currency=str(row.get("currency") or "USD"),
                )
            )
        for series in by_symbol.values():
            series.sort(key=lambda p: p.price_date)
        self._prices = by_symbol
        return by_symbol

    # ── capabilities ─────────────────────────────────────────────────────────

    def eod_prices(self, symbol: str, start: date, end: date) -> list[Price]:
        self.require(Capability.EOD_PRICES)
        series = self._load_prices().get(symbol.strip().upper(), [])
        found = [p for p in series if start <= p.price_date <= end]
        if not found:
            raise DataUnavailableError(
                f"no prices for {symbol} between {start.isoformat()} and "
                f"{end.isoformat()} in {self.price_file}",
                code=E_PRICE_MISSING,
                remedy="Add the rows to the price file, or widen the date range.",
                symbol=symbol,
                known_symbols=sorted(self._load_prices()),
            )
        return found

    def corporate_actions(
        self, symbol: str, start: date, end: date
    ) -> list[CorporateActionRecord]:
        self.require(Capability.CORPORATE_ACTIONS)
        assert self.action_file is not None
        records: list[CorporateActionRecord] = []
        for row in self._rows(self.action_file, _ACTION_COLUMNS):
            if str(row["symbol"]).strip().upper() != symbol.strip().upper():
                continue
            ex_date = date.fromisoformat(str(row["ex_date"]).strip())
            if not (start <= ex_date <= end):
                continue
            records.append(
                CorporateActionRecord(
                    symbol=symbol.upper(),
                    action_type=str(row["action_type"]).strip(),
                    ex_date=ex_date,
                    pay_date=_optional_date(row.get("pay_date")),
                    record_date=_optional_date(row.get("record_date")),
                    split_numerator=_optional_decimal(row.get("split_numerator")),
                    split_denominator=_optional_decimal(row.get("split_denominator")),
                    cash_amount=_optional_decimal(row.get("cash_amount")),
                    provider_ref=f"file:{self.action_file.name}",
                )
            )
        return sorted(records, key=lambda r: r.ex_date)

    def benchmark_series(self, name: str, start: date, end: date) -> BenchmarkSeries:
        """Load a benchmark series.

        ``return_type`` is **required** in the file and has no default. This is
        the one place a benchmark enters `portable` in v0.1 -- fafnir carries no
        total-return index series (ADR 0006) -- so it is the one place the
        ``PORT-GIPS-G01`` question gets asked, and a file that does not answer
        it is rejected rather than assumed to be total-return.
        """
        self.require(Capability.BENCHMARKS)
        assert self.benchmark_file is not None

        loaded = json.loads(self.benchmark_file.read_text(encoding="utf-8"))
        series = loaded.get(name) if isinstance(loaded, dict) else None
        if series is None:
            raise DataUnavailableError(
                f"no benchmark named {name!r} in {self.benchmark_file}",
                code=E_PRICE_MISSING,
                remedy="Check the name, or add the series to the benchmark file.",
                name=name,
                known=sorted(loaded) if isinstance(loaded, dict) else [],
            )

        if "return_type" not in series:
            raise UsageError(
                f"benchmark {name!r} does not declare return_type",
                code=E_USAGE,
                remedy=(
                    'Add "return_type": "total_return" or "price_only". There is no '
                    "default, deliberately: a price-only series understates its "
                    "benchmark by roughly the dividend yield every year, and "
                    "PORT-GIPS-G01 requires portable to refuse one rather than "
                    "assume."
                ),
                name=name,
            )

        return BenchmarkSeries(
            name=name,
            is_total_return=series["return_type"] == "total_return",
            levels=tuple(
                (date.fromisoformat(d), from_text(str(v)))
                for d, v in sorted(series.get("levels", {}).items())
                if start <= date.fromisoformat(d) <= end
            ),
            source=f"file:{self.benchmark_file.name}",
            currency=series.get("currency", "USD"),
            periodicity=series.get("periodicity", "daily"),
            is_net_of_withholding=series.get("is_net_of_withholding"),
        )

    def lookup_security(self, symbol: str, *, on: date | None = None) -> Instrument:  # noqa: ARG002 -- see the note on `on` below
        """Minimal master from what the price file mentions.

        Type is always ``equity``: a price file records what something traded
        at, not what it is, and inferring an instrument type from a price would
        be a guess with a multiplier attached.

        ``on`` is accepted and ignored. A flat file has no symbol history, so
        there is no point-in-time answer to give -- and silently returning
        today's answer to a dated question is the kind of quiet wrongness this
        repository avoids. `FafnirProvider` does honour the date, using its
        warehouse's own ticker-history table; naming that table here would be
        the first step of the coupling ADR 0006 forbids, and the confinement
        test would (rightly) reject it.
        """
        known = self._load_prices()
        upper = symbol.strip().upper()
        if upper not in known:
            raise DataUnavailableError(
                f"{symbol} does not appear in {self.price_file}",
                code=E_PRICE_MISSING,
                symbol=symbol,
                known_symbols=sorted(known),
            )
        return Instrument(
            instrument_id=0,
            symbol=upper,
            instrument_type=InstrumentType.EQUITY,
            source=f"file:{self.price_file.name if self.price_file else '?'}",
        )


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return from_text(str(value).strip())


def _optional_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    return date.fromisoformat(str(value).strip())
