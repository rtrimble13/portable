"""Row-to-domain-object mapping.

Kept out of the repositories so that the SQL in those files is readable as SQL.
Every ``Decimal`` conversion here is explicit (ADR 0005): there is no automatic
converter, so a column that is forgotten surfaces as a ``str`` where a
``Decimal`` was expected rather than as a value that silently behaves like text.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from portable_core.decimals import from_text
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
from portable_core.domain.models import (
    Account,
    BasisAdjustment,
    Benchmark,
    BondDetail,
    Instrument,
    Lot,
    LotDisposition,
    OptionDetail,
    Position,
    PositionLeg,
    Price,
    RealizedGain,
    ReturnPolicy,
    TaxRateSchedule,
    Transaction,
)

__all__ = [
    "to_account",
    "to_basis_adjustment",
    "to_benchmark",
    "to_date",
    "to_decimal",
    "to_disposition",
    "to_instrument",
    "to_lot",
    "to_position",
    "to_position_leg",
    "to_price",
    "to_realized_gain",
    "to_return_policy",
    "to_tax_rate_schedule",
    "to_transaction",
]


# ── primitives ───────────────────────────────────────────────────────────────


def to_decimal(value: Any) -> Decimal:
    """A NOT NULL decimal column."""
    return from_text(str(value))


def to_optional_decimal(value: Any) -> Decimal | None:
    return None if value is None else from_text(str(value))


def to_date(value: Any) -> date:
    return date.fromisoformat(str(value))


def to_optional_date(value: Any) -> date | None:
    return None if value is None else date.fromisoformat(str(value))


def to_datetime(value: Any) -> datetime:
    """Parse an ISO-8601 UTC timestamp.

    ``fromisoformat`` did not accept a trailing ``Z`` before Python 3.11 and
    still does not accept every ISO form, so the substitution is explicit.
    Timestamps are always stored with the ``Z``, so this is total over what
    `portable` writes.
    """
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def to_bool(value: Any) -> bool:
    return bool(int(value))


def to_optional_bool(value: Any) -> bool | None:
    return None if value is None else bool(int(value))


# ── domain objects ───────────────────────────────────────────────────────────


def to_account(row: sqlite3.Row) -> Account:
    return Account(
        account_id=int(row["account_id"]),
        name=str(row["name"]),
        account_type=AccountType(row["account_type"]),
        opened_date=to_date(row["opened_date"]),
        status=AccountStatus(row["status"]),
        closed_date=to_optional_date(row["closed_date"]),
        custodian=row["custodian"],
        account_alias=row["account_alias"],
        cash_treatment=CashTreatment(row["cash_treatment"]),
        default_relief_method=ReliefMethod(row["default_relief_method"]),
        allows_fractional=to_bool(row["allows_fractional"]),
        sweep_instrument_id=row["sweep_instrument_id"],
        currency=str(row["currency"]),
        note=row["note"],
    )


def to_tax_rate_schedule(row: sqlite3.Row) -> TaxRateSchedule:
    return TaxRateSchedule(
        rate_id=int(row["rate_id"]),
        account_id=int(row["account_id"]),
        effective_from=to_date(row["effective_from"]),
        short_term_federal=to_decimal(row["short_term_federal"]),
        long_term_federal=to_decimal(row["long_term_federal"]),
        state=to_decimal(row["state"]),
        niit=to_decimal(row["niit"]),
        qualified_dividend=to_optional_decimal(row["qualified_dividend"]),
        note=row["note"],
    )


def to_instrument(
    row: sqlite3.Row,
    option_row: sqlite3.Row | None = None,
    bond_row: sqlite3.Row | None = None,
) -> Instrument:
    option = None
    if option_row is not None:
        option = OptionDetail(
            underlier_instrument_id=int(option_row["underlier_instrument_id"]),
            option_right=OptionRight(option_row["option_right"]),
            strike=to_decimal(option_row["strike"]),
            expiry=to_date(option_row["expiry"]),
            multiplier=to_decimal(option_row["multiplier"]),
            occ_symbol=option_row["occ_symbol"],
            exercise_style=ExerciseStyle(option_row["exercise_style"]),
            settlement=str(option_row["settlement"]),
        )
    bond = None
    if bond_row is not None:
        bond = BondDetail(
            issuer=str(bond_row["issuer"]),
            coupon_rate=to_decimal(bond_row["coupon_rate"]),
            coupon_frequency=int(bond_row["coupon_frequency"]),
            maturity_date=to_date(bond_row["maturity_date"]),
            day_count=DayCount(bond_row["day_count"]),
            face_value=to_decimal(bond_row["face_value"]),
            first_coupon_date=to_optional_date(bond_row["first_coupon_date"]),
            quote_basis=str(bond_row["quote_basis"]),
            is_callable=to_bool(bond_row["is_callable"]),
            next_call_date=to_optional_date(bond_row["next_call_date"]),
            next_call_price=to_optional_decimal(bond_row["next_call_price"]),
        )
    return Instrument(
        instrument_id=int(row["instrument_id"]),
        symbol=str(row["symbol"]),
        instrument_type=InstrumentType(row["instrument_type"]),
        name=row["name"],
        currency=str(row["currency"]),
        exchange=row["exchange"],
        cusip=row["cusip"],
        isin=row["isin"],
        figi=row["figi"],
        sector=row["sector"],
        industry=row["industry"],
        asset_class=row["asset_class"],
        country=row["country"],
        is_active=to_bool(row["is_active"]),
        source=str(row["source"]),
        provider_ref=row["provider_ref"],
        option=option,
        bond=bond,
    )


def to_price(row: sqlite3.Row) -> Price:
    return Price(
        instrument_id=int(row["instrument_id"]),
        price_date=to_date(row["price_date"]),
        price=to_decimal(row["price"]),
        source=str(row["source"]),
        as_of=to_datetime(row["as_of"]),
        valuation_level=int(row["valuation_level"]),
        valuation_basis=ValuationBasis(row["valuation_basis"]),
        is_estimate=to_bool(row["is_estimate"]),
        currency=str(row["currency"]),
        price_id=int(row["price_id"]),
        provider_ref=row["provider_ref"],
    )


def to_transaction(row: sqlite3.Row) -> Transaction:
    return Transaction(
        txn_id=int(row["txn_id"]),
        account_id=int(row["account_id"]),
        trade_date=to_date(row["trade_date"]),
        seq=int(row["seq"]),
        txn_type=TransactionType(row["txn_type"]),
        net_cash_effect=to_decimal(row["net_cash_effect"]),
        settlement_date=to_optional_date(row["settlement_date"]),
        instrument_id=row["instrument_id"],
        quantity=to_optional_decimal(row["quantity"]),
        price=to_optional_decimal(row["price"]),
        gross_amount=to_optional_decimal(row["gross_amount"]),
        fees=to_decimal(row["fees"]),
        commissions=to_decimal(row["commissions"]),
        taxes_withheld=to_decimal(row["taxes_withheld"]),
        withholding_reclaimable=to_optional_decimal(row["withholding_reclaimable"]),
        fee_class=FeeClass(row["fee_class"]) if row["fee_class"] else None,
        position_id=row["position_id"],
        counter_account_id=row["counter_account_id"],
        related_txn_id=row["related_txn_id"],
        reverses_txn_id=row["reverses_txn_id"],
        lot_selection=row["lot_selection"],
        relief_method=ReliefMethod(row["relief_method"]) if row["relief_method"] else None,
        ex_date=to_optional_date(row["ex_date"]),
        pay_date=to_optional_date(row["pay_date"]),
        is_qualified=to_optional_bool(row["is_qualified"]),
        note=row["note"],
        external_ref=row["external_ref"],
        source=TransactionSource(row["source"]),
        created_at=str(row["created_at"]),
    )


def to_position(row: sqlite3.Row, legs: tuple[PositionLeg, ...] = ()) -> Position:
    return Position(
        position_id=int(row["position_id"]),
        account_id=int(row["account_id"]),
        strategy_type=StrategyType(row["strategy_type"]),
        opened_date=to_date(row["opened_date"]),
        status=PositionStatus(row["status"]),
        closed_date=to_optional_date(row["closed_date"]),
        label=row["label"],
        note=row["note"],
        opened_txn_id=row["opened_txn_id"],
        legs=legs,
    )


def to_position_leg(row: sqlite3.Row) -> PositionLeg:
    return PositionLeg(
        leg_id=int(row["leg_id"]),
        position_id=int(row["position_id"]),
        instrument_id=int(row["instrument_id"]),
        role=LegRole(row["role"]),
        sign=int(row["sign"]),
        quantity=to_decimal(row["quantity"]),
        opened_date=to_date(row["opened_date"]),
        closed_date=to_optional_date(row["closed_date"]),
        status=PositionStatus(row["status"]),
    )


def to_lot(row: sqlite3.Row, adjustments: tuple[BasisAdjustment, ...] = ()) -> Lot:
    return Lot(
        lot_id=int(row["lot_id"]),
        leg_id=int(row["leg_id"]),
        position_id=int(row["position_id"]),
        instrument_id=int(row["instrument_id"]),
        account_id=int(row["account_id"]),
        open_date=to_date(row["open_date"]),
        open_txn_id=int(row["open_txn_id"]),
        original_quantity=to_decimal(row["original_quantity"]),
        remaining_quantity=to_decimal(row["remaining_quantity"]),
        per_unit_price=to_decimal(row["per_unit_price"]),
        original_cost_basis=to_decimal(row["original_cost_basis"]),
        adjusted_cost_basis=to_decimal(row["adjusted_cost_basis"]),
        holding_period_start=to_date(row["holding_period_start"]),
        allocated_fees=to_decimal(row["allocated_fees"]),
        is_short=to_bool(row["is_short"]),
        status=LotStatus(row["status"]),
        closed_date=to_optional_date(row["closed_date"]),
        adjustments=adjustments,
    )


def to_basis_adjustment(row: sqlite3.Row) -> BasisAdjustment:
    return BasisAdjustment(
        adjustment_id=int(row["adjustment_id"]),
        lot_id=int(row["lot_id"]),
        adjustment_date=to_date(row["adjustment_date"]),
        reason=BasisAdjustmentReason(row["reason"]),
        basis_delta=to_decimal(row["basis_delta"]),
        quantity_delta=to_decimal(row["quantity_delta"]),
        holding_period_start_after=to_optional_date(row["holding_period_start_after"]),
        txn_id=row["txn_id"],
        note=row["note"],
    )


def to_disposition(row: sqlite3.Row) -> LotDisposition:
    return LotDisposition(
        disposition_id=int(row["disposition_id"]),
        lot_id=int(row["lot_id"]),
        txn_id=int(row["txn_id"]),
        account_id=int(row["account_id"]),
        instrument_id=int(row["instrument_id"]),
        disposition_date=to_date(row["disposition_date"]),
        quantity=to_decimal(row["quantity"]),
        proceeds=to_decimal(row["proceeds"]),
        cost_basis_relieved=to_decimal(row["cost_basis_relieved"]),
        realized_gain=to_decimal(row["realized_gain"]),
        holding_period=HoldingPeriod(row["holding_period"]),
        days_held=int(row["days_held"]),
        relief_method=ReliefMethod(row["relief_method"]),
        allocated_fees=to_decimal(row["allocated_fees"]),
        wash_sale_deferred=to_optional_decimal(row["wash_sale_deferred"]),
    )


def to_realized_gain(row: sqlite3.Row) -> RealizedGain:
    return RealizedGain(
        disposition_id=int(row["disposition_id"]),
        account_id=int(row["account_id"]),
        instrument_id=int(row["instrument_id"]),
        tax_year=int(row["tax_year"]),
        disposition_date=to_date(row["disposition_date"]),
        holding_period=HoldingPeriod(row["holding_period"]),
        proceeds=to_decimal(row["proceeds"]),
        cost_basis=to_decimal(row["cost_basis"]),
        gain=to_decimal(row["gain"]),
        is_taxable=to_bool(row["is_taxable"]),
        realized_gain_id=row["realized_gain_id"],
        rate_id=row["rate_id"],
        federal_rate=to_optional_decimal(row["federal_rate"]),
        state_rate=to_optional_decimal(row["state_rate"]),
        niit_rate=to_optional_decimal(row["niit_rate"]),
        estimated_tax=to_optional_decimal(row["estimated_tax"]),
    )


def to_return_policy(row: sqlite3.Row) -> ReturnPolicy:
    return ReturnPolicy(
        policy_id=int(row["policy_id"]),
        effective_from=to_date(row["effective_from"]),
        large_flow_basis=str(row["large_flow_basis"]),
        large_flow_value=to_decimal(row["large_flow_value"]),
        significant_flow_basis=row["significant_flow_basis"],
        significant_flow_value=to_optional_decimal(row["significant_flow_value"]),
        materiality_return_bps=to_optional_decimal(row["materiality_return_bps"]),
        materiality_value=to_optional_decimal(row["materiality_value"]),
        risk_measure_basis=str(row["risk_measure_basis"]),
        note=row["note"],
    )


def to_benchmark(row: sqlite3.Row) -> Benchmark:
    return Benchmark(
        benchmark_id=int(row["benchmark_id"]),
        name=str(row["name"]),
        description=str(row["description"]),
        return_type=BenchmarkReturnType(row["return_type"]),
        periodicity=str(row["periodicity"]),
        is_net_of_withholding=to_optional_bool(row["is_net_of_withholding"]),
        rebalance_rule=row["rebalance_rule"],
        is_blend=to_bool(row["is_blend"]),
        source=str(row["source"]),
    )
