"""What a time-weighted return is, as a value. ADR 0040.

Frozen, with no I/O: :mod:`portable_core.services.returns` computes these, and
a formatter renders them. The method of every sub-period travels with the
result (``PORT-GIPS-B05``), and so does the period's length, so that the
formatter can refuse to annualise a period under a year (``PORT-GIPS-B07``).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from portable_core.domain.base import check_decimal_fields
from portable_core.domain.enums import FlowLevel

__all__ = [
    "BASES",
    "Basis",
    "BasisReturn",
    "CarriedHolding",
    "CarrySource",
    "Disclosures",
    "Estimate",
    "EstimatedDay",
    "ReturnMethod",
    "SubPeriod",
    "TimeWeightedReturn",
    "TimedFlow",
]


class ReturnMethod(enum.StrEnum):
    """How one sub-period's return was computed. ``PORT-GIPS-B05``."""

    #: One calendar day between two valuations. The exact daily return under
    #: the flow timing of ADR 0040 §3.
    DAILY = "daily"
    #: More than one day between two valuations, across days that could not
    #: be valued: Modified Dietz in calendar days, the fallback
    #: ``PORT-GIPS-B06`` permits where daily returns are not calculated. The
    #: formula is the archived 2011 Guidance Statement on Calculation
    #: Methodology's; the 2020 provisions name no method.
    MODIFIED_DIETZ = "modified_dietz"


class Basis(enum.StrEnum):
    """The return bases of ``PORT-GIPS-D01``, and the supplemental one.

    In presentation order: net-of-fees first, the required presentation for
    a total fund (AO 24.A.1.b, ADR 0040 answer 4).
    """

    NET_OF_FEES = "net_of_fees"
    NET_OF_EXTERNAL_COSTS_ONLY = "net_of_external_costs_only"
    GROSS_OF_FEES = "gross_of_fees"
    #: Every cost deducted, ``other_admin`` included: the market values as
    #: they are. Supplemental information, labelled so, and no GIPS basis.
    NET_OF_ALL_COSTS = "net_of_all_costs"


#: Every basis, in presentation order.
BASES: tuple[Basis, ...] = tuple(Basis)


@dataclass(frozen=True, slots=True)
class BasisReturn:
    """One basis and its return, as a decimal fraction: 1.25% is ``0.0125``."""

    basis: Basis
    value: Decimal

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class TimedFlow:
    """One external flow at the level of the return, on its day."""

    txn_id: int
    on: date
    amount: Decimal
    is_large: bool

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class SubPeriod:
    """From the end of ``after`` to the end of ``through``, valued at both ends."""

    after: date
    through: date
    method: ReturnMethod
    beginning_value: Decimal
    ending_value: Decimal
    flows: tuple[TimedFlow, ...]
    #: Per basis, in :data:`BASES` order.
    returns: tuple[BasisReturn, ...]
    #: Either end was valued at a carried price (ADR 0044 §2).
    is_estimated: bool = False

    def __post_init__(self) -> None:
        check_decimal_fields(self)

    @property
    def days(self) -> int:
        return (self.through - self.after).days

    def return_for(self, basis: Basis) -> Decimal:
        return next(entry.value for entry in self.returns if entry.basis is basis)


class CarrySource(enum.StrEnum):
    """Where a carried price was last observed. ADR 0044 §1."""

    PRICE = "price"
    FILL = "fill"
    #: A reinvested distribution: a purchase at the fund's price that day.
    REINVESTMENT = "reinvestment"


@dataclass(frozen=True, slots=True)
class CarriedHolding:
    """One holding valued at a carried price on an estimated day. ADR 0044 §2."""

    account: str
    symbol: str
    quantity: Decimal
    #: The price it was valued at: the last known price, lowered by every
    #: cash distribution since (ADR 0044 §1).
    price: Decimal
    last_known: Decimal
    price_date: date
    #: Days from the last known price to the day estimated.
    age_days: int
    source: CarrySource
    #: The price row's id, or the fill's or the reinvestment's txn_id.
    source_id: int
    #: Per unit, the distributions since the last known price.
    lowered_by: Decimal
    value: Decimal

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class EstimatedDay:
    """A day valued with a carried price, and how much of it was carried."""

    on: date
    #: The carried holdings' value over the level's value that day.
    share: Decimal
    holdings: tuple[CarriedHolding, ...]

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class Estimate:
    """What made a return an estimate rather than a measurement. ADR 0044 §2.

    A result carrying one is never part of the track record (§3).
    """

    #: The largest share of a day's value that could be carried: the
    #: `--estimate` given, echoed so the figure is reproducible.
    limit: Decimal
    days: tuple[EstimatedDay, ...]

    def __post_init__(self) -> None:
        check_decimal_fields(self)

    @property
    def peak(self) -> EstimatedDay:
        """The day the most was carried; the earliest of equals."""
        return max(self.days, key=lambda day: (day.share, -day.on.toordinal()))

    @property
    def holdings(self) -> int:
        return sum(len(day.holdings) for day in self.days)

    @property
    def oldest_age_days(self) -> int:
        return max(holding.age_days for day in self.days for holding in day.holdings)


@dataclass(frozen=True, slots=True)
class Disclosures:
    """What the report must say beside the number (ADR 0040 §10)."""

    #: Valuation days on which a consumed price was an estimate (PORT-GIPS-A09).
    estimated_days: tuple[date, ...]
    #: Valuation days on which a consumed price was older than the day.
    stale_price_days: tuple[date, ...]
    #: Market value priced at level 5 at the period's end, and its share
    #: (PORT-GIPS-H05). The share is None when the ending value is zero.
    level5_value: Decimal
    level5_share: Decimal | None
    #: Holdings valued at zero for want of a market price (ADR 0028).
    unpriced_holdings: tuple[str, ...]
    #: Margin interest was charged, and is deducted at every basis.
    margin_interest: bool
    #: Reclaimable withholding was recorded and is not accrued (PORT-GIPS-A06).
    reclaimable_withholding: bool
    #: Days carrying a flow the policy in force calls large (PORT-GIPS-B03).
    large_flow_days: tuple[date, ...]
    #: Whether any external_mgmt_fee fell in the period. When none did,
    #: gross-of-fees and net-of-external-costs-only are the same number.
    external_fees: bool


@dataclass(frozen=True, slots=True)
class TimeWeightedReturn:
    """A cumulative time-weighted return, with everything it was made from."""

    level: FlowLevel
    #: The account, at account level; None at portfolio level.
    account: str | None
    requested_start: date
    #: The first day the return covers. That is the requested start, moved
    #: later to inception, or past days on which nothing was held to where
    #: the track record begins again (ADR 0045 §4). It is never moved
    #: silently -- both are carried.
    start: date
    #: The last day the return covers: the requested end, or the day the
    #: track record ended when nothing was held after it (PORT-GIPS-H02).
    end: date
    requested_end: date
    inception: date
    #: Per basis, in :data:`BASES` order.
    cumulative: tuple[BasisReturn, ...]
    subperiods: tuple[SubPeriod, ...]
    #: The days, first and last, with nothing held and nothing moving that
    #: the start was moved past. None when it was not.
    nothing_held_before: tuple[date, date] | None
    #: The days with nothing held from the day after the record ended to the
    #: requested end. None when the record did not end inside the period.
    nothing_held_after: tuple[date, date] | None
    disclosures: Disclosures
    #: None for a measured return. Set only under `--estimate`, and only when
    #: a carried price was needed (ADR 0044 §2).
    estimate: Estimate | None = None

    def __post_init__(self) -> None:
        check_decimal_fields(self)

    @property
    def days(self) -> int:
        """Calendar days covered, counting both ends."""
        return (self.end - self.start).days + 1

    def return_for(self, basis: Basis) -> Decimal:
        return next(entry.value for entry in self.cumulative if entry.basis is basis)

    def count(self, method: ReturnMethod) -> int:
        return sum(1 for sub in self.subperiods if sub.method is method)
