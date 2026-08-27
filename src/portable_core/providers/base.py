"""The market data provider interface.

Capabilities are **separately declarable protocols**, so a partial provider is
legal and its gaps are *visible* (bootstrap §6.4). That matters more than it
sounds: `FafnirProvider` deliberately declares no benchmark capability, because
the warehouse carries no total-return index series and ``PORT-GIPS-G01``
requires refusal rather than approximation. A monolithic interface would have
forced it to implement that method and return something -- and "something" here
is a price-only series that understates the benchmark by roughly the dividend
yield every year.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from portable_core.domain.models import Instrument, Price
from portable_core.errors import DataUnavailableError
from portable_core.errors.kinds import E_PROVIDER_CAPABILITY

__all__ = [
    "BenchmarkCapability",
    "BenchmarkSeries",
    "Capability",
    "CorporateActionCapability",
    "CorporateActionRecord",
    "EndOfDayPriceCapability",
    "FxCapability",
    "MarketDataProvider",
    "RiskFreeRate",
    "RiskFreeRateCapability",
    "SecurityMasterCapability",
    "as_benchmark_provider",
    "as_corporate_action_provider",
    "as_eod_provider",
    "as_security_master",
    "require_capability",
]


class Capability(StrEnum):
    """What a provider can do. Reported by `pt introspect` and `pt info`."""

    SECURITY_MASTER = "security_master"
    EOD_PRICES = "eod_prices"
    INTRADAY_PRICES = "intraday_prices"
    CORPORATE_ACTIONS = "corporate_actions"
    DIVIDENDS = "dividends"
    BENCHMARKS = "benchmarks"
    RISK_FREE_RATE = "risk_free_rate"
    FX = "fx"


@dataclass(frozen=True, slots=True)
class CorporateActionRecord:
    """A corporate action as a provider reports it.

    Note what is **not** here: this is what the world did, not what `portable`
    did about it. Applying it produces ledger transactions, so that `pt rebuild`
    reproduces the effect from the ledger (ADR 0010).
    """

    symbol: str
    action_type: str
    ex_date: date
    pay_date: date | None = None
    record_date: date | None = None
    split_numerator: Decimal | None = None
    split_denominator: Decimal | None = None
    cash_amount: Decimal | None = None
    provider_ref: str | None = None


@dataclass(frozen=True, slots=True)
class BenchmarkSeries:
    """A benchmark's levels, with the one attribute that decides admissibility.

    ``is_total_return`` is not optional and has no default. ``PORT-GIPS-G01``
    prohibits price-only benchmarks in a performance report, and a defaulted
    ``True`` is exactly how a price index gets used by accident.
    """

    name: str
    is_total_return: bool
    levels: tuple[tuple[date, Decimal], ...]
    source: str
    currency: str = "USD"
    periodicity: str = "daily"
    is_net_of_withholding: bool | None = None


@dataclass(frozen=True, slots=True)
class RiskFreeRate:
    """A risk-free rate, named specifically enough to disclose.

    ``PORT-GIPS-F05`` requires the *name* of the risk-free rate used. "The
    risk-free rate" is not an acceptable disclosure; "3-month US Treasury bill,
    secondary market, from `duk yc`, as of 2026-06-30" is. Every field here
    exists so that sentence can be generated rather than typed.
    """

    series_name: str
    tenor: str
    rate: Decimal
    as_of: date
    source: str


# ── capability protocols ─────────────────────────────────────────────────────


@runtime_checkable
class SecurityMasterCapability(Protocol):
    def lookup_security(self, symbol: str, *, on: date | None = None) -> Instrument: ...


@runtime_checkable
class EndOfDayPriceCapability(Protocol):
    def eod_prices(self, symbol: str, start: date, end: date) -> list[Price]: ...


@runtime_checkable
class IntradayPriceCapability(Protocol):
    def last_price(self, symbol: str) -> Price: ...


@runtime_checkable
class CorporateActionCapability(Protocol):
    def corporate_actions(
        self, symbol: str, start: date, end: date
    ) -> list[CorporateActionRecord]: ...


@runtime_checkable
class BenchmarkCapability(Protocol):
    def benchmark_series(self, name: str, start: date, end: date) -> BenchmarkSeries: ...


@runtime_checkable
class RiskFreeRateCapability(Protocol):
    def risk_free_rate(self, on: date, *, tenor: str = "3M") -> RiskFreeRate: ...


@runtime_checkable
class FxCapability(Protocol):
    """Interface only in v0.1. Multi-currency is a backlog item (P1)."""

    def fx_rate(self, base: str, quote: str, on: date) -> Decimal: ...


# ── the base class ───────────────────────────────────────────────────────────


class MarketDataProvider(ABC):
    """A source of market data.

    Subclasses declare :attr:`capabilities` honestly. Declaring one you do not
    have is worse than declaring none: a caller will trust it, and `portable`'s
    whole posture is that a refusal beats a plausible number.
    """

    #: Short, stable name. Appears in `price.source`, so it ends up in the
    #: audit trail for every valuation (PORT-GIPS-J03).
    name: str = "provider"

    @property
    @abstractmethod
    def capabilities(self) -> frozenset[Capability]:
        """What this provider can actually do."""

    def has(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def require(self, capability: Capability) -> None:
        """Refuse politely, naming what would work instead."""
        require_capability(self, capability)

    def describe(self) -> dict[str, object]:
        """For `pt info` and `pt introspect`."""
        return {
            "provider": self.name,
            "capabilities": sorted(str(c) for c in self.capabilities),
        }


def as_eod_provider(provider: MarketDataProvider) -> EndOfDayPriceCapability:
    """Narrow a provider to its end-of-day price capability, or refuse.

    The capability protocols are deliberately separate from
    :class:`MarketDataProvider`, so a caller cannot reach ``eod_prices`` on a
    provider that has not declared it -- the type checker says so before the
    code runs. These helpers are the one sanctioned way through that boundary:
    they check the declaration, then confirm the shape, so a provider that
    claims a capability it does not implement fails here rather than three
    frames into a valuation.
    """
    require_capability(provider, Capability.EOD_PRICES)
    if not isinstance(provider, EndOfDayPriceCapability):
        raise _mismatch(provider, Capability.EOD_PRICES)
    return provider


def as_security_master(provider: MarketDataProvider) -> SecurityMasterCapability:
    """Narrow a provider to its security-master capability, or refuse."""
    require_capability(provider, Capability.SECURITY_MASTER)
    if not isinstance(provider, SecurityMasterCapability):
        raise _mismatch(provider, Capability.SECURITY_MASTER)
    return provider


def as_corporate_action_provider(
    provider: MarketDataProvider,
) -> CorporateActionCapability:
    """Narrow a provider to its corporate-action capability, or refuse."""
    require_capability(provider, Capability.CORPORATE_ACTIONS)
    if not isinstance(provider, CorporateActionCapability):
        raise _mismatch(provider, Capability.CORPORATE_ACTIONS)
    return provider


def as_benchmark_provider(provider: MarketDataProvider) -> BenchmarkCapability:
    """Narrow a provider to its benchmark capability, or refuse.

    Note that `FafnirProvider` never passes here: the warehouse carries no
    total-return index series, and PORT-GIPS-G01 requires refusal rather than
    approximation (ADR 0006).
    """
    require_capability(provider, Capability.BENCHMARKS)
    if not isinstance(provider, BenchmarkCapability):
        raise _mismatch(provider, Capability.BENCHMARKS)
    return provider


def _mismatch(provider: MarketDataProvider, capability: Capability) -> DataUnavailableError:
    """A provider declaring a capability it does not implement.

    A bug rather than a user error, and worth its own message: declaring a
    capability falsely is worse than declaring none, because a caller will
    trust it.
    """
    return DataUnavailableError(
        f"the {provider.name} provider declares {capability} but does not implement it",
        code=E_PROVIDER_CAPABILITY,
        remedy="This is a bug in the provider. Please report it.",
        provider=provider.name,
        capability=str(capability),
    )


def require_capability(provider: MarketDataProvider, capability: Capability) -> None:
    """Raise a clear error when a provider lacks a capability.

    Exit code 5, not a crash: "this provider cannot do that" is a data
    availability problem, and the message names both what was asked for and
    what the provider *can* do, so the next command is obvious.
    """
    if provider.has(capability):
        return
    raise DataUnavailableError(
        f"the {provider.name} provider cannot supply {capability}",
        code=E_PROVIDER_CAPABILITY,
        remedy=(
            f"Use a provider that can, or supply the data by hand. "
            f"{provider.name} offers: "
            f"{', '.join(sorted(str(c) for c in provider.capabilities)) or 'nothing'}."
        ),
        provider=provider.name,
        capability=str(capability),
        available=sorted(str(c) for c in provider.capabilities),
    )
