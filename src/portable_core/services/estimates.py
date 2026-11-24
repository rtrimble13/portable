"""Carried prices, for the days `pert` cannot measure. ADR 0044 §1.

`pert twr --estimate` values a day valuation could not price with a carried
price: the last price observed for each holding it could not price. The rules
here are the whole of what an estimate may be:

* **The last known price** is the newest of a stored price, a fill
  (`buy`, `sell`, `short`, `cover`) in any account, and a reinvested
  distribution without tax withheld, whose amount over its units is a
  purchase at the fund's price. A stored price wins a tie with a ledger
  row, and between ledger rows on one day the later in ledger order wins.
* **Never across a corporate action** of the instrument, since prices are
  unadjusted: a split, a reverse split, a stock dividend, a spinoff, a
  merger or a delisting since the last known price refuses the carry.
* **Lowered by every cash distribution since**, except a coupon, from the
  day the snapshot first holds it (`values_from`), by the gross amount per
  unit the account was entitled to. Otherwise the distribution would be
  counted twice: in the snapshot's cash or receivable, and in the price.
* **Only for a holding valuation could not price**: missing, stale beyond
  `staleness_tolerance_days`, or recorded more than five days old
  (:data:`PERT_STALENESS_DAYS`). A day incomplete for anything else, such
  as a bond whose interest cannot be accrued, is not estimated.

Nothing here is stored. An estimate is computed when asked for, from the file
and the command line alone (invariant 6).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final, Protocol

from portable_core.decimals import money_context, quantize_money
from portable_core.domain.enums import TransactionType
from portable_core.domain.models import (
    ACCRUED_FROM_EX_DATE,
    Instrument,
    Transaction,
    ValuationSnapshot,
)
from portable_core.domain.returns import CarriedHolding, CarrySource
from portable_core.errors import DataUnavailableError, GipsRefusalError, PortableError
from portable_core.errors.kinds import (
    E_ESTIMATE_ACROSS_ACTION,
    E_ESTIMATE_UNAVAILABLE,
    E_GIPS_VALUATION_GAP,
)
from portable_core.persistence.repositories import Repositories
from portable_core.services.replay import over_dates
from portable_core.services.snapshots import held
from portable_core.services.valuation import Holding, ValuationEngine

__all__ = [
    "PERT_STALENESS_DAYS",
    "AccountDay",
    "Entitlements",
    "HoldingsBook",
    "LastKnown",
    "Ledger",
    "carried_price",
    "entitlement_days",
    "estimate_account_day",
    "last_known",
    "needs_estimate",
]

#: ADR 0044 §1. The oldest a snapshot's price may be for `pert` to measure the
#: day with it, whatever `staleness_tolerance_days` the snapshot was built
#: under: a weekend and a holiday or two. An older price is a carried price,
#: not PORT-GIPS-A09's best approximation, and only an estimate values a day
#: with one. Fixed so that one file gives one answer on every machine.
PERT_STALENESS_DAYS: Final = 5

_DAY: Final = timedelta(days=1)

#: Trades whose price is an observed price of the instrument.
_FILLS: Final = frozenset(
    {
        TransactionType.BUY,
        TransactionType.SELL,
        TransactionType.SELL_SHORT,
        TransactionType.BUY_TO_COVER,
    }
)
#: After any of these, a price from before it is a price of something else.
_BARRIERS: Final = frozenset(
    {
        TransactionType.SPLIT,
        TransactionType.REVERSE_SPLIT,
        TransactionType.STOCK_DIVIDEND,
        TransactionType.SPINOFF,
        TransactionType.MERGER_CASH,
        TransactionType.MERGER_STOCK,
        TransactionType.MERGER_MIXED,
        TransactionType.DELIST,
    }
)
#: The distributions a snapshot holds from `values_from` that lower a price.
#: A coupon does not: a bond's quoted price is clean (ADR 0041).
_LOWERING: Final = ACCRUED_FROM_EX_DATE - {TransactionType.COUPON}


class Ledger:
    """The effective ledger, by instrument, in ledger order."""

    def __init__(self, rows: Iterable[Transaction]) -> None:
        self._rows: dict[int, list[Transaction]] = {}
        for txn in rows:
            if txn.instrument_id is not None:
                self._rows.setdefault(txn.instrument_id, []).append(txn)

    def of(self, instrument_id: int) -> Sequence[Transaction]:
        return self._rows.get(instrument_id, ())


class Entitlements(Protocol):
    """What an account held of an instrument at the end of a day."""

    def units(self, account_id: int, instrument_id: int, day: date) -> Decimal: ...


class HoldingsBook:
    """Each account's holdings at the end of chosen days, as derived state stood then.

    Read inside :func:`over_dates` (ADR 0021), so a holding is the one the
    snapshot of that day priced, or could not. Days are read in batches and
    kept, so a day is replayed to once however many windows ask for it.
    """

    def __init__(self, repos: Repositories, account_ids: Iterable[int]) -> None:
        self.repos = repos
        self.account_ids = tuple(sorted(account_ids))
        self._held: dict[date, dict[int, tuple[Holding, ...]]] = {}

    def load(self, days: Iterable[date]) -> None:
        wanted = sorted(set(days) - set(self._held))
        if not wanted:
            return
        read = over_dates(
            self.repos,
            wanted,
            lambda _on: {a: tuple(held(self.repos, a)) for a in self.account_ids},
        )
        for on, found in zip(wanted, read.results, strict=True):
            self._held[on] = found

    def on(self, account_id: int, day: date) -> tuple[Holding, ...]:
        return self._held[day][account_id]

    def units(self, account_id: int, instrument_id: int, day: date) -> Decimal:
        return sum(
            (
                h.quantity
                for h in self.on(account_id, day)
                if h.instrument.instrument_id == instrument_id
            ),
            Decimal(0),
        )


@dataclass(frozen=True, slots=True)
class LastKnown:
    """The newest price observed for an instrument on or before a day."""

    price: Decimal
    on: date
    source: CarrySource
    #: The price row's id, or the fill's or the reinvestment's txn_id.
    source_id: int


def last_known(
    repos: Repositories, ledger: Ledger, instrument_id: int, on: date
) -> LastKnown | None:
    """The newest of a stored price, a fill and an untaxed reinvestment. ADR 0044 §1.

    A stored price wins a tie with a ledger row. Between ledger rows on one
    day the later in ledger order wins.
    """
    stored = repos.prices.newest_on_or_before(instrument_id, on)
    row: LastKnown | None = None
    for txn in ledger.of(instrument_id):
        if txn.trade_date > on:
            break
        price = _observed(txn)
        if price is not None:
            source = (
                CarrySource.REINVESTMENT
                if txn.txn_type is TransactionType.DIVIDEND_REINVEST
                else CarrySource.FILL
            )
            row = LastKnown(price, txn.trade_date, source, txn.txn_id)
    if (
        stored is not None
        and stored.price_id is not None
        and (row is None or stored.price_date >= row.on)
    ):
        return LastKnown(stored.price, stored.price_date, CarrySource.PRICE, stored.price_id)
    return row


def _observed(txn: Transaction) -> Decimal | None:
    """The instrument's price a ledger row observed, if it observed one."""
    if txn.txn_type in _FILLS:
        return txn.price if txn.price is not None and txn.price > 0 else None
    if txn.txn_type is TransactionType.DIVIDEND_REINVEST:
        # Not with tax withheld, until it is confirmed that the recorded
        # amount is then still what bought the units (ADR 0044, answer 4).
        if txn.taxes_withheld != 0 or not txn.quantity or txn.gross_amount is None:
            return None
        with money_context():
            return txn.gross_amount / txn.quantity
    return None


def carried_price(
    repos: Repositories,
    ledger: Ledger,
    book: Entitlements,
    *,
    account_id: int,
    instrument: Instrument,
    on: date,
) -> tuple[LastKnown, Decimal, Decimal]:
    """(last known price, lowered by per unit, price carried) for a holding on *on*.

    *book* must hold the account's holdings at the end of the day before
    each lowering distribution's `values_from`: what it was entitled to
    (:func:`entitlement_days`).

    Raises:
        PortableError: no price to carry (PT-E-ESTIMATE-UNAVAILABLE), a
            corporate action since it (PT-E-ESTIMATE-ACROSS-ACTION), or
            distributions that take it to zero (PT-E-ESTIMATE-UNAVAILABLE).
    """
    symbol = instrument.symbol
    known = last_known(repos, ledger, instrument.instrument_id, on)
    if known is None:
        raise DataUnavailableError(
            f"{symbol} has no price, fill or reinvestment on or before {on.isoformat()} "
            "to carry",
            code=E_ESTIMATE_UNAVAILABLE,
            remedy=(
                f"Price it with `pt price set {symbol}` or `pt price load`. Nothing is "
                "estimated from nothing."
            ),
            symbol=symbol,
            day=on.isoformat(),
        )
    for txn in ledger.of(instrument.instrument_id):
        if known.on < txn.trade_date <= on and txn.txn_type in _BARRIERS:
            raise DataUnavailableError(
                f"{symbol}'s last known price, of {known.on.isoformat()}, is from before "
                f"its {txn.txn_type.value.replace('_', ' ')} of {txn.trade_date.isoformat()}"
                f", so it is not a price of what is held on {on.isoformat()}",
                code=E_ESTIMATE_ACROSS_ACTION,
                remedy=(
                    "Prices are unadjusted, so a price from before a corporate action is "
                    f"never carried past it. Price {symbol} after {txn.trade_date.isoformat()}."
                ),
                symbol=symbol,
                price_date=known.on.isoformat(),
                action=txn.txn_type.value,
                action_date=txn.trade_date.isoformat(),
                txn_id=txn.txn_id,
            )
    lowered = Decimal(0)
    for txn in _lowering(ledger, account_id, instrument.instrument_id, known.on, on):
        entitled = book.units(account_id, instrument.instrument_id, txn.values_from - _DAY)
        if entitled <= 0 or txn.gross_amount is None:
            raise DataUnavailableError(
                f"{symbol}'s distribution of {txn.values_from.isoformat()} (txn "
                f"{txn.txn_id}) cannot be put per unit: the account held no units the "
                "day before it",
                code=E_ESTIMATE_UNAVAILABLE,
                remedy="Check the distribution's dates, or price the holding directly.",
                symbol=symbol,
                txn_id=txn.txn_id,
            )
        with money_context():
            lowered += txn.gross_amount / entitled
    with money_context():
        price = known.price - lowered
    if price <= 0:
        raise DataUnavailableError(
            f"{symbol}'s carried price of {known.price} from {known.on.isoformat()} falls "
            f"to {price} after the distributions since it, so it carries nothing",
            code=E_ESTIMATE_UNAVAILABLE,
            remedy=f"Price {symbol} after its distributions, with `pt price set`.",
            symbol=symbol,
            price=str(known.price),
            lowered_by=str(lowered),
        )
    return known, lowered, price


def _lowering(
    ledger: Ledger, account_id: int, instrument_id: int, after: date, through: date
) -> list[Transaction]:
    """The account's distributions of the instrument first held after *after*."""
    return [
        txn
        for txn in ledger.of(instrument_id)
        if txn.account_id == account_id
        and txn.txn_type in _LOWERING
        and after < txn.values_from <= through
    ]


def entitlement_days(
    repos: Repositories,
    ledger: Ledger,
    account_id: int,
    holdings: Sequence[Holding],
    on: date,
) -> set[date]:
    """The days before each distribution a carry on *on* would be lowered by."""
    days: set[date] = set()
    for holding in holdings:
        instrument_id = holding.instrument.instrument_id
        known = last_known(repos, ledger, instrument_id, on)
        if known is None:
            continue
        for txn in _lowering(ledger, account_id, instrument_id, known.on, on):
            days.add(txn.values_from - _DAY)
    return days


@dataclass(frozen=True, slots=True)
class AccountDay:
    """One account's value on an estimated day, and what of it was carried."""

    value: Decimal
    carried: tuple[CarriedHolding, ...]


def needs_estimate(snapshot: ValuationSnapshot) -> bool:
    """Whether the day's snapshot is one `pert` cannot measure with."""
    return not snapshot.is_complete or any(
        p.staleness_days > PERT_STALENESS_DAYS for p in snapshot.prices
    )


def estimate_account_day(
    repos: Repositories,
    ledger: Ledger,
    book: HoldingsBook,
    snapshot: ValuationSnapshot,
    *,
    account: str,
    coupons: Mapping[int, frozenset[date]],
) -> AccountDay:
    """The account's value on the snapshot's day, carrying what valuation could not price.

    The stored ending value already holds every priced holding, the cash and
    the accruals. Each holding with no recorded price, or one recorded more
    than :data:`PERT_STALENESS_DAYS` old, is valued at its carried price
    instead (:func:`carried_price`).

    Raises:
        PortableError: the day is incomplete for a cause an estimate does not
            cover, or a holding cannot be carried.
    """
    on = snapshot.snapshot_date
    recorded = {price.instrument_id: price for price in snapshot.prices}
    engine = ValuationEngine()
    value = snapshot.ending_market_value
    carried: list[CarriedHolding] = []
    for holding in book.on(snapshot.account_id, on):
        instrument = holding.instrument
        if instrument.bond is not None:
            try:
                engine.accrued_interest(holding, on)
                engine.require_coupons_recorded(
                    holding, on, coupons.get(instrument.instrument_id, frozenset())
                )
            except DataUnavailableError as exc:
                raise _not_estimated(account, on, exc) from None
        if not instrument.has_market_price:
            continue
        price = recorded.get(instrument.instrument_id)
        if price is not None and price.staleness_days <= PERT_STALENESS_DAYS:
            continue
        known, lowered, carry = carried_price(
            repos,
            ledger,
            book,
            account_id=snapshot.account_id,
            instrument=instrument,
            on=on,
        )
        with money_context():
            worth = quantize_money(instrument.value_of(holding.quantity, carry))
            value += worth - (price.market_value if price is not None else Decimal(0))
        carried.append(
            CarriedHolding(
                account=account,
                symbol=instrument.symbol,
                quantity=holding.quantity,
                price=carry,
                last_known=known.price,
                price_date=known.on,
                age_days=(on - known.on).days,
                source=known.source,
                source_id=known.source_id,
                lowered_by=lowered,
                value=worth,
            )
        )
    if not carried and not snapshot.is_complete:
        raise _not_estimated(account, on, None)
    return AccountDay(value=value, carried=tuple(carried))


def _not_estimated(account: str, on: date, cause: PortableError | None) -> GipsRefusalError:
    why = (
        cause.message
        if cause is not None
        else "the snapshot is incomplete for a cause other than a missing price"
    )
    return GipsRefusalError(
        f"{account} on {on.isoformat()} cannot be valued, and is not estimated: {why}",
        requirement="PORT-GIPS-A03",
        code=E_GIPS_VALUATION_GAP,
        remedy=(
            "An estimate carries a price, nothing else (ADR 0044 §1). A bond's accrued "
            "interest or an unrecorded coupon is a missing ledger row: record it, and "
            "`pt value` the day again."
        ),
        account=account,
        day=on.isoformat(),
    )
