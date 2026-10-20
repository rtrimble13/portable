"""Market data providers.

Capabilities are separately declarable, so a partial provider is legal and its
gaps are visible. See `docs/market-data.md` and ADR 0006.
"""

from __future__ import annotations

from portable_core.providers.base import (
    BenchmarkSeries,
    Capability,
    CorporateActionRecord,
    DailyRange,
    DailyRangeCapability,
    MarketDataProvider,
    RiskFreeRate,
    TradingCalendar,
    TradingCalendarCapability,
    as_benchmark_provider,
    as_corporate_action_provider,
    as_daily_range_provider,
    as_eod_provider,
    as_security_master,
    as_trading_calendar_provider,
    require_capability,
)
from portable_core.providers.file import FileProvider
from portable_core.providers.null import NullProvider, OfflineProvider

__all__ = [
    "BenchmarkSeries",
    "Capability",
    "CorporateActionRecord",
    "DailyRange",
    "DailyRangeCapability",
    "FileProvider",
    "MarketDataProvider",
    "NullProvider",
    "OfflineProvider",
    "RiskFreeRate",
    "TradingCalendar",
    "TradingCalendarCapability",
    "as_benchmark_provider",
    "as_corporate_action_provider",
    "as_daily_range_provider",
    "as_eod_provider",
    "as_security_master",
    "as_trading_calendar_provider",
    "build_provider",
    "require_capability",
]


def build_provider(name: str, **options: object) -> MarketDataProvider:
    """Construct a provider by name, mirroring fafnir's `-S` ergonomics.

    ``FafnirProvider`` is imported lazily so that `psycopg`'s absence is felt
    only by somebody who asked for fafnir -- never at CLI start.
    """
    match name:
        case "file":
            return FileProvider(
                price_file=options.get("price_file"),  # type: ignore[arg-type]
                action_file=options.get("action_file"),  # type: ignore[arg-type]
                benchmark_file=options.get("benchmark_file"),  # type: ignore[arg-type]
            )
        case "fafnir":
            from portable_core.providers.fafnir import FafnirProvider

            return FafnirProvider(
                dsn=options.get("fafnir_dsn"),  # type: ignore[arg-type]
                duk_path=str(options.get("duk_path") or "duk"),
            )
        case "null" | "none" | "":
            return NullProvider()
        case _:
            from portable_core.errors import UsageError

            raise UsageError(
                f"unknown market data source {name!r}",
                remedy="Choose one of: file, fafnir, null.",
                source=name,
            )
