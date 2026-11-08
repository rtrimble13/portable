"""A bond's coupon schedule and the interest it has accrued. ADR 0041 §3.

Pure functions over a :class:`~portable_core.domain.models.BondDetail`: no
I/O. What a price is worth against a bond's face is the bond's own method,
:meth:`BondDetail.value_of`.

* **Quantity is face amount**, in currency, as a custodian reports it.
* **Coupons fall on a schedule stepped back from maturity** by
  ``12 / coupon_frequency`` months. A maturity on a month-end keeps every
  coupon on a month-end. ``first_coupon_date`` bounds the schedule from below:
  before it lies a first period whose start -- the dated date -- is not
  stored, so nothing is accrued there, and the caller is told so.
* **Accrued interest runs to the date asked for.** ACT/ACT is the
  bond-market convention (ICMA): actual days elapsed over the actual days in
  the coupon period, times the period's coupon. That is not the ISDA year
  fraction of :func:`~portable_core.domain.dates.day_count_factor`, which
  splits a period at each 1 January. 30/360, ACT/365 and ACT/360 accrue the
  annual rate over their own year fraction.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from portable_core.decimals import money_context
from portable_core.domain.dates import day_count_factor
from portable_core.domain.enums import DayCount
from portable_core.domain.models import BondDetail

__all__ = [
    "CouponPeriod",
    "OddFirstPeriod",
    "accrued_interest",
    "coupon_amount",
    "coupon_period",
    "last_coupon_on_or_before",
]


class OddFirstPeriod(ValueError):
    """The date falls in the first coupon period, whose start is not stored."""


@dataclass(frozen=True, slots=True)
class CouponPeriod:
    """The scheduled coupons either side of a date: ``last <= on < next``."""

    last: date
    next: date


def _is_month_end(day: date) -> bool:
    return day.day == calendar.monthrange(day.year, day.month)[1]


def _scheduled(bond: BondDetail, steps_back: int) -> date:
    """The coupon *steps_back* periods before maturity (0 is maturity itself)."""
    maturity = bond.maturity_date
    step = 12 // bond.coupon_frequency
    year, month = divmod(maturity.year * 12 + (maturity.month - 1) - steps_back * step, 12)
    month += 1
    last_day = calendar.monthrange(year, month)[1]
    day = last_day if _is_month_end(maturity) else min(maturity.day, last_day)
    return date(year, month, day)


def coupon_period(bond: BondDetail, on: date) -> CouponPeriod | None:
    """The scheduled coupon dates around *on*, or None when nothing accrues.

    None for a zero-coupon bond, and on or after maturity.

    Raises:
        OddFirstPeriod: *on* is before ``first_coupon_date``, or that date is
            not on the schedule from maturity, so the period's start is not
            known.
    """
    if bond.is_zero_coupon or on >= bond.maturity_date:
        return None
    step = 12 // bond.coupon_frequency
    months_back = (bond.maturity_date.year - on.year) * 12 + (
        bond.maturity_date.month - on.month
    )
    k = max(1, months_back // step)
    while _scheduled(bond, k) > on:
        k += 1
    while k > 1 and _scheduled(bond, k - 1) <= on:
        k -= 1
    period = CouponPeriod(last=_scheduled(bond, k), next=_scheduled(bond, k - 1))

    first = bond.first_coupon_date
    if first is not None:
        if on < first:
            raise OddFirstPeriod(
                f"{on.isoformat()} is before the first coupon ({first.isoformat()}); the "
                "date that period accrues from is not stored"
            )
        if period.last < first:
            raise OddFirstPeriod(
                f"the first coupon ({first.isoformat()}) is not on the schedule stepped "
                f"back from maturity ({bond.maturity_date.isoformat()})"
            )
    return period


def last_coupon_on_or_before(bond: BondDetail, on: date) -> date | None:
    """The scheduled coupon on or before *on*: the one a coupon paid on *on* is for.

    None when no scheduled coupon falls on or before it, or the bond pays none.
    """
    if bond.is_zero_coupon:
        return None
    if on >= bond.maturity_date:
        return bond.maturity_date
    try:
        period = coupon_period(bond, on)
    except OddFirstPeriod:
        return None
    return None if period is None else period.last


def coupon_amount(bond: BondDetail, quantity: Decimal) -> Decimal:
    """One full coupon on *quantity* of face, unrounded."""
    if bond.is_zero_coupon:
        return Decimal(0)
    with money_context():
        return bond.coupon_rate / Decimal(bond.coupon_frequency) * quantity


def accrued_interest(bond: BondDetail, quantity: Decimal, on: date) -> Decimal:
    """Interest accrued on *quantity* of face from the last coupon to *on*, unrounded.

    Raises:
        OddFirstPeriod: see :func:`coupon_period`.
    """
    period = coupon_period(bond, on)
    if period is None:
        return Decimal(0)
    with money_context():
        if bond.day_count is DayCount.ACT_ACT:
            elapsed = Decimal((on - period.last).days)
            length = Decimal((period.next - period.last).days)
            return coupon_amount(bond, quantity) * elapsed / length
        return bond.coupon_rate * quantity * day_count_factor(period.last, on, bond.day_count)
