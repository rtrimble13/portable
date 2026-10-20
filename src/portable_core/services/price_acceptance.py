"""Price history accepted against the custodian's statements. ADR 0031.

Loading a price is one thing; knowing it is the right one is another. A
custodian's statement says what each position was worth on its date, which is
an anchor independent of every provider: a price from the wrong day, the wrong
instrument (a reused ticker) or the adjusted series disagrees with it.

Three rules, each the owner's decision (ADR 0031):

* **The price is compared, to the cent.** The statement's market value over
  its quantity is the custodian's price; portable's is the one valuation would
  use on that date (the newest on or before it, within the staleness
  tolerance). They tie when they agree to a cent a unit, plus the half cent
  the statement's rounding of market value to the cent can account for. A
  percentage of market value is not used: on a large position it hides a
  price from the wrong day.
* **A disagreement is accepted only with a reason**, and only while every
  figure it accepted is unchanged. A reloaded price makes it a break again.
* **An unresolved line blocks returns** for its account from its date
  (:func:`blocked_from`). An unpriced line is unresolved too: an unchecked
  price is not an agreeing one.

Whether a line ties is never stored. Statement lines are stored
(``statement_position``); the result is derived from them and the prices held
when asked, so it cannot go stale.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

from portable_core.decimals import money_context, quantize_money
from portable_core.domain.models import (
    Account,
    Instrument,
    PriceAcceptance,
    StatementPosition,
)
from portable_core.errors import ValidationError
from portable_core.persistence.repositories import Repositories
from portable_core.services.valuation import NO_MARKET_PRICE

__all__ = [
    "PriceLine",
    "Status",
    "accept",
    "blocked_from",
    "check",
    "check_recorded",
    "record",
]

#: A cent a unit: the owner's tolerance (ADR 0031).
PER_UNIT: Final = Decimal("0.01")
#: What a statement's rounding of market value to the cent can account for.
ROUNDING: Final = Decimal("0.005")
_STATED_PRICE: Final = Decimal("0.000001")
ZERO: Final = Decimal("0")


class Status(StrEnum):
    TIED = "tied"
    ACCEPTED = "accepted"
    BREAK = "break"
    #: No price on or before the date, or one stale beyond tolerance.
    UNPRICED = "unpriced"


@dataclass(frozen=True, slots=True)
class PriceLine:
    """One statement line against portable's price for its date."""

    account: str
    statement_date: date
    symbol: str
    #: The custodian's quantity and market value, as stated.
    quantity: Decimal
    market_value: Decimal
    #: The statement's market value over its quantity (and contract size), to
    #: six places. ``None`` for a zero quantity.
    stated_price: Decimal | None
    #: portable's price for the date, and where and when it is from.
    price: Decimal | None
    price_date: date | None
    price_source: str | None
    #: portable's price times the statement's quantity, less its market value.
    difference: Decimal | None
    status: Status
    #: The acceptance's reason, or why the line is unpriced.
    note: str = ""

    @property
    def is_unresolved(self) -> bool:
        return self.status in (Status.BREAK, Status.UNPRICED)

    def describe(self) -> str:
        head = f"{self.account} {self.symbol} {self.statement_date.isoformat()}"
        if self.status is Status.UNPRICED:
            return f"{head}: unpriced -- {self.note}"
        dated = self.price_date.isoformat() if self.price_date else "?"
        return (
            f"{head}: custodian {self.stated_price} vs portable {self.price} "
            f"({self.price_source}, {dated}), difference {self.difference}"
        )


def check(
    repos: Repositories,
    account: Account,
    instrument: Instrument,
    statement_date: date,
    quantity: Decimal,
    market_value: Decimal,
    *,
    staleness_days: int,
    recorded: StatementPosition | None = None,
) -> PriceLine:
    """One statement line against the price valuation would use on its date.

    *recorded*, when given, is the stored line whose acceptances apply.
    """
    units = quantity * instrument.contract_size
    with money_context():
        stated = (market_value / units).quantize(_STATED_PRICE) if units else None

    price: Decimal | None = None
    price_date: date | None = None
    source: str | None = None
    difference: Decimal | None = None
    note = ""
    if not instrument.has_market_price:
        # ADR 0028: valued at zero, at level 5, on every date.
        price, price_date, source = ZERO, statement_date, NO_MARKET_PRICE
    else:
        held = repos.prices.newest_on_or_before(instrument.instrument_id, statement_date)
        if held is None:
            note = f"no price on or before {statement_date.isoformat()}"
        else:
            price, price_date, source = held.price, held.price_date, held.source
            age = (statement_date - held.price_date).days
            if age > staleness_days:
                note = (
                    f"the newest price is {age} days old ({held.price_date.isoformat()}), "
                    f"beyond the {staleness_days}-day tolerance"
                )

    if note:
        status = Status.UNPRICED
    else:
        assert price is not None and price_date is not None
        with money_context():
            exact = price * units - market_value
            within = abs(exact) <= PER_UNIT * abs(units) + ROUNDING
        difference = quantize_money(exact)
        accepted = (
            _accepted(repos, recorded, price, price_date)
            if recorded is not None and not within
            else None
        )
        if within:
            status = Status.TIED
        elif accepted is not None:
            status, note = Status.ACCEPTED, accepted.reason
        else:
            status = Status.BREAK

    return PriceLine(
        account=account.name,
        statement_date=statement_date,
        symbol=instrument.symbol,
        quantity=quantity,
        market_value=market_value,
        stated_price=stated,
        price=price,
        price_date=price_date,
        price_source=source,
        difference=difference,
        status=status,
        note=note,
    )


def _accepted(
    repos: Repositories, recorded: StatementPosition, price: Decimal, price_date: date
) -> PriceAcceptance | None:
    """The newest acceptance whose every figure still holds, if any."""
    for acceptance in reversed(repos.statements.acceptances(recorded.statement_position_id)):
        if (
            acceptance.quantity == recorded.quantity
            and acceptance.market_value == recorded.market_value
            and acceptance.price == price
            and acceptance.price_date == price_date
        ):
            return acceptance
    return None


def check_recorded(
    repos: Repositories, *, staleness_days: int, account_id: int | None = None
) -> list[PriceLine]:
    """Every recorded statement line, checked against the prices held now."""
    lines: list[PriceLine] = []
    for recorded in repos.statements.all(account_id=account_id):
        account = repos.accounts.get(recorded.account_id)
        instrument = repos.instruments.get(recorded.instrument_id)
        assert account is not None and instrument is not None  # foreign keys
        lines.append(
            check(
                repos,
                account,
                instrument,
                recorded.statement_date,
                recorded.quantity,
                recorded.market_value,
                staleness_days=staleness_days,
                recorded=recorded,
            )
        )
    return lines


def blocked_from(lines: Iterable[PriceLine]) -> dict[str, PriceLine]:
    """Per account, its earliest unresolved line: returns are blocked from its date.

    `pert` refuses an account's returns for any period ending on or after it
    (ADR 0031); until `pert` exists, `pt value` and `pt price check` say so.
    """
    blocked: dict[str, PriceLine] = {}
    for line in sorted(lines, key=lambda x: (x.statement_date, x.symbol)):
        if line.is_unresolved and line.account not in blocked:
            blocked[line.account] = line
    return blocked


def record(
    repos: Repositories,
    account: Account,
    instrument: Instrument,
    statement_date: date,
    quantity: Decimal,
    market_value: Decimal,
    *,
    source: str,
) -> bool:
    """Store a statement line as an anchor. True when it is new or changed."""
    _, changed = repos.statements.record(
        StatementPosition(
            account_id=account.account_id,
            statement_date=statement_date,
            instrument_id=instrument.instrument_id,
            quantity=quantity,
            market_value=market_value,
            source=source,
        )
    )
    return changed


def accept(
    repos: Repositories,
    account: Account,
    instrument: Instrument,
    statement_date: date,
    reason: str,
    *,
    staleness_days: int,
) -> PriceLine:
    """Accept the recorded line's current disagreement, for *reason*.

    Raises:
        ValidationError: no line is recorded for that account, instrument and
            date; the line ties or is already accepted, so there is nothing to
            accept; it is unpriced, so there is no price to accept; or the
            reason is empty.
    """
    where = f"{account.name} {instrument.symbol} {statement_date.isoformat()}"
    text = reason.strip()
    if not text:
        raise ValidationError(
            "an acceptance needs a reason",
            remedy="Say why the difference is right, e.g. --reason 'evaluated bond price'.",
        )
    recorded = repos.statements.get(
        account.account_id, statement_date, instrument.instrument_id
    )
    if recorded is None:
        raise ValidationError(
            f"no statement line is recorded for {where}",
            remedy=(
                "Record the statement first: `pt reconcile --against STATEMENT.csv "
                f"--as-of {statement_date.isoformat()} --record`."
            ),
            account=account.name,
            symbol=instrument.symbol,
            statement_date=statement_date.isoformat(),
        )
    current = check(
        repos,
        account,
        instrument,
        statement_date,
        recorded.quantity,
        recorded.market_value,
        staleness_days=staleness_days,
        recorded=recorded,
    )
    if current.status is Status.UNPRICED:
        raise ValidationError(
            f"{where} is unpriced ({current.note}), so there is no price to accept",
            remedy="Load the price or set it with `pt price set`, then check again.",
            account=account.name,
            symbol=instrument.symbol,
        )
    if current.status is not Status.BREAK:
        raise ValidationError(
            f"{where} is {current.status}; there is nothing to accept",
            remedy="`pt price check` lists the lines that are breaks.",
            account=account.name,
            symbol=instrument.symbol,
            status=str(current.status),
        )
    assert current.price is not None and current.price_date is not None
    repos.statements.accept(
        PriceAcceptance(
            statement_position_id=recorded.statement_position_id,
            quantity=recorded.quantity,
            market_value=recorded.market_value,
            price=current.price,
            price_date=current.price_date,
            reason=text,
        )
    )
    return check(
        repos,
        account,
        instrument,
        statement_date,
        recorded.quantity,
        recorded.market_value,
        staleness_days=staleness_days,
        recorded=recorded,
    )
