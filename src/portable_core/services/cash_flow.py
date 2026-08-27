"""Cash-flow classification. The single function. ADR 0007, ``PORT-GIPS-B02``.

`docs/gips-standard.md` calls ``PORT-GIPS-B02`` "the highest-risk item in the
whole document", and it is right. The reason is that a wrong answer here does
not look wrong: it produces a return that is arithmetically defensible and
economically meaningless.

Two rules do most of the work, and both are counter-intuitive at first:

**Income is never an external cash flow.** Not dividends, not coupons, not
reinvestments, not return of capital. The GIPS glossary says so in the
definition itself: "Dividend and interest income payments are not considered
external cash flows." A dividend is *return*, not a contribution.

**The answer depends on the level.** A transfer between two of the owner's own
accounts is an external flow at ACCOUNT level -- money left one and arrived at
the other -- and is **not** one at PORTFOLIO level, because it nets to zero.
Treat it as external at portfolio level and a $100k shuffle between the owner's
own accounts silently rewrites the track record.

Nothing outside this module classifies a transaction. `pt cash-flows`,
`ValuationEngine`, and every future `pert` return path call :func:`classify`
and nothing else. There is a test that greps for the alternative.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import assert_never

from portable_core.domain.enums import (
    FlowClassification,
    FlowLevel,
    TransactionType,
)
from portable_core.domain.models import Transaction

__all__ = ["FlowResult", "classify", "flow_amount", "is_external"]


@dataclass(frozen=True, slots=True)
class FlowResult:
    """What a transaction is, at a level, and for how much."""

    classification: FlowClassification
    #: Signed, in portfolio base currency. Positive into the portfolio.
    #: Zero for anything that is not an external flow at this level.
    amount: Decimal
    flow_date: date
    #: True where the flow is capital moving in kind rather than as cash;
    #: valued at the time of distribution (PORT-GIPS-C02, Firms 2.A.29.c).
    is_in_kind: bool = False

    @property
    def is_external(self) -> bool:
        return self.classification is FlowClassification.EXTERNAL


def classify(
    txn: Transaction,
    level: FlowLevel,
    *,
    in_kind_value: Decimal | None = None,
) -> FlowResult:
    """Classify *txn* for return purposes at *level*.

    This is a **total** function over :class:`TransactionType`: the ``match``
    has no default arm and ends in :func:`typing.assert_never`, so adding a
    transaction type without classifying it fails ``mypy --strict``'s
    exhaustiveness check at build time *and* raises at runtime. An
    unclassified transaction type is a silently wrong return, so it is made
    impossible to add one by accident.

    Args:
        txn: the ledger event.
        level: ``ACCOUNT`` or ``PORTFOLIO``. Not optional and not defaulted --
            defaulting it would let a call site get the most dangerous
            parameter in the codebase wrong by omission.
        in_kind_value: for a stock distribution or in-kind transfer, the value
            at the time of distribution. Passed in rather than looked up: the
            domain stays pure and the caller, which has a provider, does the
            valuing.

    Returns:
        A :class:`FlowResult` whose ``amount`` is zero for anything that is not
        an external flow at this level.
    """
    kind = txn.txn_type
    zero = Decimal("0.00")

    match kind:
        # ── Capital crossing the portfolio boundary. External at both levels.
        case TransactionType.DEPOSIT | TransactionType.WITHDRAWAL:
            return FlowResult(FlowClassification.EXTERNAL, txn.net_cash_effect, txn.trade_date)

        # ── A transfer between two of the owner's own accounts.
        #
        # The whole point of ADR 0007. At ACCOUNT level this is a real flow:
        # money left this account. At PORTFOLIO level it is NOT a flow at all
        # -- not two flows that happen to cancel. That distinction matters
        # downstream, because two cancelling flows would still trigger
        # revaluation and a sub-period break under PORT-GIPS-B03, and would
        # still appear in the daily flow series PORT-GIPS-C02 feeds to the
        # money-weighted solve.
        case TransactionType.TRANSFER:
            if level is FlowLevel.ACCOUNT:
                return FlowResult(
                    FlowClassification.EXTERNAL, txn.net_cash_effect, txn.trade_date
                )
            return FlowResult(FlowClassification.INTERNAL, zero, txn.trade_date)

        # A journal moves value within one account -- a cash sweep, a
        # reclassification. Never a flow at either level.
        case TransactionType.JOURNAL:
            return FlowResult(FlowClassification.INTERNAL, zero, txn.trade_date)

        # ── Income. NEVER an external flow, at ANY level.
        #
        # Return of capital sits here too, and this is the trap worth naming:
        # for TAX purposes it reduces basis and is not income; for FLOW
        # purposes it is not external capital. Those are two different
        # questions about one event, and conflating them gets both wrong.
        case (
            TransactionType.DIVIDEND
            | TransactionType.DIVIDEND_REINVEST
            | TransactionType.COUPON
            | TransactionType.INTEREST
            | TransactionType.RETURN_OF_CAPITAL
            | TransactionType.ACCRUAL_INCOME
        ):
            return FlowResult(FlowClassification.INCOME, zero, txn.trade_date)

        # ── Costs. A fee is not a flow; it is a reduction in return.
        #
        # Which return bases it reduces is a separate question, answered by
        # transaction.fee_class (PORT-GIPS-D01), not here. Margin interest is a
        # financing cost -- GIPS is silent, and `portable` treats it as
        # reducing return in all three bases, with a disclosure saying so.
        case TransactionType.FEE | TransactionType.MARGIN_INTEREST:
            return FlowResult(FlowClassification.COST, zero, txn.trade_date)

        # ── Trades. Buying and selling changes what the portfolio holds, not
        # how much capital is in it. Never a flow.
        case (
            TransactionType.BUY
            | TransactionType.SELL
            | TransactionType.SELL_SHORT
            | TransactionType.BUY_TO_COVER
        ):
            return FlowResult(FlowClassification.INTERNAL, zero, txn.trade_date)

        # ── Corporate actions are transformations of what is held, not capital
        # movements. A split, a symbol change, a stock merger: the portfolio
        # owns something different afterwards, but nothing entered or left.
        case (
            TransactionType.SPLIT
            | TransactionType.REVERSE_SPLIT
            | TransactionType.SYMBOL_CHANGE
            | TransactionType.MERGER_STOCK
            | TransactionType.MERGER_CASH
            | TransactionType.MERGER_MIXED
            | TransactionType.SPINOFF
            | TransactionType.DELIST
        ):
            return FlowResult(FlowClassification.INTERNAL, zero, txn.trade_date)

        # A stock dividend received from outside -- a distribution in kind --
        # IS an external flow, valued at the time of distribution
        # (PORT-GIPS-C02.c / Firms 2.A.29.c). Where no value was supplied it
        # is being treated as an internal transformation of an existing
        # holding, which is the common case for a stock dividend on a held
        # position.
        case TransactionType.STOCK_DIVIDEND:
            if in_kind_value is not None:
                return FlowResult(
                    FlowClassification.EXTERNAL,
                    in_kind_value,
                    txn.trade_date,
                    is_in_kind=True,
                )
            return FlowResult(FlowClassification.INTERNAL, zero, txn.trade_date)

        # ── Options lifecycle. An assignment converting to stock is an
        # internal transformation: the premium becomes part of the stock's
        # proceeds or basis, and no capital crossed the boundary.
        case (
            TransactionType.OPTION_EXERCISE
            | TransactionType.OPTION_ASSIGNMENT
            | TransactionType.OPTION_EXPIRATION
        ):
            return FlowResult(FlowClassification.INTERNAL, zero, txn.trade_date)

        # ── Fixed income lifecycle. Amortization and accretion are basis
        # movements; a call or a maturity is a disposition. None is a flow.
        case (
            TransactionType.BOND_AMORTIZATION
            | TransactionType.BOND_ACCRETION
            | TransactionType.BOND_CALL
            | TransactionType.BOND_MATURITY
        ):
            return FlowResult(FlowClassification.INTERNAL, zero, txn.trade_date)

        # ── Adjustments inherit nothing: a reversal of a deposit is a
        # withdrawal-shaped external flow, and a reversal of a trade is not a
        # flow at all. The caller resolves the reversed transaction and
        # classifies THAT, negated. Classifying the reversal row itself
        # would need the ledger, and this function is pure.
        case TransactionType.REVERSAL | TransactionType.CORRECTION:
            return FlowResult(FlowClassification.INTERNAL, zero, txn.trade_date)

    assert_never(kind)


def classify_reversal(
    reversal: Transaction,
    reversed_txn: Transaction,
    level: FlowLevel,
) -> FlowResult:
    """Classify a reversal by reference to what it reverses, negated.

    Kept separate from :func:`classify` because it needs two transactions and
    :func:`classify` is deliberately pure over one. A reversal of a deposit
    really is an outward external flow; treating every reversal as internal
    would leave a phantom contribution in the record.
    """
    original = classify(reversed_txn, level)
    return FlowResult(
        original.classification,
        -original.amount,
        reversal.trade_date,
        is_in_kind=original.is_in_kind,
    )


def is_external(txn: Transaction, level: FlowLevel) -> bool:
    """Convenience predicate. Same single source of truth."""
    return classify(txn, level).is_external


def flow_amount(txn: Transaction, level: FlowLevel) -> Decimal:
    """The signed external-flow amount, or zero. Same single source of truth."""
    return classify(txn, level).amount
