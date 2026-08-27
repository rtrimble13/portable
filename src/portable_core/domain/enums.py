"""Every enumeration in the domain, in one place.

These mirror the schema's ``CHECK`` constraints exactly. A test asserts the
correspondence in both directions -- a value the schema permits and Python does
not is a row nothing can read; a value Python permits and the schema does not
is an insert that fails at the worst possible moment.
"""

from __future__ import annotations

import enum


class AccountType(enum.StrEnum):
    """Tax treatment. Not a tag: it changes what a realized gain means."""

    TAXABLE = "taxable"
    TAX_DEFERRED = "tax_deferred"
    TAX_EXEMPT = "tax_exempt"


class AccountStatus(enum.StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class CashTreatment(enum.StrEnum):
    """PORT-GIPS-A07. Invested cash is always in the return.

    Operating cash not fully available for investment may be excluded, but only
    by this explicit stored flag -- never implicitly. There is no ex-cash
    return basis.
    """

    INVESTED = "invested"
    OPERATING = "operating"


class InstrumentType(enum.StrEnum):
    EQUITY = "equity"
    ETF = "etf"
    MUTUAL_FUND = "mutual_fund"
    ADR = "adr"
    CASH = "cash"
    MONEY_MARKET = "money_market"
    OPTION = "option"
    BOND = "bond"


class OptionRight(enum.StrEnum):
    CALL = "call"
    PUT = "put"


class ExerciseStyle(enum.StrEnum):
    AMERICAN = "american"
    EUROPEAN = "european"


class DayCount(enum.StrEnum):
    """Accrued-interest conventions.

    Getting this wrong misstates accrued interest, which is part of market
    value (PORT-GIPS-A06) -- so it is a return error, not only a bond-maths
    error.
    """

    THIRTY_360 = "30/360"
    ACT_ACT = "ACT/ACT"
    ACT_365 = "ACT/365"
    ACT_360 = "ACT/360"


class ReliefMethod(enum.StrEnum):
    """How a closing trade consumes lots. Default is specific identification."""

    SPEC = "spec"
    FIFO = "fifo"
    LIFO = "lifo"
    HIFO = "hifo"
    LOFO = "lofo"
    AVERAGE = "avg"


class HoldingPeriod(enum.StrEnum):
    """Long-term requires MORE THAN one year from the DAY AFTER acquisition.

    Exactly one year is short. Short sales are always short, however long held.
    """

    SHORT = "short"
    LONG = "long"


class StrategyType(enum.StrEnum):
    """What the trader meant by grouping these legs. ADR 0009."""

    SINGLE = "single"
    COVERED_CALL = "covered_call"
    VERTICAL = "vertical"
    CALENDAR = "calendar"
    DIAGONAL = "diagonal"
    COLLAR = "collar"
    STRADDLE = "straddle"
    STRANGLE = "strangle"
    CUSTOM = "custom"


class LegRole(enum.StrEnum):
    """What a leg is *for* within its position.

    This is what lets the engines know that *this* short call is written
    against *that* stock, which is what makes assignment a within-position
    operation rather than a cross-position fixup.
    """

    UNDERLYING = "underlying"
    LONG_CALL = "long_call"
    SHORT_CALL = "short_call"
    LONG_PUT = "long_put"
    SHORT_PUT = "short_put"
    LONG_STOCK = "long_stock"
    SHORT_STOCK = "short_stock"
    BOND = "bond"
    HEDGE = "hedge"
    OTHER = "other"


class PositionStatus(enum.StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class LotStatus(enum.StrEnum):
    OPEN = "open"
    PARTIAL = "partial"
    CLOSED = "closed"


class TransactionType(enum.StrEnum):
    """Every ledger event type.

    :func:`portable_core.services.cash_flow.classify` switches on this
    exhaustively: adding a member without classifying it fails mypy's
    exhaustiveness check *and* raises at runtime. That is deliberate -- an
    unclassified transaction type is a silently wrong return.
    """

    # trades
    BUY = "buy"
    SELL = "sell"
    SELL_SHORT = "sell_short"
    BUY_TO_COVER = "buy_to_cover"
    # cash
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    TRANSFER = "transfer"
    JOURNAL = "journal"
    INTEREST = "interest"
    FEE = "fee"
    MARGIN_INTEREST = "margin_interest"
    # income
    DIVIDEND = "dividend"
    DIVIDEND_REINVEST = "dividend_reinvest"
    RETURN_OF_CAPITAL = "return_of_capital"
    COUPON = "coupon"
    ACCRUAL_INCOME = "accrual_income"
    # corporate actions
    SPLIT = "split"
    REVERSE_SPLIT = "reverse_split"
    STOCK_DIVIDEND = "stock_dividend"
    SPINOFF = "spinoff"
    MERGER_CASH = "merger_cash"
    MERGER_STOCK = "merger_stock"
    MERGER_MIXED = "merger_mixed"
    SYMBOL_CHANGE = "symbol_change"
    DELIST = "delist"
    # options lifecycle
    OPTION_EXERCISE = "option_exercise"
    OPTION_ASSIGNMENT = "option_assignment"
    OPTION_EXPIRATION = "option_expiration"
    # fixed income lifecycle
    BOND_AMORTIZATION = "bond_amortization"
    BOND_ACCRETION = "bond_accretion"
    BOND_CALL = "bond_call"
    BOND_MATURITY = "bond_maturity"
    # adjustments
    REVERSAL = "reversal"
    CORRECTION = "correction"


#: Transaction types that open or add to a position.
OPENING_TYPES: frozenset[TransactionType] = frozenset(
    {
        TransactionType.BUY,
        TransactionType.SELL_SHORT,
        TransactionType.DIVIDEND_REINVEST,
        TransactionType.STOCK_DIVIDEND,
        TransactionType.SPINOFF,
        TransactionType.MERGER_STOCK,
    }
)

#: Transaction types that reduce or liquidate a position.
CLOSING_TYPES: frozenset[TransactionType] = frozenset(
    {
        TransactionType.SELL,
        TransactionType.BUY_TO_COVER,
        TransactionType.OPTION_EXERCISE,
        TransactionType.OPTION_ASSIGNMENT,
        TransactionType.OPTION_EXPIRATION,
        TransactionType.MERGER_CASH,
        TransactionType.BOND_CALL,
        TransactionType.BOND_MATURITY,
        TransactionType.DELIST,
    }
)


class FeeClass(enum.StrEnum):
    """PORT-GIPS-D01. The three return bases are *derived* from this.

    A stored fact about the fee, decided when it is recorded -- not an
    inference at report time. NULL is an error, enforced by the schema.

    Note the trap: **a custody fee is not a transaction cost.** Under the Asset
    Owner ladder that `portable` follows, custody falls inside investment
    management costs (per the November 2020 errata) and reduces net-of-fees
    only. Under the Firms ladder it is administrative and reduces neither. In
    both regimes the transaction-based component of a custody fee is still not
    a transaction cost.
    """

    #: Deducted at every basis, including gross.
    TRANSACTION_COST = "transaction_cost"
    #: Embedded in a fund's NAV. Already deducted -- never deduct it again
    #: (PORT-GIPS-D05); "correcting" for an expense ratio double-counts it.
    EMBEDDED_FUND_FEE = "embedded_fund_fee"
    #: An advisor's fee on a separately managed account.
    EXTERNAL_MGMT_FEE = "external_mgmt_fee"
    #: The owner's own cost of running the portfolio -- custody, data,
    #: research, performance tooling. Reduces net-of-fees only.
    INTERNAL_MGMT_COST = "internal_mgmt_cost"
    #: Wire fees, tax preparation, legal. Reduces no GIPS basis at all.
    OTHER_ADMIN = "other_admin"


class ReturnBasis(enum.StrEnum):
    """PORT-GIPS-D01, the Asset Owner ladder.

    For a self-managed portfolio with no external manager, gross-of-fees and
    net-of-external-costs-only are numerically identical -- reported once and
    labelled, rather than as two identical columns.
    """

    GROSS_OF_FEES = "gross_of_fees"
    NET_OF_EXTERNAL_COSTS_ONLY = "net_of_external_costs_only"
    NET_OF_FEES = "net_of_fees"


class FlowLevel(enum.StrEnum):
    """The level at which a return is being computed.

    The single most important argument in the codebase: an inter-account
    transfer is an external flow at ACCOUNT level and is not one at PORTFOLIO
    level. See PORT-GIPS-B02 and ADR 0007.
    """

    ACCOUNT = "account"
    PORTFOLIO = "portfolio"


class FlowClassification(enum.StrEnum):
    """What a transaction is, for return purposes, at a given level."""

    #: Capital entering or leaving. Triggers revaluation when large.
    EXTERNAL = "external"
    #: A movement that does not cross the boundary at this level.
    INTERNAL = "internal"
    #: Dividends, coupons, reinvestments, return of capital. **Never** an
    #: external flow, at any level.
    INCOME = "income"
    #: A fee or financing charge. A cost, not a flow.
    COST = "cost"


class ValuationBasis(enum.StrEnum):
    """How a price was arrived at, alongside its hierarchy level."""

    EXCHANGE_CLOSE = "exchange_close"
    MODEL = "model"
    ESTIMATE = "estimate"
    MANUAL = "manual"


class BenchmarkReturnType(enum.StrEnum):
    """PORT-GIPS-G01. `portable` refuses a price-only benchmark rather than warning."""

    TOTAL_RETURN = "total_return"
    PRICE_ONLY = "price_only"


class BasisAdjustmentReason(enum.StrEnum):
    """Why a lot's basis or holding-period start changed."""

    COMMISSION = "commission"
    FEE = "fee"
    RETURN_OF_CAPITAL = "return_of_capital"
    SPLIT = "split"
    REVERSE_SPLIT = "reverse_split"
    STOCK_DIVIDEND = "stock_dividend"
    SPINOFF = "spinoff"
    MERGER = "merger"
    OPTION_PREMIUM_EXERCISE = "option_premium_exercise"
    OPTION_PREMIUM_ASSIGNMENT = "option_premium_assignment"
    AMORTIZATION = "amortization"
    ACCRETION = "accretion"
    WASH_SALE = "wash_sale"
    MANUAL_CORRECTION = "manual_correction"
    FORCED_ZERO_BASIS = "forced_zero_basis"


class TransactionSource(enum.StrEnum):
    MANUAL = "manual"
    IMPORT = "import"
    DERIVED = "derived"
