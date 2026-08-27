"""The Decimal boundary: representation, arithmetic context, and rounding.

`CLAUDE.md` invariant 1 and ADR 0005. Every money, quantity, price, and rate in
`portable` passes through this module. It is the one place that knows how a
number becomes text and how text becomes a number, so that there is one place
to check when a figure looks wrong.

Three things live here and nowhere else:

1. **The canonical text form.** SQLite has no decimal type, so money is stored
   as ``TEXT``. :func:`to_text` and :func:`from_text` are the only permitted
   conversions.
2. **The arithmetic context.** One :class:`decimal.Context`, used by every
   engine, so that precision and rounding cannot vary by call site.
3. **The rounding boundaries.** Rounding happens at the seven places ADR 0005
   enumerates and nowhere else. Intermediate results are never rounded; a total
   is allocated across parts by largest remainder so the parts sum exactly to
   the whole and the residue is never dropped.
"""

from __future__ import annotations

import decimal
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager
from decimal import Decimal
from typing import Final

__all__ = [
    "CENT",
    "PORTABLE_CONTEXT",
    "QUANTITY_PLACES",
    "ZERO",
    "allocate",
    "from_text",
    "is_whole",
    "money_context",
    "quantize_money",
    "quantize_quantity",
    "to_text",
]

# ── The context ──────────────────────────────────────────────────────────────

#: The one arithmetic context. ADR 0005.
#:
#: ``prec=34`` is decimal128's coefficient length -- comfortably beyond any
#: realistic share count times price, with room for intermediate products.
#:
#: ``Inexact`` and ``Rounded`` are deliberately **not** trapped: dividing a
#: dollar amount by a share count is legitimately inexact, and trapping it
#: would make correct code raise. ``InvalidOperation``, ``DivisionByZero``,
#: ``Overflow`` and ``Underflow`` **are** trapped, because each of those means
#: the computation has gone wrong rather than merely lost a digit.
PORTABLE_CONTEXT: Final[decimal.Context] = decimal.Context(
    prec=34,
    rounding=decimal.ROUND_HALF_EVEN,
    Emin=-6143,
    Emax=6144,
    capitals=1,
    clamp=0,
    flags=[],
    traps=[
        decimal.InvalidOperation,
        decimal.DivisionByZero,
        decimal.Overflow,
        decimal.Underflow,
    ],
)

ZERO: Final[Decimal] = Decimal("0")

#: Currency quantum. Money persisted to the ledger is 2 dp, ROUND_HALF_EVEN.
CENT: Final[Decimal] = Decimal("0.01")

#: Fractional-share quantum, for accounts that permit fractional holdings.
#: Six places matches fafnir's ``NUMERIC(20,6)`` price and quantity scale, so a
#: round trip through the warehouse cannot lose a digit.
QUANTITY_PLACES: Final[Decimal] = Decimal("0.000001")


def money_context() -> AbstractContextManager[decimal.Context]:
    """Run a block under :data:`PORTABLE_CONTEXT`.

    Engines wrap their arithmetic in this rather than relying on the ambient
    context, because the ambient context is process-global and something else
    may have changed it.

        >>> with money_context():
        ...     _ = Decimal("1") / Decimal("3")
    """
    return decimal.localcontext(PORTABLE_CONTEXT)


# ── Text representation ──────────────────────────────────────────────────────


def to_text(value: Decimal) -> str:
    """Render *value* in the canonical decimal text form for storage.

    ``format(value, "f")``, not ``str(value)``: ``str`` produces scientific
    notation for small and large exponents (``str(Decimal("1E+2")) == '1E+2'``),
    which would sort and compare wrongly as text and would not round-trip
    through a naive reader.

    **Trailing zeros are preserved.** A price quoted ``10.500`` is stored
    ``10.500``, because the trailing zeros carry the significance the source
    asserted and discarding them discards information about the quote. The
    consequence is that text equality is stricter than numeric equality --
    compare money in Python, with :class:`~decimal.Decimal`, not in SQL.

    Negative zero normalises to ``0``: ``-0.00`` and ``0.00`` are the same
    amount of money, and letting them differ in storage would break the
    byte-identical round-trip that `pt export`/`pt import` promises.

    Raises:
        ValueError: if *value* is a NaN or an infinity. Neither is an amount of
            money, and storing one would defer the failure to whoever reads it.
    """
    if not isinstance(value, Decimal):  # defensive: the guard that catches float
        raise TypeError(f"expected Decimal, got {type(value).__name__}: {value!r}")
    if value.is_nan() or value.is_infinite():
        raise ValueError(f"not a finite decimal: {value!r}")
    if value.is_zero() and value.is_signed():
        value = -value
    return format(value, "f")


def from_text(text: str) -> Decimal:
    """Parse the canonical text form back to a :class:`~decimal.Decimal`.

    Raises:
        ValueError: on anything that is not a finite decimal, including the
            strings ``"NaN"`` and ``"Infinity"`` that :class:`Decimal` would
            otherwise accept happily.
    """
    try:
        value = Decimal(text)
    except decimal.InvalidOperation as exc:
        raise ValueError(f"not a decimal: {text!r}") from exc
    if value.is_nan() or value.is_infinite():
        raise ValueError(f"not a finite decimal: {text!r}")
    return value


# ── Rounding, at the documented boundaries only ──────────────────────────────


def quantize_money(value: Decimal, *, quantum: Decimal = CENT) -> Decimal:
    """Round a currency amount to the storage quantum. ADR 0005.

    Call this **at the boundary where an amount is persisted or presented**,
    never on an intermediate result. Rounding a running total on every step
    accumulates the rounding error that this repository exists to avoid.
    """
    with money_context():
        return value.quantize(quantum, rounding=decimal.ROUND_HALF_EVEN)


def quantize_quantity(value: Decimal, *, places: Decimal = QUANTITY_PLACES) -> Decimal:
    """Round a share or contract quantity to the account's permitted scale.

    Note what this function does **not** do: it does not decide whether a
    fractional quantity is permissible. That is the caller's question, and the
    answer is a property of the account and the instrument. A corporate action
    that would create a fractional share an account cannot hold is an error
    (``PT-E-FRACTIONAL-SHARE``), not a rounding -- see `CLAUDE.md` invariant 9.
    """
    with money_context():
        return value.quantize(places, rounding=decimal.ROUND_HALF_EVEN)


def is_whole(value: Decimal) -> bool:
    """True when *value* has no fractional part.

    Used where a fractional result is an error rather than a rounding: option
    contracts, and share quantities in accounts that do not permit fractions.
    """
    return value == value.to_integral_value()


# ── Allocation ───────────────────────────────────────────────────────────────


def allocate(
    total: Decimal, weights: Sequence[Decimal], *, quantum: Decimal = CENT
) -> list[Decimal]:
    """Split *total* across *weights* so the parts sum **exactly** to the total.

    Largest-remainder apportionment. Naively rounding each share independently
    loses or invents money: allocating $100.00 across three equal lots gives
    three amounts of $33.33 and a missing cent. That cent is a basis error that
    propagates into a realized gain and then into a tax figure.

    This is what allocates commissions and fees across lots, a spinoff's basis
    across old and new shares, and a merger's consideration across
    constituents.

    Args:
        total: the amount to distribute. May be negative.
        weights: relative weights. Must be non-negative and not all zero.
        quantum: the smallest unit of the result.

    Returns:
        One amount per weight, in the same order, summing exactly to
        ``quantize_money(total, quantum=quantum)``.

    Raises:
        ValueError: if *weights* is empty, contains a negative, or sums to zero
            -- each of which means the caller does not know how to split this,
            and guessing would be the wrong kind of helpful.
    """
    if not weights:
        raise ValueError("cannot allocate across an empty set of weights")
    if any(w < 0 for w in weights):
        raise ValueError(f"weights must be non-negative: {weights!r}")

    with money_context():
        weight_total = sum(weights, ZERO)
        if weight_total == 0:
            raise ValueError("weights sum to zero; nothing to allocate across")

        target = quantize_money(total, quantum=quantum)

        exact = [target * w / weight_total for w in weights]
        floors = [e.quantize(quantum, rounding=decimal.ROUND_DOWN) for e in exact]
        allocated = sum(floors, ZERO)

        # Distribute the residue one quantum at a time, largest remainder
        # first. Ties break on the earlier index, so the result is
        # deterministic -- CLAUDE.md invariant 6 reaches down to here.
        residue = target - allocated
        steps = int((residue / quantum).to_integral_value(rounding=decimal.ROUND_HALF_EVEN))
        step = quantum if steps >= 0 else -quantum

        order = sorted(
            range(len(weights)),
            key=lambda i: (-(exact[i] - floors[i]), i),
        )
        result = list(floors)
        for n in range(abs(steps)):
            result[order[n % len(order)]] += step

        assert sum(result, ZERO) == target, "allocation did not preserve the total"
        return result


def total(values: Iterable[Decimal]) -> Decimal:
    """Sum decimals under the portable context.

    A trivial helper that exists so that summation, too, happens under a known
    precision rather than whatever context the caller happened to inherit.
    """
    with money_context():
        return sum(values, ZERO)
