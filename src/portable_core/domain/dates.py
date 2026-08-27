"""Date arithmetic: holding periods and day-count conventions.

Pure functions over :class:`datetime.date`. No I/O, no clock -- every function
here takes the dates it needs, because `CLAUDE.md` invariant 6 forbids a
wall-clock dependency and because a function that reads "today" cannot be
tested at a boundary.

Two things in this module are load-bearing and are routinely got wrong.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

from portable_core.decimals import money_context
from portable_core.domain.enums import DayCount, HoldingPeriod

__all__ = [
    "accrual_fraction",
    "day_count_factor",
    "days_between",
    "holding_period",
    "is_long_term",
    "month_end",
    "year_fraction",
]


def days_between(start: date, end: date) -> int:
    """Calendar days from *start* to *end*. Negative when *end* precedes *start*."""
    return (end - start).days


def is_long_term(acquired: date, disposed: date) -> bool:
    """Whether a disposition qualifies for long-term treatment.

    **The rule, stated exactly:** long-term requires the disposition to be
    **more than one year** after the acquisition, measured **from the day after
    acquisition**. Equivalently, and how it is implemented here: the holding
    period is long when ``disposed > acquired + 1 year``, where the anniversary
    is a real calendar anniversary rather than 365 days.

    Three consequences worth stating, because each has been got wrong
    somewhere:

    * **Exactly one year is SHORT-term.** Buy on 2024-03-14, sell on
      2025-03-14, and that is short. Sell on 2025-03-15 and it is long.
    * **Leap years matter**, which is why this counts calendar anniversaries
      rather than days. Buy 2023-03-01, sell 2024-03-01: 366 days, still
      short-term, because it is exactly one year.
    * **29 February has no anniversary in a common year.** A lot acquired
      2024-02-29 has its anniversary on 2025-02-28 -- the last day of the same
      month -- so a disposition on 2025-02-28 is short and 2025-03-01 is long.
      Any other convention either invents a date or moves the boundary by a
      day.

    This function does **not** know about short sales. Short sales are always
    short-term regardless of holding period, and that rule belongs to the
    caller which knows whether the lot is short -- see :func:`holding_period`.
    """
    anniversary = _add_one_year(acquired)
    return disposed > anniversary


def _add_one_year(day: date) -> date:
    """The same calendar day one year later, clamped to a real date.

    Only 29 February needs clamping, and it clamps to 28 February -- the last
    day of the same month -- rather than to 1 March.
    """
    year = day.year + 1
    last_day = calendar.monthrange(year, day.month)[1]
    return date(year, day.month, min(day.day, last_day))


def holding_period(
    acquired: date,
    disposed: date,
    *,
    is_short_sale: bool = False,
) -> HoldingPeriod:
    """Classify a disposition's holding period.

    Args:
        acquired: the lot's holding-period start. Note this is **not**
            necessarily the trade date: a spinoff's new shares inherit the
            original holding period, and a split does not reset it.
        disposed: the disposition date.
        is_short_sale: when the lot is a short position. **Short sales are
            always short-term**, regardless of how long the position was held.
    """
    if is_short_sale:
        return HoldingPeriod.SHORT
    return HoldingPeriod.LONG if is_long_term(acquired, disposed) else HoldingPeriod.SHORT


def month_end(day: date) -> date:
    """The last calendar day of *day*'s month.

    GIPS requires annual and monthly valuation dates to fall at calendar period
    end or the last business day (PORT-GIPS-A03, A04). The business-day variant
    needs an exchange calendar and lives in the provider layer; this is the
    calendar one.
    """
    return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


# ── Day-count conventions ────────────────────────────────────────────────────


def day_count_factor(start: date, end: date, convention: DayCount) -> Decimal:
    """The year fraction between two dates under *convention*.

    Used for bond accrued interest, which is **part of market value**
    (PORT-GIPS-A06) -- so an error here is a return error, not merely a bond
    calculation error.

    The conventions:

    * **30/360** (US/NASD): every month is 30 days, every year 360. The
      end-of-month adjustments are the fiddly part and are implemented below.
    * **ACT/ACT** (ISDA): actual days, divided by the actual length of the
      year(s) spanned. A period crossing a year boundary is split, so a leap
      year contributes days/366 for its part.
    * **ACT/365** (fixed): actual days over 365, leap year or not.
    * **ACT/360**: actual days over 360. Yields more than a year's accrual for
      a full year, which is correct for this convention and not a bug.
    """
    with money_context():
        if convention is DayCount.THIRTY_360:
            return Decimal(_days_30_360(start, end)) / Decimal(360)
        if convention is DayCount.ACT_365:
            return Decimal(days_between(start, end)) / Decimal(365)
        if convention is DayCount.ACT_360:
            return Decimal(days_between(start, end)) / Decimal(360)
        if convention is DayCount.ACT_ACT:
            return _act_act(start, end)
    raise ValueError(f"unhandled day-count convention: {convention!r}")


def _days_30_360(start: date, end: date) -> int:
    """Day count under the 30/360 US convention.

    The adjustments, in order, are what distinguish this from "pretend every
    month has 30 days":

    1. If the start day is 31, it becomes 30.
    2. If the end day is 31 **and** the (already adjusted) start day is 30,
       the end day becomes 30. Note the condition: an end on the 31st with a
       start before the 30th is *not* adjusted, and gets the extra day.
    """
    d1, d2 = start.day, end.day
    if d1 == 31:
        d1 = 30
    if d2 == 31 and d1 == 30:
        d2 = 30
    return 360 * (end.year - start.year) + 30 * (end.month - start.month) + (d2 - d1)


def _act_act(start: date, end: date) -> Decimal:
    """ACT/ACT (ISDA): each calendar year's days over that year's length.

    Splitting at the year boundary is the whole point. A period spanning
    2023-12-01 to 2024-02-01 accrues 31 days over 365 for the 2023 part and 31
    days over 366 for the 2024 part -- not 62 days over some blended
    denominator.
    """
    if end < start:
        return -_act_act(end, start)
    if start == end:
        return Decimal(0)

    fraction = Decimal(0)
    cursor = start
    while cursor < end:
        year_end = date(cursor.year, 12, 31)
        segment_end = min(end, date(cursor.year + 1, 1, 1))
        days = (segment_end - cursor).days
        year_length = 366 if calendar.isleap(cursor.year) else 365
        fraction += Decimal(days) / Decimal(year_length)
        cursor = segment_end
        if cursor > year_end and cursor >= end:
            break
    return fraction


def year_fraction(start: date, end: date, convention: DayCount) -> Decimal:
    """Alias for :func:`day_count_factor`, named for return-period contexts."""
    return day_count_factor(start, end, convention)


def accrual_fraction(
    last_coupon: date,
    settlement: date,
    next_coupon: date,
    convention: DayCount,
) -> Decimal:
    """The fraction of a coupon period accrued at *settlement*.

    A bond bought between coupons pays accrued interest to the seller. **That
    is not basis** -- it is a receivable that the next coupon extinguishes, and
    treating it as basis overstates cost and understates the eventual gain.

    Returns a value in [0, 1]; a settlement on a coupon date accrues nothing.
    """
    with money_context():
        elapsed = day_count_factor(last_coupon, settlement, convention)
        period = day_count_factor(last_coupon, next_coupon, convention)
        if period == 0:
            return Decimal(0)
        return elapsed / period
