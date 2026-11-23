"""Multi-period returns: what a period's length allows. ADR 0045.

So far, annualising. The periods a table shows, and the rows it builds from
one walk, follow with `pert periods`.
"""

from __future__ import annotations

import decimal
from datetime import date
from decimal import Decimal
from typing import Final

from portable_core.domain.periods import at_least_a_year, years
from portable_core.errors import GipsRefusalError, ValidationError
from portable_core.errors.kinds import E_GIPS_ANNUALIZE_SUB_YEAR, E_RETURN_TOTAL_LOSS
from portable_core.services.returns import RETURN_CONTEXT

__all__ = ["annualize"]

_ONE: Final = Decimal(1)


def annualize(cumulative: Decimal, *, after: date, through: date) -> Decimal:
    """The annualised rate of *cumulative*, from the end of *after* to the end of *through*.

    ADR 0045 §5: ``exp(ln(1 + R) / N) - 1``, where N counts whole anniversaries
    and the part year left over against the year it falls in
    (:func:`~portable_core.domain.periods.years`). It is computed in
    :data:`RETURN_CONTEXT`. ``Decimal.ln`` and ``Decimal.exp`` are correctly
    rounded, which a non-integral ``**`` is not guaranteed to be, so every
    step's rounding is defined and so are the bytes (invariant 6). Over
    exactly one year the rate is the cumulative return itself. Computed
    through ``exp`` and ``ln``, it could differ from it in the last digit.

    Raises:
        GipsRefusalError: the period is under a year (``PORT-GIPS-B07``).
            Annualising it is prohibited outright, not discouraged.
        ValidationError: the cumulative return is -100% or below. No rate
            compounds to nothing, and none is invented.
    """
    if not at_least_a_year(after, through):
        raise GipsRefusalError(
            f"cannot annualize the return from {after.isoformat()} to "
            f"{through.isoformat()}: the period is shorter than a year",
            requirement="PORT-GIPS-B07",
            code=E_GIPS_ANNUALIZE_SUB_YEAR,
            remedy=(
                "Present the cumulative return for the period instead. Annualizing "
                "a sub-year return is prohibited outright, not merely discouraged."
            ),
            after=after.isoformat(),
            through=through.isoformat(),
        )
    length = years(after, through)
    with decimal.localcontext(RETURN_CONTEXT):
        growth = _ONE + cumulative
        if growth <= 0:
            raise ValidationError(
                f"the return from {after.isoformat()} to {through.isoformat()} is "
                f"{cumulative}, and a loss of everything has no annualized rate",
                code=E_RETURN_TOTAL_LOSS,
                remedy="Present the cumulative return. Nothing is clamped or estimated.",
                cumulative=str(cumulative),
            )
        if (length.whole, length.days) == (1, 0):
            return cumulative
        count = Decimal(length.whole) + Decimal(length.days) / Decimal(length.of)
        return (growth.ln() / count).exp() - _ONE
