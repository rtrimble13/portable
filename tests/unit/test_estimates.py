"""Carried prices: what an estimate may value a holding at. ADR 0044 §1.

Each rule is a way a carried price would be plausibly wrong without it:
- the wrong last price;
- a pre-split price carried past the split;
- a dividend counted twice, once in cash and once in the price.

The figures are worked by hand.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal as D
from types import SimpleNamespace
from typing import cast

import pytest

from portable_core.domain.enums import TransactionType
from portable_core.domain.models import Account, Instrument, Price
from portable_core.domain.returns import CarriedHolding, CarrySource, EstimatedDay
from portable_core.errors import PortableError
from portable_core.persistence.repositories import Repositories
from portable_core.services import returns
from portable_core.services.estimates import AccountDay, Ledger, carried_price, last_known
from portable_core.services.replay import effective_ledger
from portable_core.services.returns import Walk
from tests.conftest import append

pytestmark = pytest.mark.unit


class Held:
    """The account held the same units every day: enough for per-unit arithmetic."""

    def __init__(self, units: str) -> None:
        self.held = D(units)

    def units(self, account_id: int, instrument_id: int, day: date) -> D:
        return self.held


def _price(repos: Repositories, instrument: Instrument, on: date, price: str) -> int:
    return repos.prices.add(
        Price(instrument.instrument_id, on, D(price), "test", datetime(2024, 1, 1, tzinfo=UTC))
    )


def _ledger(repos: Repositories) -> Ledger:
    return Ledger(effective_ledger(repos.transactions.in_ledger_order()))


def _carry(
    repos: Repositories, account: Account, instrument: Instrument, on: date, held: str = "100"
) -> D:
    _, _, price = carried_price(
        repos,
        _ledger(repos),
        Held(held),
        account_id=account.account_id,
        instrument=instrument,
        on=on,
    )
    return price


def _buy(
    repos: Repositories, account: Account, instrument: Instrument, on: date, px: str
) -> int:
    return append(
        repos,
        account.account_id,
        TransactionType.BUY,
        on,
        instrument_id=instrument.instrument_id,
        quantity=D("1"),
        price=D(px),
    )


@pytest.mark.gips
def test_the_last_known_price_is_the_newest_observed(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """A stored price of 01-03, then a fill of 01-05: the fill. A stored
    price on the fill's day wins the tie. Two fills on one day: the later."""
    _price(repos, aapl, date(2024, 1, 3), "100")
    fill = _buy(repos, taxable_account, aapl, date(2024, 1, 5), "102")
    known = last_known(repos, _ledger(repos), aapl.instrument_id, date(2024, 1, 9))
    assert known is not None
    assert (known.price, known.on, known.source, known.source_id) == (
        D("102"), date(2024, 1, 5), CarrySource.FILL, fill,
    )  # fmt: skip

    stored = _price(repos, aapl, date(2024, 1, 5), "101.50")
    known = last_known(repos, _ledger(repos), aapl.instrument_id, date(2024, 1, 9))
    assert known is not None
    assert (known.price, known.source, known.source_id) == (
        D("101.50"),
        CarrySource.PRICE,
        stored,
    )

    _buy(repos, taxable_account, aapl, date(2024, 1, 8), "101")
    later = _buy(repos, taxable_account, aapl, date(2024, 1, 8), "103")
    known = last_known(repos, _ledger(repos), aapl.instrument_id, date(2024, 1, 9))
    assert known is not None
    assert (known.price, known.source_id) == (D("103"), later)


@pytest.mark.gips
def test_a_reinvestment_is_a_price_unless_tax_was_withheld(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """50.00 reinvested in 0.5 units is a purchase at 100. With tax withheld it
    is not used, and the price of 01-03 stands."""
    _price(repos, aapl, date(2024, 1, 3), "90")
    reinvested = append(repos, taxable_account.account_id, TransactionType.DIVIDEND_REINVEST,
                        date(2024, 1, 10), instrument_id=aapl.instrument_id,
                        quantity=D("0.5"), gross_amount=D("50.00"))  # fmt: skip
    known = last_known(repos, _ledger(repos), aapl.instrument_id, date(2024, 1, 12))
    assert known is not None
    assert (known.price, known.source, known.source_id) == (
        D("100"), CarrySource.REINVESTMENT, reinvested,
    )  # fmt: skip

    append(repos, taxable_account.account_id, TransactionType.DIVIDEND_REINVEST,
           date(2024, 1, 11), instrument_id=aapl.instrument_id, quantity=D("0.5"),
           gross_amount=D("60.00"), taxes_withheld=D("6.00"))  # fmt: skip
    known = last_known(repos, _ledger(repos), aapl.instrument_id, date(2024, 1, 12))
    assert known is not None
    assert known.source_id == reinvested


@pytest.mark.gips
def test_a_carry_never_crosses_a_corporate_action(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """A price of 01-03 and a split of 01-10. Unadjusted, the old price is
    twice the new one after a 2:1, so it is not carried past 01-10. A price
    after the split carries again."""
    _price(repos, aapl, date(2024, 1, 3), "100")
    append(repos, taxable_account.account_id, TransactionType.SPLIT, date(2024, 1, 10),
           instrument_id=aapl.instrument_id)  # fmt: skip
    assert _carry(repos, taxable_account, aapl, date(2024, 1, 9)) == D("100")
    with pytest.raises(PortableError) as refused:
        _carry(repos, taxable_account, aapl, date(2024, 1, 12))
    assert refused.value.code == "PT-E-ESTIMATE-ACROSS-ACTION"
    assert refused.value.context["action_date"] == "2024-01-10"

    _price(repos, aapl, date(2024, 1, 11), "50")
    assert _carry(repos, taxable_account, aapl, date(2024, 1, 12)) == D("50")


@pytest.mark.gips
def test_a_dividend_lowers_the_carry_from_the_day_the_snapshot_holds_it(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """50.00 last known on 01-10. A dividend of 100.00 on 100 units entitled,
    ex 01-15 and paid 01-20, is a receivable from 01-15, so the carry is 49
    from 01-15 and 50 before it."""
    _price(repos, aapl, date(2024, 1, 10), "50")
    append(repos, taxable_account.account_id, TransactionType.DIVIDEND, date(2024, 1, 20),
           instrument_id=aapl.instrument_id, gross_amount=D("100.00"),
           ex_date=date(2024, 1, 15))  # fmt: skip
    assert _carry(repos, taxable_account, aapl, date(2024, 1, 14)) == D("50")
    assert _carry(repos, taxable_account, aapl, date(2024, 1, 15)) == D("49")
    assert _carry(repos, taxable_account, aapl, date(2024, 1, 25)) == D("49")


@pytest.mark.gips
def test_a_dividend_with_no_ex_date_lowers_the_carry_from_its_pay_date(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """With no ex-date the row accrues nothing: its 100.00 arrives as cash on
    01-20. Lowering from an ex-date only would leave the price at 50 from
    01-20 on and count the dividend twice."""
    _price(repos, aapl, date(2024, 1, 10), "50")
    append(repos, taxable_account.account_id, TransactionType.DIVIDEND, date(2024, 1, 20),
           instrument_id=aapl.instrument_id, gross_amount=D("100.00"))  # fmt: skip
    assert _carry(repos, taxable_account, aapl, date(2024, 1, 19)) == D("50")
    assert _carry(repos, taxable_account, aapl, date(2024, 1, 20)) == D("49")


@pytest.mark.gips
def test_return_of_capital_and_a_capital_gain_lower_it_too_but_a_coupon_never(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """Each distribution the snapshot holds lowers the price by its gross per
    unit: 0.50 of capital and 0.25 of gain on 200 units. A coupon does not:
    a bond's price is clean (ADR 0041)."""
    _price(repos, aapl, date(2024, 1, 10), "50")
    for kind, amount in (
        (TransactionType.RETURN_OF_CAPITAL, "100.00"),
        (TransactionType.CAPITAL_GAIN_LT, "50.00"),
        (TransactionType.COUPON, "999.00"),
    ):
        append(repos, taxable_account.account_id, kind, date(2024, 1, 20),
               instrument_id=aapl.instrument_id, gross_amount=D(amount))  # fmt: skip
    assert _carry(repos, taxable_account, aapl, date(2024, 1, 21), held="200") == D("49.25")


@pytest.mark.gips
def test_a_carry_lowered_to_nothing_is_refused(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """0.50 carried, then 1.00 a unit distributed: there is no price left."""
    _price(repos, aapl, date(2024, 1, 10), "0.50")
    append(repos, taxable_account.account_id, TransactionType.DIVIDEND, date(2024, 1, 20),
           instrument_id=aapl.instrument_id, gross_amount=D("100.00"))  # fmt: skip
    with pytest.raises(PortableError) as refused:
        _carry(repos, taxable_account, aapl, date(2024, 1, 21))
    assert refused.value.code == "PT-E-ESTIMATE-UNAVAILABLE"


@pytest.mark.gips
def test_nothing_is_estimated_from_nothing(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    with pytest.raises(PortableError) as refused:
        _carry(repos, taxable_account, aapl, date(2024, 1, 21))
    assert refused.value.code == "PT-E-ESTIMATE-UNAVAILABLE"


class _Days:
    """A walk's estimator that values every day at one figure, one holding carried."""

    def __init__(self, value: str, carried: str) -> None:
        self.day = AccountDay(
            value=D(value),
            carried=(
                CarriedHolding(
                    account="B", symbol="XYZ", quantity=D(100), price=D(1), last_known=D(1),
                    price_date=date(2024, 1, 2), age_days=8, source=CarrySource.PRICE,
                    source_id=1, lowered_by=D(0), value=D(carried),
                ),
            ),
        )  # fmt: skip

    def load(self, days: object) -> None:
        pass

    def on(self, day: date) -> dict[int, AccountDay]:
        return {1: self.day}


def _estimated(value: str, carried: str, limit: str = "0.05") -> object:
    walked = cast(Walk, SimpleNamespace(estimator=_Days(value, carried), account=None))
    return returns._estimate(walked, [date(2024, 1, 10)], D(limit))


@pytest.mark.gips
@pytest.mark.parametrize("value", ["0.00", "-250.00"])
def test_a_day_worth_nothing_or_less_has_no_share_and_is_refused(value: str) -> None:
    """ADR 0044 §4, as level5_share is null there: a share of a value that is
    not positive is no share at all, whatever the limit."""
    with pytest.raises(PortableError) as refused:
        _estimated(value, "100.00", limit="1")
    assert refused.value.code == "PT-E-ESTIMATE-LIMIT"
    assert refused.value.context["value"] == value


@pytest.mark.gips
def test_a_share_is_of_the_carried_value_unsigned() -> None:
    """A short carried at -100 on a day worth 2,000 is 5% of it, at the limit."""
    (day,) = cast(dict[date, EstimatedDay], _estimated("2000.00", "-100.00")).values()
    assert (day.share, day.holdings[0].share) == (D("0.05"), D("0.05"))
    with pytest.raises(PortableError) as refused:
        _estimated("2000.00", "-100.01")
    assert refused.value.code == "PT-E-ESTIMATE-LIMIT"
