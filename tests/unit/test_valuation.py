"""The valuation engine: accruals, leverage, cash, and refusals.

GIPS acceptance tests:
    test_accrued_interest_in_market_value      (PORT-GIPS-A06)
    test_dividend_accrues_on_ex_date           (PORT-GIPS-A06)
    test_cash_drag_is_reflected                (PORT-GIPS-A07)
    test_market_value_net_of_margin_loan       (PORT-GIPS-D04)
    test_every_snapshot_price_has_source_and_asof (PORT-GIPS-J03)
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from portable_core.domain.enums import (
    AccountType,
    CashTreatment,
    DayCount,
    InstrumentType,
    TransactionType,
    ValuationBasis,
)
from portable_core.domain.models import (
    Account,
    BondDetail,
    Instrument,
    Price,
    Transaction,
)
from portable_core.errors import DataUnavailableError
from portable_core.services.valuation import Holding, ValuationEngine

pytestmark = pytest.mark.unit

D = Decimal
AS_OF = datetime(2025, 6, 30, 21, 0, tzinfo=UTC)
ENGINE = ValuationEngine()


def account(
    *, cash_treatment: CashTreatment = CashTreatment.INVESTED, account_id: int = 1
) -> Account:
    return Account(
        account_id=account_id,
        name="Brokerage",
        account_type=AccountType.TAXABLE,
        opened_date=date(2024, 1, 1),
        cash_treatment=cash_treatment,
    )


def equity(instrument_id: int = 1, symbol: str = "AAPL") -> Instrument:
    return Instrument(
        instrument_id=instrument_id, symbol=symbol, instrument_type=InstrumentType.EQUITY
    )


def price(
    value: str,
    *,
    on: date = date(2025, 6, 30),
    level: int = 1,
    estimate: bool = False,
    source: str = "fafnir:core.daily_price",
) -> Price:
    return Price(
        instrument_id=1,
        price_date=on,
        price=D(value),
        source=source,
        as_of=AS_OF,
        valuation_level=level,
        valuation_basis=ValuationBasis.EXCHANGE_CLOSE,
        is_estimate=estimate,
        price_id=1,
    )


# ── pricing and its refusals ─────────────────────────────────────────────────


def test_market_value_uses_the_instruments_contract_size() -> None:
    """The option multiplier is read, never assumed. A wrong one is 100x wrong."""
    from portable_core.domain.models import OptionDetail

    option = Instrument(
        instrument_id=1,
        symbol="AAPL  250718C00200000",
        instrument_type=InstrumentType.OPTION,
        option=OptionDetail(
            underlier_instrument_id=2,
            option_right="call",  # type: ignore[arg-type]
            strike=D("200"),
            expiry=date(2025, 7, 18),
            multiplier=D("100"),
        ),
    )
    priced = ENGINE.price_holding(Holding(option, D("2")), price("3.25"), date(2025, 6, 30))
    assert priced.market_value == D("650.00"), "2 contracts x 100 x 3.25"


def test_a_missing_price_stops_the_command() -> None:
    """CLAUDE.md invariant 9, and exit code 5."""
    with pytest.raises(DataUnavailableError) as exc:
        ENGINE.price_holding(Holding(equity(), D("100")), None, date(2025, 6, 30))
    assert exc.value.code == "PT-E-PRICE-MISSING"
    assert exc.value.exit_code == 5


def test_a_stale_price_is_refused_rather_than_carried_forward() -> None:
    """Carrying it forward produces a flat series that looks like a calm market."""
    with pytest.raises(DataUnavailableError) as exc:
        ENGINE.price_holding(
            Holding(equity(), D("100")),
            price("190.00", on=date(2025, 6, 1)),
            date(2025, 6, 30),
        )
    assert exc.value.code == "PT-E-PRICE-STALE"
    assert exc.value.context["staleness_days"] == 29
    assert "will not carry a stale price forward" in (exc.value.remedy or "")


def test_a_price_within_tolerance_records_its_staleness() -> None:
    """A weekend or a holiday is a real gap, not an error -- but it is recorded."""
    priced = ENGINE.price_holding(
        Holding(equity(), D("100")),
        price("190.00", on=date(2025, 6, 27)),
        date(2025, 6, 30),
    )
    assert priced.staleness_days == 3
    assert priced.market_value == D("19000.00")


@pytest.mark.gips
def test_every_snapshot_price_has_source_and_asof() -> None:
    """PORT-GIPS-J03 -- supporting data for every reported figure.

    Including records obtained from third parties, which is why the source
    string names the warehouse table rather than merely "fafnir".
    """
    priced = ENGINE.price_holding(
        Holding(equity(), D("100")), price("190.00"), date(2025, 6, 30)
    )
    snapshot = ENGINE.build_snapshot(
        account(), date(2025, 6, 30), priced=[priced], cash_balance=D("1000.00")
    )
    assert snapshot.prices
    for entry in snapshot.prices:
        assert entry.source
        assert entry.as_of is not None
        assert 1 <= entry.valuation_level <= 5
        assert entry.price_id is not None


# ── accruals ─────────────────────────────────────────────────────────────────


@pytest.mark.gips
def test_accrued_interest_in_market_value() -> None:
    """PORT-GIPS-A06 -- accrued interest is part of value, not a memo.

    Omitting it understates market value between coupons and produces a
    sawtooth return series that looks like the bond is oscillating.
    """
    bond = Instrument(
        instrument_id=1,
        symbol="T 4.25 2030",
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
    holding = Holding(
        bond,
        D("10"),
        last_coupon_date=date(2025, 5, 15),
        next_coupon_date=date(2025, 11, 15),
    )
    accrued = ENGINE.accrued_interest(holding, date(2025, 6, 30))
    assert accrued > 0, "46 days into a semi-annual period must accrue something"

    snapshot = ENGINE.build_snapshot(
        account(),
        date(2025, 6, 30),
        priced=[ENGINE.price_holding(holding, price("99.50"), date(2025, 6, 30))],
        cash_balance=D("0.00"),
        accrued_interest=accrued,
    )
    assert snapshot.accrued_income == accrued
    assert snapshot.ending_market_value == snapshot.securities_value + accrued


def test_a_zero_coupon_bond_accrues_no_interest() -> None:
    bond = Instrument(
        instrument_id=1,
        symbol="ZERO 2030",
        instrument_type=InstrumentType.BOND,
        bond=BondDetail(
            issuer="Corp",
            coupon_rate=D("0"),
            coupon_frequency=0,
            maturity_date=date(2030, 1, 1),
            day_count=DayCount.ACT_365,
            face_value=D("1000"),
        ),
    )
    assert ENGINE.accrued_interest(Holding(bond, D("10")), date(2025, 6, 30)) == D("0.00")


@pytest.mark.gips
def test_dividend_accrues_on_ex_date() -> None:
    """PORT-GIPS-A06 -- entitlement on the ex-date, cash on the pay-date.

    A dividend whose ex-date has passed and whose pay-date has not is owed to
    the portfolio and belongs in ending market value. Accruing on the pay-date
    instead moves the return into the next period, which shows up as one good
    quarter and one bad one rather than as an error.
    """
    declared = [
        Transaction(
            txn_id=1,
            account_id=1,
            trade_date=date(2025, 6, 10),
            seq=1,
            txn_type=TransactionType.DIVIDEND,
            net_cash_effect=D("0.00"),
            gross_amount=D("240.00"),
            ex_date=date(2025, 6, 10),
            pay_date=date(2025, 7, 15),
        ),
        Transaction(  # ex-date in the future: not yet entitled
            txn_id=2,
            account_id=1,
            trade_date=date(2025, 9, 10),
            seq=1,
            txn_type=TransactionType.DIVIDEND,
            net_cash_effect=D("0.00"),
            gross_amount=D("250.00"),
            ex_date=date(2025, 9, 10),
            pay_date=date(2025, 10, 15),
        ),
        Transaction(  # already paid: no longer a receivable
            txn_id=3,
            account_id=1,
            trade_date=date(2025, 3, 10),
            seq=1,
            txn_type=TransactionType.DIVIDEND,
            net_cash_effect=D("230.00"),
            gross_amount=D("230.00"),
            ex_date=date(2025, 3, 10),
            pay_date=date(2025, 4, 15),
        ),
    ]
    assert ENGINE.accrued_dividends(declared, date(2025, 6, 30)) == D("240.00")


# ── cash, leverage, and operating cash ───────────────────────────────────────


@pytest.mark.gips
def test_cash_drag_is_reflected() -> None:
    """PORT-GIPS-A07 -- returns from cash are in all return calculations.

    A portfolio half in cash over a period in which equities rise shows
    approximately half the equity return, not the equity return. There is no
    ex-cash basis.
    """
    start = ENGINE.build_snapshot(
        account(),
        date(2025, 1, 1),
        priced=[
            ENGINE.price_holding(
                Holding(equity(), D("100")),
                price("100.00", on=date(2025, 1, 1)),
                date(2025, 1, 1),
            )
        ],
        cash_balance=D("10000.00"),
    )
    end = ENGINE.build_snapshot(
        account(),
        date(2025, 6, 30),
        priced=[
            ENGINE.price_holding(
                Holding(equity(), D("100")), price("120.00"), date(2025, 6, 30)
            )
        ],
        cash_balance=D("10000.00"),
        beginning_market_value=start.ending_market_value,
    )

    assert start.ending_market_value == D("20000.00")
    assert end.ending_market_value == D("22000.00")
    total_return = (end.ending_market_value / start.ending_market_value) - 1
    assert total_return == D("0.1"), "20% on equities, 10% on the portfolio"


def test_operating_cash_is_excluded_only_by_the_explicit_flag() -> None:
    """PORT-GIPS-A07 / AO 22.B.9. Never an implicit exclusion."""
    priced = [
        ENGINE.price_holding(Holding(equity(), D("100")), price("100.00"), date(2025, 6, 30))
    ]
    invested = ENGINE.build_snapshot(
        account(), date(2025, 6, 30), priced=priced, cash_balance=D("5000.00")
    )
    operating = ENGINE.build_snapshot(
        account(cash_treatment=CashTreatment.OPERATING),
        date(2025, 6, 30),
        priced=priced,
        cash_balance=D("5000.00"),
    )
    assert invested.ending_market_value == D("15000.00")
    assert operating.ending_market_value == D("10000.00")
    # The balance is still recorded either way: excluded from the return, not
    # from the record.
    assert operating.cash_balance == D("5000.00")


@pytest.mark.gips
def test_market_value_net_of_margin_loan() -> None:
    """PORT-GIPS-D04 -- assets are not grossed up as if leverage did not exist."""
    priced = [
        ENGINE.price_holding(Holding(equity(), D("100")), price("200.00"), date(2025, 6, 30))
    ]
    snapshot = ENGINE.build_snapshot(
        account(),
        date(2025, 6, 30),
        priced=priced,
        cash_balance=D("0.00"),
        margin_loan=D("8000.00"),
    )
    assert snapshot.securities_value == D("20000.00")
    assert snapshot.ending_market_value == D("12000.00")


# ── flows into the snapshot ──────────────────────────────────────────────────


def test_a_transfer_appears_at_account_level_and_not_at_portfolio_level() -> None:
    """The classification is not re-derived here. ADR 0007."""
    transfer = Transaction(
        txn_id=1,
        account_id=1,
        trade_date=date(2025, 6, 30),
        seq=1,
        txn_type=TransactionType.TRANSFER,
        net_cash_effect=D("-50000.00"),
        counter_account_id=2,
    )
    snapshot = ENGINE.build_snapshot(
        account(),
        date(2025, 6, 30),
        priced=[],
        cash_balance=D("50000.00"),
        transactions=[transfer],
    )
    assert snapshot.external_flow_account == D("-50000.00")
    assert snapshot.external_flow_portfolio == D("0.00")


def test_income_never_reaches_the_flow_series() -> None:
    dividend = Transaction(
        txn_id=1,
        account_id=1,
        trade_date=date(2025, 6, 30),
        seq=1,
        txn_type=TransactionType.DIVIDEND,
        net_cash_effect=D("240.00"),
        gross_amount=D("240.00"),
    )
    snapshot = ENGINE.build_snapshot(
        account(),
        date(2025, 6, 30),
        priced=[],
        cash_balance=D("240.00"),
        transactions=[dividend],
    )
    assert snapshot.external_flow_account == D("0.00")
    assert snapshot.external_flow_portfolio == D("0.00")
    assert snapshot.flows == ()


def test_a_large_flow_is_marked_against_the_policy_in_force() -> None:
    """PORT-GIPS-B03. The threshold is a stored policy, never a default."""
    from portable_core.domain.models import ReturnPolicy

    policy = ReturnPolicy(
        policy_id=1,
        effective_from=date(2024, 1, 1),
        large_flow_basis="percent",
        large_flow_value=D("0.10"),
    )
    deposit = Transaction(
        txn_id=1,
        account_id=1,
        trade_date=date(2025, 6, 30),
        seq=1,
        txn_type=TransactionType.DEPOSIT,
        net_cash_effect=D("50000.00"),
    )
    snapshot = ENGINE.build_snapshot(
        account(),
        date(2025, 6, 30),
        priced=[],
        cash_balance=D("100000.00"),
        transactions=[deposit],
        policy=policy,
        beginning_market_value=D("100000.00"),
    )
    assert snapshot.flows[0].is_large is True


# ── level 5 ──────────────────────────────────────────────────────────────────


def test_level_five_percentage_is_rendered_even_when_zero() -> None:
    """PORT-GIPS-H05 -- "0%" is information; a missing row is not."""
    snapshot = ENGINE.build_snapshot(
        account(),
        date(2025, 6, 30),
        priced=[
            ENGINE.price_holding(
                Holding(equity(), D("100")), price("100.00"), date(2025, 6, 30)
            )
        ],
        cash_balance=D("0.00"),
    )
    assert snapshot.level5_market_value == D("0.00")
    assert ENGINE.level5_percentage(snapshot) == D("0.00")


def test_a_manually_priced_holding_lands_at_level_five() -> None:
    """A price entered by hand with no documented basis is unobservable input."""
    snapshot = ENGINE.build_snapshot(
        account(),
        date(2025, 6, 30),
        priced=[
            ENGINE.price_holding(
                Holding(equity(), D("100")),
                price("100.00", level=5, source="manual"),
                date(2025, 6, 30),
            )
        ],
        cash_balance=D("0.00"),
    )
    assert snapshot.level5_market_value == D("10000.00")
    assert ENGINE.level5_percentage(snapshot) == D("1")


def test_a_snapshot_using_an_estimate_says_so() -> None:
    """PORT-GIPS-A09 / I10 -- preliminary values must be flagged."""
    snapshot = ENGINE.build_snapshot(
        account(),
        date(2025, 6, 30),
        priced=[
            ENGINE.price_holding(
                Holding(equity(), D("100")), price("100.00", estimate=True), date(2025, 6, 30)
            )
        ],
        cash_balance=D("0.00"),
    )
    assert snapshot.uses_estimates is True


def test_an_incomplete_snapshot_is_marked_incomplete() -> None:
    """A snapshot that could not price everything must not be used for a return."""
    snapshot = ENGINE.build_snapshot(
        account(),
        date(2025, 6, 30),
        priced=[],
        cash_balance=D("0.00"),
        incomplete_symbols=("ACME",),
    )
    assert snapshot.is_complete is False
