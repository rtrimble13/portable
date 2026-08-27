"""Number presentation rules. Once, here, and nowhere else.

`CLAUDE.md`: "New output formatting rules go in `formatters/`, once, not inline
at call sites." This module is that place.

The rules, from the bootstrap (§6.2) and `docs/output-formats.md`:

* **Human formats** (`table`, `markdown`) -- thousands separators, 2dp for
  currency, a leading minus for negatives (chosen over parentheses; see
  :func:`money`), basis points for small returns.
* **Machine formats** (`json`, `csv`) -- **full stored precision, never
  rounded**, and ``Decimal`` serialised as a **string**, never a float.
* **Explicit null** everywhere. A blank and a zero must never look the same.

And the two return-specific rules that live here so that no call site can
bypass them:

* a return for a period shorter than one year is **never annualized**
  (``PORT-GIPS-B07``);
* every rendered return carries its **method, basis, and period**
  (``PORT-GIPS-H04``).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from portable_core.decimals import money_context, quantize_money, to_text
from portable_core.errors import GipsRefusalError
from portable_core.formatters.model import ColumnKind, ReturnValue

__all__ = [
    "NULL_TEXT",
    "human",
    "machine",
    "money",
    "price",
    "quantity",
    "rate",
    "render_return",
    "require_not_annualized",
]

#: What a missing value looks like in a human format. Deliberately not "0",
#: not "-", and not blank: an em dash is unmistakably "no value", and a reader
#: scanning a column can tell it from a zero at a glance.
NULL_TEXT = "—"

#: A return smaller than this renders in basis points rather than percent,
#: because "0.03%" and "3 bps" are the same number and only one of them is
#: readable in a column of them.
_BPS_THRESHOLD = Decimal("0.01")

#: Below one year, annualizing is prohibited outright (PORT-GIPS-B07).
_DAYS_IN_YEAR = 365


def money(value: Decimal | None, *, null: str = NULL_TEXT) -> str:
    """Currency for a human format: thousands separators, two decimals.

    Negatives take a **leading minus**, not parentheses. Both conventions are
    defensible; the bootstrap requires choosing one and being consistent, and
    the minus wins because it survives being copied into a spreadsheet, a
    terminal with a narrow column, and an LLM context window, where a lone
    ``)`` at a line break becomes ambiguous.
    """
    if value is None:
        return null
    return f"{quantize_money(value):,.2f}"


def quantity(value: Decimal | None, *, null: str = NULL_TEXT) -> str:
    """A share or contract count.

    Trailing zeros are trimmed -- ``100`` rather than ``100.000000`` -- because
    a whole-share count padded to six places reads as a precision claim nobody
    made. The stored value is untouched; this is presentation only.
    """
    if value is None:
        return null
    # `normalize()` produces exponent form for a trailing-zero value --
    # Decimal("100.000000").normalize() is Decimal("1E+2") -- and formatting
    # that with ":," keeps the exponent. This is the same trap ADR 0005 names
    # for str(): go through the integral value, or through "f" formatting,
    # never through the default repr.
    if value == value.to_integral_value():
        return f"{int(value):,}"
    trimmed = format(value.normalize(), "f")
    return f"{Decimal(trimmed):,f}"


def price(value: Decimal | None, *, places: int = 2, null: str = NULL_TEXT) -> str:
    """A per-unit price. Widened past two places when the value needs it.

    A sub-penny price must not floor to ``0.00``: a back-adjusted series or a
    deep out-of-the-money option genuinely trades there, and rendering it as
    zero says something false. This mirrors `duk`'s behaviour, so the two tools
    show the same number.
    """
    if value is None:
        return null
    with money_context():
        if value != 0 and abs(value) < Decimal("0.01"):
            return f"{value.normalize():f}"
        return f"{value.quantize(Decimal(10) ** -places):,.{places}f}"


def rate(value: Decimal | None, *, null: str = NULL_TEXT) -> str:
    """A rate or return, as a percentage -- or basis points when small."""
    if value is None:
        return null
    with money_context():
        if value != 0 and abs(value) < _BPS_THRESHOLD:
            return f"{(value * 10000).quantize(Decimal('0.1'))} bps"
        return f"{(value * 100).quantize(Decimal('0.01'))}%"


def human(value: object, kind: ColumnKind, *, null: str = NULL_TEXT) -> str:
    """Render one value for `table` or `markdown`."""
    if value is None:
        return null
    match kind:
        case ColumnKind.MONEY:
            return money(value)  # type: ignore[arg-type]
        case ColumnKind.QUANTITY:
            return quantity(value)  # type: ignore[arg-type]
        case ColumnKind.PRICE:
            return price(value)  # type: ignore[arg-type]
        case ColumnKind.RATE:
            return rate(value)  # type: ignore[arg-type]
        case ColumnKind.RETURN:
            return render_return(value)  # type: ignore[arg-type]
        case ColumnKind.BOOL:
            return "yes" if value else "no"
        case ColumnKind.INTEGER:
            return f"{int(value):,}"  # type: ignore[call-overload]
        case _:
            return str(value)


def machine(value: object) -> object:
    """Render one value for `json` or `csv`.

    **Full stored precision, and `Decimal` becomes a string.** Never a float:
    ``json.dumps(Decimal("0.1"))`` cannot be made to round-trip through a JSON
    number, and a consumer that parses it as a float has silently lost the
    guarantee this whole codebase is built on.
    """
    if isinstance(value, Decimal):
        return to_text(value)
    if isinstance(value, ReturnValue):
        return {
            "value": to_text(value.value),
            "method": value.method,
            "basis": value.basis,
            "period_start": value.period_start.isoformat(),
            "period_end": value.period_end.isoformat(),
            "period_days": value.period_days,
            "is_annualized": value.is_annualized,
            "is_supplemental": value.is_supplemental,
        }
    if isinstance(value, date | datetime):
        return value.isoformat()
    return value


# ── the two return rules ─────────────────────────────────────────────────────


def require_not_annualized(value: ReturnValue) -> None:
    """Refuse to render an annualized return for a period under one year.

    ``PORT-GIPS-B07``: "Returns for periods of less than one year must not be
    annualized." Unconditional -- Firms 2.A.12 and Asset Owners 22.A.9 -- and it
    binds the since-inception money-weighted return too, which is the one place
    the natural implementation (``XIRR``) returns an annualized rate by
    construction and has to be de-annualized or refused (``PORT-GIPS-C03``).

    The check lives in the formatter deliberately. Anywhere else, a call site
    would eventually be added that does not call it.
    """
    if value.is_annualized and value.period_days < _DAYS_IN_YEAR:
        raise GipsRefusalError(
            f"cannot present an annualized return for a {value.period_days}-day "
            f"period ({value.period_start.isoformat()} to "
            f"{value.period_end.isoformat()})",
            requirement="PORT-GIPS-B07",
            code="PT-E-GIPS-ANNUALIZE-SUB-YEAR",
            remedy=(
                "Present the cumulative return for the period instead. Annualizing "
                "a sub-year return is prohibited outright, not merely discouraged."
            ),
            period_days=value.period_days,
            period_start=value.period_start.isoformat(),
            period_end=value.period_end.isoformat(),
        )


def render_return(value: ReturnValue) -> str:
    """Render a return **with its method, basis, and period**.

    ``PORT-GIPS-H04``. There is no way to render a bare number through this
    function, which is the point: a return without its basis is not
    interpretable, and a reader given one will assume whichever basis flatters
    the manager.
    """
    require_not_annualized(value)

    parts = [rate(value.value)]
    descriptor = [value.method.upper(), value.basis.replace("_", "-")]
    if value.is_annualized:
        descriptor.append("annualized")
    if value.is_supplemental:
        descriptor.append("SUPPLEMENTAL")
    parts.append(f"({', '.join(descriptor)})")
    return " ".join(parts)
