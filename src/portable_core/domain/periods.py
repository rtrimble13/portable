"""Return periods: a year counted by anniversary, and the fiscal year. ADR 0045 §2, §5.

Pure functions and values over :class:`datetime.date`, with no I/O and no
clock. A *period* here runs from the end of one day (*after*) to the end of
another (*through*), which is how every return in `portable` is dated
(ADR 0040 §3).

Two rules, each easy to get wrong in a way no test of an ordinary date catches:

* **A year is an anniversary, not 365 days** (``PORT-GIPS-B07``). The period
  from the end of 2023-12-31 to the end of 2024-12-30 is 365 days and one day
  short of a year, because 2024 has a 29 February. Counting days would let it
  be annualised, which the standard forbids outright.
* **A return period runs month end to month end.** When *after* is the last
  day of its month, its anniversary is the last day of that month in the
  later year, so the end of February goes to the end of February. This is
  deliberately **not** the tax holding period's anniversary
  (:func:`portable_core.domain.dates.is_long_term`). A holding period is
  measured from a calendar date to the same calendar date, so 2023-02-28 goes
  to 2024-02-28 there. Making the two rules agree would move a tax boundary
  by a day.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

from portable_core.domain.dates import month_end

__all__ = [
    "FiscalYearEnd",
    "Years",
    "at_least_a_year",
    "shift_years",
    "years",
]

#: A common year: its February ends on the 28th, which is how a fiscal year
#: ending in February is written (`02-28`, the last day in every year).
_COMMON_YEAR = 2023


def shift_years(day: date, count: int) -> date:
    """*day* moved by *count* whole years, forward or back, month end to month end.

    The last day of a month goes to the last day of the same month, so
    2024-02-29 goes to 2025-02-28 and 2023-02-28 to 2024-02-29. Any other day
    keeps its month and day. That date always exists: the only day a year
    can lack is 29 February, and in its year that is the month's last day.
    """
    year = day.year + count
    if day == month_end(day):
        return date(year, day.month, calendar.monthrange(year, day.month)[1])
    return date(year, day.month, day.day)


def at_least_a_year(after: date, through: date) -> bool:
    """Whether the period from the end of *after* to the end of *through* is a year or more.

    ``PORT-GIPS-B07``: a return for a period under a year is never
    annualised. This is the test, wherever it is made.
    """
    return through >= shift_years(after, 1)


@dataclass(frozen=True, slots=True)
class Years:
    """A period's length: *whole* anniversaries, then *days* more of a year *of* days long.

    The *days* are counted from the last whole anniversary, and *of* is the
    length of the year that follows it, so a part year is measured against
    the year it falls in (actual/actual). A period of exactly three years is
    3 whatever 29 Februaries it holds. Converting to a number is arithmetic,
    and belongs to the service that owns the decimal context.
    """

    whole: int
    days: int
    of: int


def years(after: date, through: date) -> Years:
    """The length of the period from the end of *after* to the end of *through*. ADR 0045 §5.

    Raises:
        ValueError: *through* is before *after*.
    """
    if through < after:
        raise ValueError(f"a period cannot end ({through}) before it starts ({after})")
    whole = through.year - after.year
    while whole > 0 and shift_years(after, whole) > through:
        whole -= 1
    base = shift_years(after, whole)
    return Years(
        whole=whole,
        days=(through - base).days,
        of=(shift_years(after, whole + 1) - base).days,
    )


@dataclass(frozen=True, slots=True)
class FiscalYearEnd:
    """The month a portfolio's fiscal year ends in, on its last day. ``PORT-GIPS-A04``.

    Stored as `meta.fiscal_year_end`, `MM-DD`. ADR 0045 §2 requires a month
    end. Quarters are the three-month steps back from it, and a weeks-based
    (52/53-week) fiscal year is not supported.
    """

    month: int

    @classmethod
    def parse(cls, text: str) -> FiscalYearEnd:
        """Read `MM-DD`, which must name the last day of its month.

        `02-28` is the end of February in every year, the 29th in a leap
        year. `02-29` is refused, since three years in four have no such
        day.

        Raises:
            ValueError: what is wrong with *text*, in a sentence.
        """
        match = re.fullmatch(r"(\d{2})-(\d{2})", text)
        if match is None:
            raise ValueError(f"{text!r} is not MM-DD")
        month, day = int(match[1]), int(match[2])
        if not 1 <= month <= 12:
            raise ValueError(f"{text!r} has no month {month:02d}")
        last = calendar.monthrange(_COMMON_YEAR, month)[1]
        if month == 2 and day == 29:
            raise ValueError(
                f"{text!r} does not exist in three years of four; 02-28 means the "
                "last day of February in every year"
            )
        if day != last:
            raise ValueError(
                f"{text!r} is not the last day of its month: a fiscal year ends at a "
                f"month end, here {month:02d}-{last:02d}"
            )
        return cls(month)

    def __str__(self) -> str:
        return f"{self.month:02d}-{calendar.monthrange(_COMMON_YEAR, self.month)[1]:02d}"

    def in_year(self, year: int) -> date:
        """The day the fiscal year ending in *year* ends."""
        return month_end(date(year, self.month, 1))
