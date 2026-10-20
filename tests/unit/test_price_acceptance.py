"""Price history accepted against the custodian's statements. ADR 0031.

A price ties when it agrees with the statement's (market value over quantity)
to a cent a unit, plus the half cent the statement's rounding of market value
accounts for. A disagreement is accepted only with a reason, and only while
the figures it accepted are unchanged. An unresolved line blocks returns.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from portable_core.domain.enums import InstrumentType, OptionRight
from portable_core.domain.models import Account, Instrument, OptionDetail, Price
from portable_core.errors import ValidationError
from portable_core.persistence.repositories import Repositories
from portable_core.services import price_acceptance as pa
from portable_core.services.price_acceptance import Status

pytestmark = pytest.mark.unit

D = Decimal
YEAR_END = date(2022, 12, 31)  # a Saturday: valued from Friday's close


def _price(repos: Repositories, instrument: Instrument, on: date, value: str) -> None:
    repos.prices.add(
        Price(
            instrument_id=instrument.instrument_id,
            price_date=on,
            price=D(value),
            source="fafnir:duk ph",
            as_of=datetime.combine(on, datetime.min.time(), tzinfo=UTC),
        )
    )


def _check(
    repos: Repositories,
    account: Account,
    instrument: Instrument,
    quantity: str,
    market_value: str,
    *,
    on: date = YEAR_END,
) -> pa.PriceLine:
    return pa.check(
        repos,
        account,
        instrument,
        on,
        D(quantity),
        D(market_value),
        staleness_days=5,
        recorded=repos.statements.get(account.account_id, on, instrument.instrument_id),
    )


@pytest.fixture
def priced(repos: Repositories, aapl: Instrument) -> Instrument:
    _price(repos, aapl, date(2022, 12, 30), "129.93")
    return aapl


@pytest.mark.parametrize(
    ("quantity", "market_value", "status"),
    [
        ("10", "1299.30", Status.TIED),  # exactly
        ("10", "1299.20", Status.TIED),  # a cent a unit, plus the rounding half cent
        ("10", "1299.19", Status.BREAK),  # just past it
        ("0.5", "64.97", Status.TIED),  # 64.965 rounded half up by the custodian
        ("0.5", "64.98", Status.BREAK),
    ],
)
def test_a_price_ties_to_the_cent_a_unit_and_no_further(
    repos: Repositories,
    taxable_account: Account,
    priced: Instrument,
    quantity: str,
    market_value: str,
    status: Status,
) -> None:
    line = _check(repos, taxable_account, priced, quantity, market_value)

    assert line.status is status
    assert line.price_date == date(2022, 12, 30), "a Saturday is valued from Friday's close"


def test_a_price_from_another_day_is_a_break_however_large_the_position(
    repos: Repositories, taxable_account: Account, priced: Instrument
) -> None:
    """0.4% off: a percentage-of-value tolerance would have passed it."""
    line = _check(repos, taxable_account, priced, "1000", "130450.00")  # 130.45 stated

    assert line.status is Status.BREAK
    assert line.stated_price == D("130.450000")
    assert line.difference == D("-520.00")


def test_a_quantity_disagreement_is_not_also_a_price_break(
    repos: Repositories, taxable_account: Account, priced: Instrument
) -> None:
    """Compared on the statement's quantity, so the price question stands alone."""
    assert _check(repos, taxable_account, priced, "7", "909.51").status is Status.TIED


def test_no_price_or_a_stale_one_is_unpriced_and_unresolved(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """An unchecked price is not an agreeing one."""
    missing = _check(repos, taxable_account, aapl, "10", "1299.30")
    assert missing.status is Status.UNPRICED and missing.is_unresolved
    assert "no price on or before 2022-12-31" in missing.note

    _price(repos, aapl, date(2022, 12, 20), "130.00")
    stale = _check(repos, taxable_account, aapl, "10", "1300.00")
    assert stale.status is Status.UNPRICED
    assert "11 days old" in stale.note


def test_an_instrument_with_no_market_price_is_compared_with_zero(
    repos: Repositories, taxable_account: Account
) -> None:
    """ADR 0028: valued at zero. A custodian's non-zero figure is a break to explain."""
    cvr_id = repos.instruments.add(
        Instrument(
            instrument_id=0,
            symbol="003CVR016",
            instrument_type=InstrumentType.EQUITY,
            has_market_price=False,
        )
    )
    cvr = repos.instruments.get(cvr_id)
    assert cvr is not None

    assert _check(repos, taxable_account, cvr, "100", "0.00").status is Status.TIED
    assert _check(repos, taxable_account, cvr, "100", "35.00").status is Status.BREAK


def test_an_option_is_priced_per_share_and_held_per_contract(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    option_id = repos.instruments.add(
        Instrument(
            instrument_id=0,
            symbol="AAPL230120C00150000",
            instrument_type=InstrumentType.OPTION,
            option=OptionDetail(
                underlier_instrument_id=aapl.instrument_id,
                option_right=OptionRight.CALL,
                strike=D("150"),
                expiry=date(2023, 1, 20),
                multiplier=D("100"),
            ),
        )
    )
    option = repos.instruments.get(option_id)
    assert option is not None
    _price(repos, option, date(2022, 12, 30), "1.50")

    line = _check(repos, taxable_account, option, "2", "300.00")

    assert line.status is Status.TIED
    assert line.stated_price == D("1.500000")


def _record(
    repos: Repositories, account: Account, instrument: Instrument, quantity: str, value: str
) -> bool:
    return pa.record(
        repos, account, instrument, YEAR_END, D(quantity), D(value), source="2022-12.csv"
    )


def test_an_acceptance_holds_only_for_the_figures_it_accepted(
    repos: Repositories, taxable_account: Account, priced: Instrument
) -> None:
    """A reloaded price, or a re-recorded statement, is a new disagreement."""
    assert _record(repos, taxable_account, priced, "10", "1310.00") is True
    assert _record(repos, taxable_account, priced, "10", "1310.00") is False, "unchanged"

    accepted = pa.accept(
        repos, taxable_account, priced, YEAR_END, " custodian's print ", staleness_days=5
    )
    assert accepted.status is Status.ACCEPTED
    assert accepted.note == "custodian's print"

    _price(repos, priced, date(2022, 12, 30), "129.95")  # the same source corrects itself
    assert _check(repos, taxable_account, priced, "10", "1310.00").status is Status.BREAK

    _price(repos, priced, date(2022, 12, 30), "129.93")  # and back: the acceptance holds
    assert _check(repos, taxable_account, priced, "10", "1310.00").status is Status.ACCEPTED

    assert _record(repos, taxable_account, priced, "10", "1311.00") is True
    assert _check(repos, taxable_account, priced, "10", "1311.00").status is Status.BREAK


def test_only_a_recorded_break_can_be_accepted_and_only_with_a_reason(
    repos: Repositories, taxable_account: Account, priced: Instrument, aapl: Instrument
) -> None:
    def accept(reason: str = "evaluated price") -> pa.PriceLine:
        return pa.accept(repos, taxable_account, priced, YEAR_END, reason, staleness_days=5)

    with pytest.raises(ValidationError, match="no statement line is recorded"):
        accept()

    _record(repos, taxable_account, priced, "10", "1299.30")
    with pytest.raises(ValidationError, match="is tied; there is nothing to accept"):
        accept()

    _record(repos, taxable_account, priced, "10", "1310.00")
    with pytest.raises(ValidationError, match="needs a reason"):
        accept("   ")
    accept()
    with pytest.raises(ValidationError, match="is accepted; there is nothing to accept"):
        accept()


def test_an_unpriced_line_cannot_be_accepted(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """There is no price to accept: load or set one first."""
    _record(repos, taxable_account, aapl, "10", "1299.30")

    with pytest.raises(ValidationError, match="is unpriced"):
        pa.accept(repos, taxable_account, aapl, YEAR_END, "whatever", staleness_days=5)


def test_returns_are_blocked_from_each_accounts_earliest_unresolved_line(
    repos: Repositories, taxable_account: Account, ira_account: Account, priced: Instrument
) -> None:
    _price(repos, priced, date(2023, 12, 29), "192.53")
    pa.record(repos, taxable_account, priced, YEAR_END, D("10"), D("1310.00"), source="s")
    pa.record(
        repos, taxable_account, priced, date(2023, 12, 31), D("10"), D("1999.99"), source="s"
    )
    pa.record(repos, ira_account, priced, date(2023, 12, 31), D("1"), D("192.53"), source="s")

    blocked = pa.blocked_from(pa.check_recorded(repos, staleness_days=5))
    assert {name: line.statement_date for name, line in blocked.items()} == {
        "Brokerage": YEAR_END
    }, "the IRA ties; Brokerage is blocked from its first break"

    pa.accept(repos, taxable_account, priced, YEAR_END, "evaluated", staleness_days=5)
    blocked = pa.blocked_from(pa.check_recorded(repos, staleness_days=5))
    assert blocked["Brokerage"].statement_date == date(2023, 12, 31)
