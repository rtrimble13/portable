"""An index: its definition, the market data it is built from, and what it builds. ADR 0050.

A `.bmk` file holds one of these. The definition is the owner's and is never
edited; the inputs are facts with a source; the built rows follow from both.
No I/O here, and every amount is a ``Decimal``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from portable_core.domain.base import check_decimal_fields

__all__ = [
    "Close",
    "DistributionSchedule",
    "Dividend",
    "EventType",
    "HoldingSpan",
    "IndexBuild",
    "IndexMeta",
    "LevelRow",
    "MarketCap",
    "Member",
    "Methodology",
    "RebalanceRule",
    "RebalanceWeight",
    "Reconstitution",
    "Reinvestment",
    "Schedule",
    "SecurityEvent",
    "SecurityType",
    "Split",
    "SyntheticSecurity",
    "Weighting",
]


class Weighting(StrEnum):
    """How a methodology sets its target weights (ADR 0050 §3)."""

    CUSTOM = "custom"
    EQUAL = "equal"
    CAP = "cap"
    PRICE = "price"


class RebalanceRule(StrEnum):
    """When the units are reset to the targets, at a period's last close."""

    DAILY = "daily"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    SEMIANNUAL = "semiannual"
    ANNUAL = "annual"
    DRIFT_BAND = "drift_band"
    NEVER = "never"


class SecurityType(StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    MUTUAL_FUND = "mutual_fund"
    ADR = "adr"
    REIT = "reit"


class EventType(StrEnum):
    """What happened to a member that fafnir does not carry (ADR 0050 §5)."""

    DELISTING = "delisting"
    CASH_MERGER = "cash_merger"
    STOCK_MERGER = "stock_merger"
    SPINOFF = "spinoff"


class Schedule(StrEnum):
    """How often a security pays (ADR 0050, answer 3)."""

    NONE = "none"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    SEMIANNUAL = "semiannual"
    ANNUAL = "annual"
    IRREGULAR = "irregular"


@dataclass(frozen=True, slots=True)
class IndexMeta:
    """The index's identity. ``base_date`` and ``base_level`` never move once built."""

    index_name: str
    description: str
    base_date: date
    base_level: Decimal
    currency: str
    #: The exchange whose trading days the index is computed on.
    calendar: str

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class SyntheticSecurity:
    """A security the index may hold, identified by fafnir's id, not its ticker."""

    security_id: int
    symbol: str
    security_type: SecurityType
    source: str
    fafnir_sid: int | None = None
    name: str | None = None
    currency: str = "USD"
    #: The issuer, so one issuer is held once (ADR 0050 §6).
    issuer_key: str | None = None
    provider_ref: str | None = None


@dataclass(frozen=True, slots=True)
class Methodology:
    """How the index weights and rebalances, from a date's close."""

    methodology_id: int
    effective_from: date
    weighting: Weighting
    rebalance_rule: RebalanceRule
    drift_band: Decimal | None = None
    note: str | None = None
    corrects_methodology_id: int | None = None

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class Member:
    """One member of a reconstitution."""

    security_id: int
    #: The target under `custom`; None under every other method.
    weight: Decimal | None = None
    #: Under `cap`: the share of the count that floats. None is full shares.
    float_factor: Decimal | None = None
    float_source: str | None = None

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class Reconstitution:
    """The full membership from a date's close."""

    reconstitution_id: int
    effective_date: date
    members: tuple[Member, ...]
    note: str | None = None
    corrects_reconstitution_id: int | None = None


@dataclass(frozen=True, slots=True)
class SecurityEvent:
    """A delisting, merger or spinoff, declared (ADR 0050 §5)."""

    event_id: int
    security_id: int
    #: A delisting or merger: the last day held, valued at its consideration.
    #: A spinoff: its ex-date, when the received shares are added at the open.
    event_date: date
    event_type: EventType
    source: str
    cash_per_share: Decimal | None = None
    received_security_id: int | None = None
    received_ratio: Decimal | None = None
    note: str | None = None
    corrects_event_id: int | None = None

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class DistributionSchedule:
    """How often a security pays, from a date."""

    schedule_id: int
    security_id: int
    effective_from: date
    schedule: Schedule
    source: str
    max_gap_days: int | None = None
    corrects_schedule_id: int | None = None


@dataclass(frozen=True, slots=True)
class Close:
    """An unadjusted close."""

    security_id: int
    trade_date: date
    close: Decimal
    source: str
    as_of: str

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class Split:
    """A split: ``numerator`` new shares for each ``denominator`` held."""

    security_id: int
    ex_date: date
    numerator: Decimal
    denominator: Decimal
    source: str
    as_of: str

    def __post_init__(self) -> None:
        check_decimal_fields(self)

    @property
    def ratio(self) -> Decimal:
        return self.numerator / self.denominator


@dataclass(frozen=True, slots=True)
class Dividend:
    """A cash dividend per share, gross of withholding, entitled on its ex-date."""

    dividend_id: int
    security_id: int
    ex_date: date
    amount: Decimal
    source: str
    as_of: str
    pay_date: date | None = None

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class MarketCap:
    """A member's market cap on a date, with what `duk mc` computed it from."""

    security_id: int
    cap_date: date
    market_cap: Decimal
    source: str
    as_of: str
    close_raw: Decimal | None = None
    shares_outstanding: Decimal | None = None
    share_factor: Decimal | None = None
    fiscal_date: date | None = None
    known_from: date | None = None

    def __post_init__(self) -> None:
        check_decimal_fields(self)


# ── built ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LevelRow:
    """One trading day's levels."""

    level_date: date
    price_level: Decimal
    total_return_level: Decimal
    dividend_points: Decimal
    divisor: Decimal
    is_rebalance: bool
    is_reconstitution: bool
    member_count: int

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class HoldingSpan:
    """A member's units over days in which they did not change, inclusive."""

    security_id: int
    from_date: date
    to_date: date
    units: Decimal

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class RebalanceWeight:
    """A member's target at a rebalance or reconstitution close."""

    rebalance_date: date
    security_id: int
    target_weight: Decimal
    close: Decimal
    units: Decimal
    weighting_cap: Decimal | None = None

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class Reinvestment:
    """A dividend the total-return level reinvested, and the points it added."""

    dividend_id: int
    level_date: date
    security_id: int
    units: Decimal
    amount: Decimal
    index_points: Decimal

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class IndexBuild:
    """The build the derived rows came from."""

    inputs_hash: str
    first_date: date
    last_date: date
    built_at: str
    built_by: str
