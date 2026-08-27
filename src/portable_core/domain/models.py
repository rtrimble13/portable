"""The typed domain objects.

Frozen, slotted dataclasses with no I/O, no SQL, and no business rules
(ADR 0003). Business logic lives in ``services/``; these are what it operates
on and what repositories return.

Every money field is a :class:`~decimal.Decimal`, and
:func:`~portable_core.domain.base.check_decimal_fields` rejects a float at
construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from portable_core.domain.base import check_decimal_fields
from portable_core.domain.enums import (
    AccountStatus,
    AccountType,
    BasisAdjustmentReason,
    BenchmarkReturnType,
    CashTreatment,
    DayCount,
    ExerciseStyle,
    FeeClass,
    HoldingPeriod,
    InstrumentType,
    LegRole,
    LotStatus,
    OptionRight,
    PositionStatus,
    ReliefMethod,
    StrategyType,
    TransactionSource,
    TransactionType,
    ValuationBasis,
)

# ── Portfolio and accounts ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PortfolioMeta:
    """Identity of the portfolio. One per `.port` file. A GIPS *total fund*."""

    name: str
    inception_date: date
    base_currency: str
    #: ``MM-DD``. Calendar by default (PORT-GIPS-A04). Annual period boundaries
    #: derive from this and never from an ad-hoc ``--as-of``.
    fiscal_year_end: str
    schema_version: int
    #: Required disclosure on every report (PORT-GIPS-I01).
    description: str = ""
    created_at: str = ""
    updated_at: str = ""
    portable_version: str = ""


@dataclass(frozen=True, slots=True)
class Account:
    """Holds positions and cash. Every transaction happens in one.

    Accounts track P&L **net of tax**; positions do not. That split is why the
    model has both.
    """

    account_id: int
    name: str
    account_type: AccountType
    opened_date: date
    status: AccountStatus = AccountStatus.OPEN
    closed_date: date | None = None
    custodian: str | None = None
    account_alias: str | None = None
    cash_treatment: CashTreatment = CashTreatment.INVESTED
    default_relief_method: ReliefMethod = ReliefMethod.SPEC
    allows_fractional: bool = False
    sweep_instrument_id: int | None = None
    currency: str = "USD"
    note: str | None = None

    @property
    def is_taxable(self) -> bool:
        return self.account_type is AccountType.TAXABLE


@dataclass(frozen=True, slots=True)
class TaxRateSchedule:
    """Effective-dated rates, in components so the effective rate is explainable.

    Kept as separate federal / state / NIIT parts rather than one blended
    number, because "why is my estimated tax 40.8%?" should have an answer that
    is arithmetic rather than folklore.
    """

    rate_id: int
    account_id: int
    effective_from: date
    short_term_federal: Decimal
    long_term_federal: Decimal
    state: Decimal = Decimal("0")
    niit: Decimal = Decimal("0")
    qualified_dividend: Decimal | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        check_decimal_fields(self)

    def effective_rate(self, period: HoldingPeriod) -> Decimal:
        """Federal + state + NIIT for the given holding period.

        NIIT applies to net investment income regardless of holding period,
        which is why it is added to both. Whether the taxpayer is actually over
        the NIIT threshold is not something `portable` can know -- ADR 0011 is
        explicit that this is an estimate and says what it does not model.
        """
        federal = (
            self.short_term_federal if period is HoldingPeriod.SHORT else self.long_term_federal
        )
        return federal + self.state + self.niit


# ── Instruments ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OptionDetail:
    """Listed option specifics."""

    underlier_instrument_id: int
    option_right: OptionRight
    strike: Decimal
    expiry: date
    #: Shares per contract. Stored, never assumed: it is 100 until an adjusted
    #: contract says otherwise, and a wrong multiplier is a 100x wrong number.
    multiplier: Decimal
    occ_symbol: str | None = None
    exercise_style: ExerciseStyle = ExerciseStyle.AMERICAN
    settlement: str = "physical"

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class BondDetail:
    """Fixed income specifics."""

    issuer: str
    coupon_rate: Decimal
    coupon_frequency: int
    maturity_date: date
    day_count: DayCount
    face_value: Decimal
    first_coupon_date: date | None = None
    quote_basis: str = "percent_of_par"
    is_callable: bool = False
    next_call_date: date | None = None
    next_call_price: Decimal | None = None

    def __post_init__(self) -> None:
        check_decimal_fields(self)

    @property
    def is_zero_coupon(self) -> bool:
        return self.coupon_frequency == 0


@dataclass(frozen=True, slots=True)
class Instrument:
    """A security in the local master."""

    instrument_id: int
    symbol: str
    instrument_type: InstrumentType
    name: str | None = None
    currency: str = "USD"
    exchange: str | None = None
    cusip: str | None = None
    isin: str | None = None
    figi: str | None = None
    sector: str | None = None
    industry: str | None = None
    asset_class: str | None = None
    country: str | None = None
    is_active: bool = True
    source: str = "manual"
    provider_ref: str | None = None
    option: OptionDetail | None = None
    bond: BondDetail | None = None

    @property
    def is_option(self) -> bool:
        return self.instrument_type is InstrumentType.OPTION

    @property
    def is_bond(self) -> bool:
        return self.instrument_type is InstrumentType.BOND

    @property
    def contract_size(self) -> Decimal:
        """Units of exposure per unit of quantity.

        100 for a standard option, 1 for everything else -- read from the
        instrument rather than assumed, which is the point of storing it.
        """
        if self.option is not None:
            return self.option.multiplier
        return Decimal(1)


@dataclass(frozen=True, slots=True)
class Price:
    """One observed price, with the provenance PORT-GIPS-J03 requires."""

    instrument_id: int
    price_date: date
    price: Decimal
    source: str
    as_of: datetime
    #: The GIPS fair-value hierarchy, 1 (observable, active) to 5 (subjective,
    #: unobservable). PORT-GIPS-A02.
    valuation_level: int = 1
    valuation_basis: ValuationBasis = ValuationBasis.EXCHANGE_CLOSE
    is_estimate: bool = False
    currency: str = "USD"
    price_id: int | None = None
    provider_ref: str | None = None

    def __post_init__(self) -> None:
        check_decimal_fields(self)


# ── Ledger ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Transaction:
    """An immutable ledger event.

    Replay order is ``(trade_date, seq, txn_id)``. Not ``created_at``: a wall
    clock is not a total order across machines. Not ``trade_date`` alone: two
    trades on one day in the wrong order consume the wrong lots under FIFO.
    """

    txn_id: int
    account_id: int
    trade_date: date
    seq: int
    txn_type: TransactionType
    net_cash_effect: Decimal
    settlement_date: date | None = None
    instrument_id: int | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    gross_amount: Decimal | None = None
    fees: Decimal = Decimal("0.00")
    commissions: Decimal = Decimal("0.00")
    taxes_withheld: Decimal = Decimal("0.00")
    #: PORT-GIPS-A06: reclaimable withholding is accrued, non-reclaimable
    #: reduces return. Splitting them is the only way to get both right.
    withholding_reclaimable: Decimal | None = None
    #: PORT-GIPS-D01. NULL where a fee is present is an error, enforced by the
    #: schema so it binds every writer.
    fee_class: FeeClass | None = None
    position_id: int | None = None
    #: The other side of a transfer. ONE transaction with two sides, so that
    #: portfolio-level netting is structural rather than a matching heuristic
    #: (ADR 0007).
    counter_account_id: int | None = None
    related_txn_id: int | None = None
    reverses_txn_id: int | None = None
    lot_selection: str | None = None
    relief_method: ReliefMethod | None = None
    ex_date: date | None = None
    pay_date: date | None = None
    is_qualified: bool | None = None
    note: str | None = None
    external_ref: str | None = None
    source: TransactionSource = TransactionSource.MANUAL
    created_at: str = ""

    def __post_init__(self) -> None:
        check_decimal_fields(self)

    @property
    def total_costs(self) -> Decimal:
        """Fees plus commissions. Not including withholding, which is not a cost."""
        return self.fees + self.commissions

    @property
    def order_key(self) -> tuple[date, int, int]:
        """The ledger's total order. See the class docstring."""
        return (self.trade_date, self.seq, self.txn_id)


# ── Positions and lots ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PositionLeg:
    """One instrument's participation in a position, with its role and sign."""

    leg_id: int
    position_id: int
    instrument_id: int
    role: LegRole
    #: +1 long, -1 short.
    sign: int
    quantity: Decimal
    opened_date: date
    closed_date: date | None = None
    status: PositionStatus = PositionStatus.OPEN

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class Position:
    """The container, and the unit of trader intent. May span instruments.

    A covered call is one position, not two. See ADR 0009 for why that is
    structural rather than cosmetic -- chiefly, it makes assignment a
    within-position operation.
    """

    position_id: int
    account_id: int
    strategy_type: StrategyType
    opened_date: date
    status: PositionStatus = PositionStatus.OPEN
    closed_date: date | None = None
    label: str | None = None
    note: str | None = None
    opened_txn_id: int | None = None
    legs: tuple[PositionLeg, ...] = field(default_factory=tuple)

    @property
    def is_multi_leg(self) -> bool:
        return len(self.legs) > 1

    def leg_for(self, instrument_id: int) -> PositionLeg | None:
        for leg in self.legs:
            if leg.instrument_id == instrument_id:
                return leg
        return None


@dataclass(frozen=True, slots=True)
class BasisAdjustment:
    """One explained change to a lot's basis or holding-period start.

    The log of these is the difference between a basis you can defend and one
    you can only assert.
    """

    adjustment_id: int
    lot_id: int
    adjustment_date: date
    reason: BasisAdjustmentReason
    basis_delta: Decimal = Decimal("0.00")
    quantity_delta: Decimal = Decimal("0")
    holding_period_start_after: date | None = None
    txn_id: int | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class Lot:
    """Created by an opening transaction, consumed by closing ones.

    The tax engine's atom. Hangs off a **leg**, not a position: a lot must
    resolve to exactly one instrument and a position need not.
    """

    lot_id: int
    leg_id: int
    position_id: int
    instrument_id: int
    account_id: int
    open_date: date
    open_txn_id: int
    original_quantity: Decimal
    remaining_quantity: Decimal
    per_unit_price: Decimal
    original_cost_basis: Decimal
    adjusted_cost_basis: Decimal
    #: A split does NOT reset this. A spinoff's new shares INHERIT it. Wash
    #: sales (v0.2) will move it forward.
    holding_period_start: date
    allocated_fees: Decimal = Decimal("0.00")
    is_short: bool = False
    status: LotStatus = LotStatus.OPEN
    closed_date: date | None = None
    adjustments: tuple[BasisAdjustment, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        check_decimal_fields(self)

    @property
    def basis_per_unit(self) -> Decimal:
        """Adjusted basis divided by *remaining* quantity.

        Remaining, not original: after a partial disposition the basis has
        been relieved proportionally, and dividing by the original would
        understate what is left.
        """
        if self.remaining_quantity == 0:
            return Decimal("0")
        return self.adjusted_cost_basis / self.remaining_quantity

    @property
    def is_open(self) -> bool:
        return self.status is not LotStatus.CLOSED and self.remaining_quantity > 0


@dataclass(frozen=True, slots=True)
class LotDisposition:
    """One (closing transaction, lot consumed) pair. The tax engine's input."""

    disposition_id: int
    lot_id: int
    txn_id: int
    account_id: int
    instrument_id: int
    disposition_date: date
    quantity: Decimal
    #: Includes option premium where an assignment resolved into stock: a
    #: written call that is assigned adds its premium to PROCEEDS, not to
    #: independent P&L.
    proceeds: Decimal
    cost_basis_relieved: Decimal
    realized_gain: Decimal
    holding_period: HoldingPeriod
    days_held: int
    relief_method: ReliefMethod
    allocated_fees: Decimal = Decimal("0.00")
    #: v0.2. Until wash-sale detection lands this is always None, and the tax
    #: report says on its face that it does not account for wash sales.
    wash_sale_deferred: Decimal | None = None

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class RealizedGain:
    """A disposition with the estimated tax applied.

    The tax figure is an **estimate** and the components are stored so that the
    effective rate is explainable. ADR 0011 states exactly what it does and
    does not model.
    """

    disposition_id: int
    account_id: int
    instrument_id: int
    tax_year: int
    disposition_date: date
    holding_period: HoldingPeriod
    proceeds: Decimal
    cost_basis: Decimal
    gain: Decimal
    is_taxable: bool
    realized_gain_id: int | None = None
    rate_id: int | None = None
    federal_rate: Decimal | None = None
    state_rate: Decimal | None = None
    niit_rate: Decimal | None = None
    estimated_tax: Decimal | None = None

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class CorporateAction:
    """A corporate action as reported, with the parameters needed to apply it.

    This is a fact about the world, not about the portfolio. Applying it
    produces ledger transactions -- and because the parameters live here rather
    than in a free-text note on the ledger row, `pt rebuild` can reproduce the
    action's effect rather than losing it (ADR 0010, CLAUDE.md invariant 3).
    """

    instrument_id: int
    action_type: str
    ex_date: date
    corporate_action_id: int | None = None
    record_date: date | None = None
    pay_date: date | None = None
    split_numerator: Decimal | None = None
    split_denominator: Decimal | None = None
    cash_amount: Decimal | None = None
    target_instrument_id: int | None = None
    target_ratio: Decimal | None = None
    #: Relative fair market values, recorded because a spinoff's basis
    #: allocation is only defensible if its inputs are.
    parent_fmv: Decimal | None = None
    target_fmv: Decimal | None = None
    new_symbol: str | None = None
    source: str = "manual"
    provider_ref: str | None = None
    applied_txn_id: int | None = None

    def __post_init__(self) -> None:
        check_decimal_fields(self)


# ── Valuation ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SnapshotPrice:
    """One price a snapshot consumed, with its provenance.

    PORT-GIPS-J03 requires supporting data for every reported figure; this is
    it. It is also what makes PORT-GIPS-A09 workable -- when a final price
    replaces an estimate, the snapshots that used the estimate are identifiable.
    """

    instrument_id: int
    price: Decimal
    quantity: Decimal
    market_value: Decimal
    source: str
    as_of: datetime
    valuation_level: int
    is_estimate: bool = False
    staleness_days: int = 0
    price_id: int | None = None

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class CashFlow:
    """One external cash flow, at one level, on one day.

    Day resolution is required: PORT-GIPS-C02 mandates daily external cash
    flows for money-weighted returns for periods beginning on or after
    1 January 2020.
    """

    txn_id: int
    account_id: int
    flow_date: date
    amount: Decimal
    is_large: bool = False
    is_in_kind: bool = False

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class ValuationSnapshot:
    """Per account per date. The substrate `pert` needs."""

    account_id: int
    snapshot_date: date
    beginning_market_value: Decimal
    ending_market_value: Decimal
    securities_value: Decimal
    cash_balance: Decimal
    margin_loan: Decimal = Decimal("0.00")
    accrued_interest: Decimal = Decimal("0.00")
    accrued_dividends: Decimal = Decimal("0.00")
    accrued_income: Decimal = Decimal("0.00")
    external_flow_account: Decimal = Decimal("0.00")
    external_flow_portfolio: Decimal = Decimal("0.00")
    income_amount: Decimal = Decimal("0.00")
    fees_amount: Decimal = Decimal("0.00")
    level5_market_value: Decimal = Decimal("0.00")
    #: 0 when any position could not be priced. A snapshot that is not complete
    #: must not be silently used for a return.
    is_complete: bool = True
    uses_estimates: bool = False
    snapshot_id: int | None = None
    prices: tuple[SnapshotPrice, ...] = field(default_factory=tuple)
    flows: tuple[CashFlow, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        check_decimal_fields(self)


# ── Policy and benchmarks ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReturnPolicy:
    """Effective-dated thresholds GIPS requires an entity to define itself.

    A missing policy is an error, not a zero (PORT-GIPS-B03).
    """

    policy_id: int
    effective_from: date
    large_flow_basis: str
    large_flow_value: Decimal
    significant_flow_basis: str | None = None
    significant_flow_value: Decimal | None = None
    materiality_return_bps: Decimal | None = None
    materiality_value: Decimal | None = None
    risk_measure_basis: str = "gross_of_fees"
    note: str | None = None

    def __post_init__(self) -> None:
        check_decimal_fields(self)

    def is_large_flow(self, amount: Decimal, portfolio_value: Decimal) -> bool:
        """Whether *amount* crosses the large-flow threshold in force.

        Compared on the absolute amount: a large withdrawal distorts a return
        exactly as much as a large contribution.
        """
        magnitude = abs(amount)
        if self.large_flow_basis == "amount":
            return magnitude >= self.large_flow_value
        if portfolio_value == 0:
            # A flow into an empty portfolio is definitionally the whole of it.
            return magnitude > 0
        return magnitude / abs(portfolio_value) >= self.large_flow_value


@dataclass(frozen=True, slots=True)
class Benchmark:
    """A benchmark series. Price-only series are refused, not warned about."""

    benchmark_id: int
    name: str
    description: str
    return_type: BenchmarkReturnType
    periodicity: str = "daily"
    is_net_of_withholding: bool | None = None
    rebalance_rule: str | None = None
    is_blend: bool = False
    source: str = "manual"
