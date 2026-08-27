"""PORT-GIPS-B02 -- the cash-flow classification matrix, verbatim.

`docs/gips-standard.md` calls this "the highest-risk item in the whole
document". The matrix below is transcribed from it **unchanged**, including the
rows whose answer is "no", because those are the rows that are wrong in real
systems. If a row disappears from the standard it disappears from here, in the
same commit.

GIPS acceptance tests:
    test_flow_classification_matrix
    test_internal_transfer_is_flow_neutral_at_portfolio_level
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portable_core.domain.enums import (
    FlowClassification,
    FlowLevel,
    TransactionSource,
    TransactionType,
)
from portable_core.domain.models import Transaction
from portable_core.services import cash_flow

pytestmark = [pytest.mark.unit, pytest.mark.gips]

D = Decimal


def txn(
    txn_type: TransactionType,
    *,
    amount: str = "1000.00",
    counter: int | None = None,
    txn_id: int = 1,
) -> Transaction:
    return Transaction(
        txn_id=txn_id,
        account_id=1,
        trade_date=date(2025, 6, 30),
        seq=1,
        txn_type=txn_type,
        net_cash_effect=D(amount),
        counter_account_id=counter,
        source=TransactionSource.MANUAL,
    )


# The PORT-GIPS-B02 table. Columns: event, external at ACCOUNT level, external
# at PORTFOLIO level. Transcribed verbatim from docs/gips-standard.md §6.B.
MATRIX: list[tuple[str, TransactionType, bool, bool]] = [
    ("Deposit from outside", TransactionType.DEPOSIT, True, True),
    ("Withdrawal to outside", TransactionType.WITHDRAWAL, True, True),
    ("Transfer between two accounts", TransactionType.TRANSFER, True, False),
    ("Cash dividend received", TransactionType.DIVIDEND, False, False),
    ("Bond coupon received", TransactionType.COUPON, False, False),
    ("Reinvested dividend", TransactionType.DIVIDEND_REINVEST, False, False),
    ("Return of capital", TransactionType.RETURN_OF_CAPITAL, False, False),
    ("Fee paid", TransactionType.FEE, False, False),
    ("Margin interest paid", TransactionType.MARGIN_INTEREST, False, False),
    ("Option assignment converting to stock", TransactionType.OPTION_ASSIGNMENT, False, False),
]


@pytest.mark.parametrize(
    ("label", "txn_type", "external_at_account", "external_at_portfolio"),
    MATRIX,
    ids=[row[0] for row in MATRIX],
)
def test_flow_classification_matrix(
    label: str,
    txn_type: TransactionType,
    external_at_account: bool,
    external_at_portfolio: bool,
) -> None:
    """PORT-GIPS-B02, the table, parametrised verbatim."""
    counter = 2 if txn_type is TransactionType.TRANSFER else None
    t = txn(txn_type, counter=counter)

    assert cash_flow.is_external(t, FlowLevel.ACCOUNT) is external_at_account, (
        f"{label}: account level"
    )
    assert cash_flow.is_external(t, FlowLevel.PORTFOLIO) is external_at_portfolio, (
        f"{label}: portfolio level"
    )


def test_stock_distribution_in_kind_is_an_external_flow_valued_at_distribution() -> None:
    """PORT-GIPS-B02 / C02.c -- the one row that needs a valuation to answer."""
    t = txn(TransactionType.STOCK_DIVIDEND, amount="0.00")

    without_value = cash_flow.classify(t, FlowLevel.PORTFOLIO)
    assert without_value.classification is FlowClassification.INTERNAL

    with_value = cash_flow.classify(t, FlowLevel.PORTFOLIO, in_kind_value=D("5000.00"))
    assert with_value.classification is FlowClassification.EXTERNAL
    assert with_value.amount == D("5000.00")
    assert with_value.is_in_kind is True


def test_internal_transfer_is_flow_neutral_at_portfolio_level() -> None:
    """PORT-GIPS-B02 -- and the reason it is not "two flows that cancel".

    A transfer is ONE ledger row with two sides (ADR 0007). At portfolio level
    it yields NO flow at all. That is a stronger statement than "two flows
    summing to zero", and the difference is not academic: two cancelling flows
    would still cross the large-flow threshold, still force a revaluation and a
    sub-period break under PORT-GIPS-B03, and still appear in the daily flow
    series that PORT-GIPS-C02 feeds to the money-weighted solve.
    """
    transfer = txn(TransactionType.TRANSFER, amount="-100000.00", counter=2)

    account = cash_flow.classify(transfer, FlowLevel.ACCOUNT)
    assert account.classification is FlowClassification.EXTERNAL
    assert account.amount == D("-100000.00")

    portfolio = cash_flow.classify(transfer, FlowLevel.PORTFOLIO)
    assert portfolio.classification is FlowClassification.INTERNAL
    assert portfolio.amount == D("0.00")
    assert portfolio.is_external is False


def test_a_100k_shuffle_does_not_reach_the_portfolio_flow_series() -> None:
    """The concrete failure this exists to prevent.

    Treat an inter-account transfer as external at portfolio level and a
    $100,000 shuffle between the owner's own accounts silently rewrites the
    track record, with a number that is arithmetically defensible and
    economically meaningless.
    """
    out_leg = txn(TransactionType.TRANSFER, amount="-100000.00", counter=2, txn_id=1)
    deposit = txn(TransactionType.DEPOSIT, amount="100000.00", txn_id=2)

    portfolio_flows = [
        cash_flow.flow_amount(t, FlowLevel.PORTFOLIO) for t in (out_leg, deposit)
    ]
    assert portfolio_flows == [D("0.00"), D("100000.00")], (
        "only the genuine outside contribution reaches the portfolio flow series"
    )


def test_income_is_never_an_external_flow_at_any_level() -> None:
    """The GIPS glossary says so inside the definition of external cash flow."""
    income_types = [
        TransactionType.DIVIDEND,
        TransactionType.DIVIDEND_REINVEST,
        TransactionType.COUPON,
        TransactionType.INTEREST,
        TransactionType.RETURN_OF_CAPITAL,
        TransactionType.ACCRUAL_INCOME,
    ]
    for txn_type in income_types:
        for level in FlowLevel:
            result = cash_flow.classify(txn(txn_type), level)
            assert result.classification is FlowClassification.INCOME, txn_type
            assert result.amount == D("0.00"), txn_type


def test_return_of_capital_is_income_for_flows_and_basis_for_tax() -> None:
    """Two different questions about one event. Conflating them gets both wrong.

    For FLOW purposes return of capital is income and never external capital.
    For TAX purposes it reduces basis and is not income. This asserts the flow
    half; `tests/unit/test_corporate_actions.py` asserts the tax half.
    """
    at_level = {
        level: cash_flow.classify(txn(TransactionType.RETURN_OF_CAPITAL), level)
        for level in FlowLevel
    }
    assert {r.classification for r in at_level.values()} == {FlowClassification.INCOME}
    assert not any(r.is_external for r in at_level.values())
    assert all(r.amount == D("0.00") for r in at_level.values())


def test_every_transaction_type_is_classified_at_every_level() -> None:
    """Totality, asserted rather than assumed.

    `classify` ends in `assert_never`, so mypy catches an unhandled member at
    build time. This catches it at runtime too, because an unclassified
    transaction type is a silently wrong return and one guard is not enough.
    """
    for txn_type in TransactionType:
        counter = 2 if txn_type is TransactionType.TRANSFER else None
        for level in FlowLevel:
            result = cash_flow.classify(txn(txn_type, counter=counter), level)
            assert isinstance(result.classification, FlowClassification), txn_type


def test_a_reversal_is_classified_by_what_it_reverses() -> None:
    """A reversed deposit is an outward flow, not a non-event.

    Treating every reversal as internal would leave a phantom contribution in
    the record -- the deposit's flow would stand and its undoing would not.
    """
    deposit = txn(TransactionType.DEPOSIT, amount="50000.00", txn_id=1)
    reversal = Transaction(
        txn_id=2,
        account_id=1,
        trade_date=date(2025, 7, 1),
        seq=1,
        txn_type=TransactionType.REVERSAL,
        net_cash_effect=D("-50000.00"),
        reverses_txn_id=1,
    )

    result = cash_flow.classify_reversal(reversal, deposit, FlowLevel.PORTFOLIO)
    assert result.classification is FlowClassification.EXTERNAL
    assert result.amount == D("-50000.00")
    assert result.flow_date == date(2025, 7, 1)

    trade = txn(TransactionType.BUY, amount="-10000.00", txn_id=3)
    trade_reversal = cash_flow.classify_reversal(reversal, trade, FlowLevel.PORTFOLIO)
    assert trade_reversal.classification is FlowClassification.INTERNAL
    assert trade_reversal.amount == D("0.00")


def test_level_is_a_required_argument() -> None:
    """It has no default, deliberately.

    Defaulting the most dangerous parameter in the codebase would let a call
    site get it wrong by omission, which is precisely how the classic error
    happens.
    """
    import inspect

    signature = inspect.signature(cash_flow.classify)
    level = signature.parameters["level"]
    assert level.default is inspect.Parameter.empty
    assert level.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
