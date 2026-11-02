"""What a valuation snapshot accrues, and what it says it could not value. ADR 0035.

Built through :func:`snapshots.value_range`, the path `pt value` takes, so
these assert what is stored rather than what the engine can compute when
handed the right numbers -- which it always could, and which is why the
snapshot path went untested.

GIPS acceptance tests:
    test_port_gips_a06_a_dividend_accrues_from_its_ex_date_to_its_pay_date
    test_port_gips_a06_a_coupon_bond_makes_its_snapshot_incomplete_and_says_why
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from portable_core.domain.enums import DayCount, FeeClass, InstrumentType, TransactionType
from portable_core.domain.models import (
    Account,
    BondDetail,
    Instrument,
    Price,
    Transaction,
    ValuationSnapshot,
)
from portable_core.persistence.repositories import Repositories
from portable_core.services import snapshots
from portable_core.services.replay import ReplayEngine
from portable_core.services.trading import TradingService
from portable_core.services.valuation import Holding, ValuationEngine
from tests.conftest import append

pytestmark = pytest.mark.unit

D = Decimal
START = date(2024, 3, 1)
EX = date(2024, 3, 4)
PAY = date(2024, 3, 7)


def _prices(repos: Repositories, instrument: Instrument, value: str, days: int = 10) -> None:
    for offset in range(days):
        repos.prices.add(
            Price(
                instrument_id=instrument.instrument_id,
                price_date=START + timedelta(days=offset),
                price=D(value),
                source="manual",
                as_of=datetime(2024, 3, 1, 21, tzinfo=UTC),
            )
        )


def _hold(repos: Repositories, account: Account, instrument: Instrument, quantity: str) -> None:
    """Fund *account* on START and buy *quantity* at 100."""
    gross = D(quantity) * D("100")
    append(repos, account.account_id, TransactionType.DEPOSIT, START,
           net_cash_effect=gross + D("1000.00"))  # fmt: skip
    append(repos, account.account_id, TransactionType.BUY, START,
           instrument_id=instrument.instrument_id, quantity=D(quantity), price=D("100"),
           gross_amount=gross, net_cash_effect=-gross)  # fmt: skip


def _series(
    repos: Repositories, account: Account, first: date, last: date
) -> dict[date, ValuationSnapshot]:
    built, _ = snapshots.value_range(repos, [account], first, last, staleness_tolerance_days=5)
    return {snapshot.snapshot_date: snapshot for snapshot in built}


def _record_dividend(
    repos: Repositories, account: Account, instrument: Instrument, *, ex_date: date | None
) -> int:
    txn = TradingService(repos).record_income(
        account,
        instrument,
        TransactionType.DIVIDEND,
        D("100.00"),
        PAY,
        ex_date=ex_date,
        taxes_withheld=D("15.00"),
    )
    return repos.transactions.append(txn)


@pytest.mark.gips
def test_port_gips_a06_a_dividend_accrues_from_its_ex_date_to_its_pay_date(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """PORT-GIPS-A06 (B): entitlement on the ex-date, cash on the pay-date.

    The accrual is what the pay-date delivers, the gross less the tax withheld,
    so the pay-date itself shows no change: the receivable becomes cash.
    """
    _hold(repos, taxable_account, aapl, "10")
    _prices(repos, aapl, "100.00")
    _record_dividend(repos, taxable_account, aapl, ex_date=EX)
    ReplayEngine(repos).rebuild()

    series = _series(repos, taxable_account, START, PAY + timedelta(days=1))

    assert series[EX - timedelta(days=1)].accrued_dividends == D("0.00")
    for on in (EX, EX + timedelta(days=1), PAY - timedelta(days=1)):
        assert series[on].accrued_dividends == D("85.00"), on
        assert series[on].accrued_income == D("85.00"), on
    assert series[PAY].accrued_dividends == D("0.00")
    # The receivable became cash: nothing moved on the pay-date.
    eve = PAY - timedelta(days=1)
    assert series[PAY].ending_market_value == series[eve].ending_market_value
    # And the ex-date carries the distribution, not the pay-date.
    assert series[EX].ending_market_value - series[START].ending_market_value == D("85.00")
    assert series[PAY].income_amount == D("85.00")
    assert series[EX].income_amount == D("0.00")


def test_a_distribution_with_no_ex_date_is_recognised_when_it_is_paid(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """An imported distribution carries no ex-date, so it accrues nothing (ADR 0035).

    Its ex-date is stored as its pay-date, and the whole amount arrives on the
    day it is paid. That is the honest reading of a row that says nothing
    about entitlement -- not an accrual guessed from a typical lag.
    """
    _hold(repos, taxable_account, aapl, "10")
    _prices(repos, aapl, "100.00")
    _record_dividend(repos, taxable_account, aapl, ex_date=None)
    ReplayEngine(repos).rebuild()

    series = _series(repos, taxable_account, START, PAY)

    assert all(snapshot.accrued_dividends == D("0.00") for snapshot in series.values())
    eve = PAY - timedelta(days=1)
    assert series[PAY].ending_market_value - series[eve].ending_market_value == D("85.00")


def test_recording_a_distribution_removes_the_snapshots_from_its_ex_date(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """A distribution is recorded when it is paid; the snapshots since its ex-date accrue it.

    So recording it changes them, and ADR 0024 requires every snapshot a
    write changes to be removed rather than left describing a book that no
    longer exists.
    """
    _hold(repos, taxable_account, aapl, "10")
    _prices(repos, aapl, "100.00")
    ReplayEngine(repos).rebuild()
    built, _ = snapshots.value_range(
        repos, [taxable_account], START, PAY - timedelta(days=1), staleness_tolerance_days=5
    )
    snapshots.store(repos, built)

    _record_dividend(repos, taxable_account, aapl, ex_date=EX)

    [removed] = repos.transactions.removed_snapshots
    assert (removed.first, removed.last) == (EX, PAY - timedelta(days=1))
    kept = {on for _, _, on in repos.valuations.stored_snapshots()}
    assert kept == {START + timedelta(days=n) for n in range((EX - START).days)}


def test_a_snapshot_records_the_income_received_and_the_costs_paid(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """Both were always zero, whatever happened that day.

    Costs are every class together, positive when charged: a memo, which a
    return basis cannot be derived from (PORT-GIPS-D01). Deposits are neither.
    """
    _hold(repos, taxable_account, aapl, "10")
    _prices(repos, aapl, "100.00")
    _record_dividend(repos, taxable_account, aapl, ex_date=None)
    append(repos, taxable_account.account_id, TransactionType.FEE, PAY,
           net_cash_effect=D("-25.00"), fee_class=FeeClass.OTHER_ADMIN)  # fmt: skip
    append(repos, taxable_account.account_id, TransactionType.MARGIN_INTEREST, PAY,
           net_cash_effect=D("-4.10"))  # fmt: skip
    ReplayEngine(repos).rebuild()

    series = _series(repos, taxable_account, START, PAY)

    assert series[PAY].income_amount == D("85.00")
    assert series[PAY].fees_amount == D("29.10")
    assert series[START].income_amount == D("0.00")
    assert series[START].fees_amount == D("0.00")
    assert snapshots.verify(repos) == []


@pytest.mark.gips
def test_port_gips_a06_a_coupon_bond_makes_its_snapshot_incomplete_and_says_why(
    repos: Repositories, taxable_account: Account
) -> None:
    """PORT-GIPS-A06 (A) requires accrued interest; `portable` cannot yet supply it.

    The ledger does not record the accrued interest a buyer pays, so an
    accrual in market value would be booked as a gain the day the bond is
    bought. Until it does, the snapshot is unfit for a return and says so
    (ADR 0035) -- rather than valuing the bond at its clean price and calling
    the snapshot complete, which is what a zero accrual did.
    """
    bond = _bond(repos, coupon=D("0.0425"), frequency=2)
    _hold(repos, taxable_account, bond, "10")
    _prices(repos, bond, "99.50")
    ReplayEngine(repos).rebuild()

    [snapshot] = _series(repos, taxable_account, START, START).values()

    assert snapshot.is_complete is False
    assert snapshot.unvalued == (
        "T-4.25-2030: accrued interest on a coupon bond is not modelled yet, so its "
        "market value would omit it (ADR 0035)",
    )
    # Still valued at its price: incomplete is a statement about the return,
    # not a refusal to say what the holding is worth.
    assert snapshot.securities_value == D("995.00")


def test_a_zero_coupon_bond_has_no_accrual_to_omit(
    repos: Repositories, taxable_account: Account
) -> None:
    """Its accretion is in its price, so its snapshot is complete."""
    bond = _bond(repos, coupon=D("0"), frequency=0)
    _hold(repos, taxable_account, bond, "10")
    _prices(repos, bond, "97.00")
    ReplayEngine(repos).rebuild()

    [snapshot] = _series(repos, taxable_account, START, START).values()

    assert snapshot.is_complete is True
    assert snapshot.unvalued == ()


def test_a_missing_price_is_named_on_the_snapshot(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """`pt value` said "some positions could not be priced" and named none."""
    _hold(repos, taxable_account, aapl, "10")
    ReplayEngine(repos).rebuild()

    [snapshot] = _series(repos, taxable_account, START, START).values()

    assert snapshot.is_complete is False
    assert snapshot.unvalued == ("AAPL: no price for AAPL on or before 2024-03-01",)


def test_a_distribution_without_a_pay_date_stops_accruing_on_its_own_date() -> None:
    """Otherwise a row that never said when it was paid would be owed for ever."""
    row = Transaction(
        txn_id=1,
        account_id=1,
        trade_date=PAY,
        seq=1,
        txn_type=TransactionType.DIVIDEND,
        net_cash_effect=D("50.00"),
        gross_amount=D("50.00"),
        ex_date=EX,
    )
    engine = ValuationEngine()
    assert engine.accrued_dividends([row], PAY - timedelta(days=1)) == D("50.00")
    assert engine.accrued_dividends([row], PAY) == D("0.00")
    assert engine.accrued_dividends([row], PAY + timedelta(days=30)) == D("0.00")


def test_accrued_interest_without_coupon_dates_is_refused_not_zero() -> None:
    """Zero is a plausible accrual and a wrong one (`CLAUDE.md` invariant 10)."""
    instrument = Instrument(
        instrument_id=1,
        symbol="T-4.25-2030",
        instrument_type=InstrumentType.BOND,
        bond=BondDetail(
            issuer="US Treasury",
            coupon_rate=D("0.0425"),
            coupon_frequency=2,
            maturity_date=date(2030, 5, 15),
            day_count=DayCount.ACT_ACT,
            face_value=D("1000"),
        ),
    )
    with pytest.raises(ValueError, match="coupon dates"):
        ValuationEngine().accrued_interest(Holding(instrument, D("10")), date(2024, 6, 30))


def _bond(repos: Repositories, *, coupon: Decimal, frequency: int) -> Instrument:
    instrument_id = repos.instruments.add(
        Instrument(
            instrument_id=0,
            symbol="T-4.25-2030" if frequency else "STRIP-2030",
            instrument_type=InstrumentType.BOND,
            bond=BondDetail(
                issuer="US Treasury",
                coupon_rate=coupon,
                coupon_frequency=frequency,
                maturity_date=date(2030, 5, 15),
                day_count=DayCount.ACT_ACT,
                face_value=D("1000"),
            ),
        )
    )
    instrument = repos.instruments.get(instrument_id)
    assert instrument is not None
    return instrument
