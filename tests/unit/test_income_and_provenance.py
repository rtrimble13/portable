"""Income withholding, and provenance on every ledger row.

The import prerequisites in `docs/broker-import.md` §10. Two facts every
imported row needs and could not carry:

* **where it came from** -- `TradingService` hardcoded `source = MANUAL`, so an
  imported row would have claimed to be hand-entered (`PORT-GIPS-J03`); and
* **what was withheld** -- the `taxes_withheld` column and domain field existed
  with no service or CLI able to set them, which every foreign dividend and
  every retirement distribution needs.

Withholding is the one with arithmetic behind it, so it is tested hardest:
`gross_amount` is the income the instrument paid and `net_cash_effect` is what
landed, and a report needs both because return is earned on the gross while the
cash balance moved by the net.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portable_core.domain.enums import TransactionSource, TransactionType
from portable_core.domain.models import Account, Instrument
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_WITHHOLDING_INVALID
from portable_core.persistence.repositories import Repositories
from portable_core.services.trading import TradeIntent, TradingService

pytestmark = pytest.mark.unit

D = Decimal
PAY = date(2024, 3, 15)
EX = date(2024, 3, 1)


# ── withholding arithmetic ───────────────────────────────────────────────────


def test_withholding_splits_gross_from_what_landed(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """Gross stays the income; net is the cash. Both are recorded."""
    txn = TradingService(repos).record_income(
        taxable_account,
        aapl,
        TransactionType.DIVIDEND,
        D("100.00"),
        PAY,
        ex_date=EX,
        taxes_withheld=D("15.00"),
    )
    assert txn.gross_amount == D("100.00")
    assert txn.net_cash_effect == D("85.00")
    assert txn.taxes_withheld == D("15.00")


def test_withholding_is_not_a_fee(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """Tax withheld is not a cost and must not be counted as one.

    `Transaction.total_costs` is fees plus commissions; withholding sits outside
    it. Folding withholding into fees would deduct it from a return basis under
    `PORT-GIPS-D01`, which is a different claim about a different thing.
    """
    txn = TradingService(repos).record_income(
        taxable_account,
        aapl,
        TransactionType.DIVIDEND,
        D("100.00"),
        PAY,
        taxes_withheld=D("15.00"),
    )
    assert txn.fees == D("0.00")
    assert txn.total_costs == D("0.00")
    assert txn.fee_class is None


def test_the_reclaimable_portion_is_stored_separately(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """Reclaimable is accrued; non-reclaimable reduces return (PORT-GIPS-A06).

    One combined figure cannot answer both questions, which is why the schema
    carries two columns and why the service keeps them apart.
    """
    txn = TradingService(repos).record_income(
        taxable_account,
        aapl,
        TransactionType.DIVIDEND,
        D("100.00"),
        PAY,
        taxes_withheld=D("15.00"),
        withholding_reclaimable=D("5.00"),
    )
    assert txn.taxes_withheld == D("15.00")
    assert txn.withholding_reclaimable == D("5.00")
    # Still the whole 15 that left the account, not 15 minus the reclaim.
    assert txn.net_cash_effect == D("85.00")


def test_no_withholding_leaves_reclaimable_null_not_zero(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """Blank and zero must not mean the same thing (`CLAUDE.md` output rules).

    A NULL says nobody stated a split; a zero says somebody stated that none of
    it is reclaimable. Those are different facts about a foreign dividend.
    """
    txn = TradingService(repos).record_income(
        taxable_account, aapl, TransactionType.DIVIDEND, D("100.00"), PAY
    )
    assert txn.taxes_withheld == D("0.00")
    assert txn.withholding_reclaimable is None
    assert txn.net_cash_effect == D("100.00")


# ── the refusals ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("withheld", "reclaimable", "why"),
    [
        (D("-1.00"), None, "negative withholding"),
        (D("150.00"), None, "withholding above the gross"),
        (D("15.00"), D("-1.00"), "negative reclaim"),
        (D("15.00"), D("20.00"), "reclaim above the withholding"),
    ],
)
def test_an_impossible_withholding_split_is_refused(
    repos: Repositories,
    taxable_account: Account,
    aapl: Instrument,
    withheld: Decimal,
    reclaimable: Decimal | None,
    why: str,
) -> None:
    """Each of these would otherwise produce a plausible, wrong number.

    Withholding above the gross inverts the cash effect; a reclaim above what
    was withheld accrues a receivable that does not exist.
    """
    with pytest.raises(ValidationError) as caught:
        TradingService(repos).record_income(
            taxable_account,
            aapl,
            TransactionType.DIVIDEND,
            D("100.00"),
            PAY,
            taxes_withheld=withheld,
            withholding_reclaimable=reclaimable,
        )
    assert caught.value.code == E_WITHHOLDING_INVALID, why


def test_an_ex_date_after_the_pay_date_is_refused(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """Entitlement is fixed before the cash arrives, never after."""
    with pytest.raises(ValidationError):
        TradingService(repos).record_income(
            taxable_account,
            aapl,
            TransactionType.DIVIDEND,
            D("100.00"),
            date(2024, 3, 1),
            ex_date=date(2024, 3, 15),
        )


def test_income_recognises_on_the_pay_date_and_accrues_from_the_ex_date(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """PORT-GIPS-A06: accruing on the wrong one shifts return across a period."""
    txn = TradingService(repos).record_income(
        taxable_account, aapl, TransactionType.DIVIDEND, D("100.00"), PAY, ex_date=EX
    )
    assert txn.trade_date == PAY
    assert txn.pay_date == PAY
    assert txn.ex_date == EX


def test_an_omitted_ex_date_defaults_to_the_pay_date(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """Explicit, not implicit: a coupon has no ex-date and must not carry None."""
    txn = TradingService(repos).record_income(
        taxable_account, aapl, TransactionType.COUPON, D("25.00"), PAY
    )
    assert txn.ex_date == PAY


# ── provenance ───────────────────────────────────────────────────────────────


def test_a_trade_carries_the_source_it_was_given(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """An importer must be able to say a row was imported."""
    service = TradingService(repos)
    plan = service.plan(
        TradeIntent(
            account=taxable_account,
            instrument=aapl,
            txn_type=TransactionType.BUY,
            quantity=D("10"),
            price=D("100"),
            trade_date=date(2024, 1, 10),
            external_ref="wb:9f2c1a04d3e88b71",
            source=TransactionSource.IMPORT,
        )
    )
    assert plan.transaction.source is TransactionSource.IMPORT
    assert plan.transaction.external_ref == "wb:9f2c1a04d3e88b71"


def test_a_trade_defaults_to_manual(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """Hand entry stays the default; only an importer says otherwise."""
    service = TradingService(repos)
    plan = service.plan(
        TradeIntent(
            account=taxable_account,
            instrument=aapl,
            txn_type=TransactionType.BUY,
            quantity=D("10"),
            price=D("100"),
            trade_date=date(2024, 1, 10),
        )
    )
    assert plan.transaction.source is TransactionSource.MANUAL


def test_cash_and_income_carry_source_too(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """Provenance is on every write path, not only the trade one."""
    service = TradingService(repos)
    cash = service.record_cash(
        taxable_account,
        TransactionType.DEPOSIT,
        D("1000.00"),
        date(2024, 1, 2),
        external_ref="wb:deposit-1",
        source=TransactionSource.IMPORT,
    )
    income = service.record_income(
        taxable_account,
        aapl,
        TransactionType.DIVIDEND,
        D("100.00"),
        PAY,
        external_ref="wb:div-1",
        source=TransactionSource.IMPORT,
    )
    assert cash.source is income.source is TransactionSource.IMPORT
    assert (cash.external_ref, income.external_ref) == ("wb:deposit-1", "wb:div-1")


def test_source_survives_a_round_trip_through_the_ledger(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """Stored and read back, not merely set on the object in memory."""
    service = TradingService(repos)
    txn = service.record_income(
        taxable_account,
        aapl,
        TransactionType.DIVIDEND,
        D("100.00"),
        PAY,
        taxes_withheld=D("15.00"),
        withholding_reclaimable=D("5.00"),
        external_ref="wb:div-1",
        source=TransactionSource.IMPORT,
    )
    txn_id = repos.transactions.append(txn)

    stored = repos.transactions.get(txn_id)
    assert stored is not None
    assert stored.source is TransactionSource.IMPORT
    assert stored.external_ref == "wb:div-1"
    assert stored.taxes_withheld == D("15.00")
    assert stored.withholding_reclaimable == D("5.00")
    assert stored.net_cash_effect == D("85.00")


# ── a distribution taken in units ────────────────────────────────────────────


def test_a_reinvested_distribution_is_income_and_a_lot_and_moves_no_cash(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """One event, one row: the gross is the income earned and the cost of the
    units bought with it. A dividend plus a buy would put a pair of cash
    movements in the ledger that never happened."""
    txn = TradingService(repos).record_income(
        taxable_account,
        aapl,
        TransactionType.DIVIDEND_REINVEST,
        D("100.00"),
        PAY,
        ex_date=EX,
        reinvested_units=D("0.5"),
    )
    assert txn.gross_amount == D("100.00")
    assert txn.net_cash_effect == D("0.00")
    assert txn.quantity == D("0.5")
    assert txn.price == D("200")


def test_a_reinvestment_states_its_units_and_a_dividend_states_none(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    service = TradingService(repos)
    with pytest.raises(ValidationError, match="states the units it bought"):
        service.record_income(
            taxable_account, aapl, TransactionType.DIVIDEND_REINVEST, D("100.00"), PAY
        )
    with pytest.raises(ValidationError, match="reinvests nothing"):
        service.record_income(
            taxable_account,
            aapl,
            TransactionType.DIVIDEND,
            D("100.00"),
            PAY,
            reinvested_units=D("1"),
        )


def test_withholding_on_a_reinvestment_is_refused_not_netted(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    with pytest.raises(ValidationError, match="cannot also have tax withheld"):
        TradingService(repos).record_income(
            taxable_account,
            aapl,
            TransactionType.DIVIDEND_REINVEST,
            D("100.00"),
            PAY,
            taxes_withheld=D("15.00"),
            reinvested_units=D("0.5"),
        )


def test_a_capital_gain_distribution_keeps_its_character_and_may_take_units(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """The character is the type: a long-term distribution is never a
    dividend. Taken in units it is income and a lot, like a reinvested
    dividend; taken in cash it is income."""
    service = TradingService(repos)
    cash = service.record_income(
        taxable_account, aapl, TransactionType.CAPITAL_GAIN_LT, D("40.00"), PAY
    )
    assert cash.txn_type is TransactionType.CAPITAL_GAIN_LT
    assert cash.net_cash_effect == D("40.00") and cash.quantity is None

    units = service.record_income(
        taxable_account,
        aapl,
        TransactionType.CAPITAL_GAIN_ST,
        D("40.00"),
        PAY,
        reinvested_units=D("0.2"),
    )
    assert units.txn_type is TransactionType.CAPITAL_GAIN_ST
    assert units.net_cash_effect == D("0.00") and units.quantity == D("0.2")
