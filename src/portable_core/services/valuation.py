"""The valuation engine: market value, accruals, and snapshots.

Builds the substrate `pert` will consume. Every decision here is made against
`docs/gips-standard.md` and cites its requirement, because a valuation error
does not announce itself -- it shows up as a return that is slightly wrong for
a year.

What is deliberate rather than incidental:

* **Unadjusted prices only** (``PORT-GIPS-A01``). Adjusted prices are not fair
  values on the measurement date and would double-count splits.
* **Accrued income is part of market value**, not a memo (``PORT-GIPS-A06``).
  Interest accrual is *required*; dividend accrual on the ex-date is
  *recommended* -- `portable` does both, and must not describe the second as
  required.
* **Market value is net of the margin loan** (``PORT-GIPS-D04``). Assets are
  not grossed up as if the leverage did not exist.
* **Cash is always in the return** (``PORT-GIPS-A07``) unless an account is
  explicitly designated operating cash.
* **Every price consumed is recorded** with its source, as-of, level, and
  estimate flag (``PORT-GIPS-J03``, ``A09``), so a return traces back to the
  ticks that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from portable_core.decimals import money_context, quantize_money
from portable_core.domain.dates import accrual_fraction
from portable_core.domain.enums import CashTreatment, FlowLevel
from portable_core.domain.models import (
    Account,
    CashFlow,
    Instrument,
    Price,
    ReturnPolicy,
    SnapshotPrice,
    Transaction,
    ValuationSnapshot,
)
from portable_core.errors import DataUnavailableError
from portable_core.errors.kinds import E_PRICE_MISSING, E_PRICE_STALE
from portable_core.services import cash_flow

__all__ = ["Holding", "ValuationEngine"]

ZERO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class Holding:
    """What an account holds of one instrument on a date."""

    instrument: Instrument
    quantity: Decimal
    #: For bonds: the last coupon paid on or before the valuation date, and the
    #: next one due. Needed for accrued interest, which is part of market
    #: value.
    last_coupon_date: date | None = None
    next_coupon_date: date | None = None


class ValuationEngine:
    """Prices holdings and assembles valuation snapshots."""

    def __init__(self, *, staleness_tolerance_days: int = 5) -> None:
        #: Beyond this, a price is not "the best approximation of current fair
        #: value" (PORT-GIPS-A09) and the command stops with exit code 5.
        self.staleness_tolerance_days = staleness_tolerance_days

    # ── pricing ──────────────────────────────────────────────────────────────

    def price_holding(
        self,
        holding: Holding,
        price: Price | None,
        as_of: date,
        *,
        offline: bool = False,
    ) -> SnapshotPrice:
        """Value one holding, refusing rather than guessing.

        Raises:
            DataUnavailableError: when there is no price
                (``PT-E-PRICE-MISSING``) or the newest one is stale beyond
                tolerance (``PT-E-PRICE-STALE``). Both exit 5. Carrying the
                last known price forward silently is what produces a flat
                return series that looks like a calm market.
        """
        symbol = holding.instrument.symbol
        if price is None:
            raise DataUnavailableError(
                f"no price for {symbol} on or before {as_of.isoformat()}",
                code=E_PRICE_MISSING,
                remedy=(
                    f"Load one with `pt price load` or set it with "
                    f"`pt price set {symbol} --date {as_of.isoformat()} --price X`."
                    + (" Running --offline; the cache has nothing." if offline else "")
                ),
                symbol=symbol,
                as_of=as_of.isoformat(),
            )

        staleness = (as_of - price.price_date).days
        if staleness > self.staleness_tolerance_days:
            raise DataUnavailableError(
                f"newest price for {symbol} is {staleness} days old "
                f"({price.price_date.isoformat()}), beyond the "
                f"{self.staleness_tolerance_days}-day tolerance",
                code=E_PRICE_STALE,
                remedy=(
                    "Refresh prices, raise --staleness-tolerance if the gap is "
                    "genuine (a holiday, a halted security), or price it by hand. "
                    "portable will not carry a stale price forward silently."
                ),
                symbol=symbol,
                price_date=price.price_date.isoformat(),
                as_of=as_of.isoformat(),
                staleness_days=staleness,
            )

        with money_context():
            market_value = quantize_money(
                price.price * holding.quantity * holding.instrument.contract_size
            )

        return SnapshotPrice(
            instrument_id=holding.instrument.instrument_id,
            price=price.price,
            quantity=holding.quantity,
            market_value=market_value,
            source=price.source,
            as_of=price.as_of,
            valuation_level=price.valuation_level,
            is_estimate=price.is_estimate,
            staleness_days=max(staleness, 0),
            price_id=price.price_id,
        )

    # ── accruals ─────────────────────────────────────────────────────────────

    def accrued_interest(self, holding: Holding, as_of: date) -> Decimal:
        """Accrued interest on a bond holding. ``PORT-GIPS-A06``.

        **This is part of market value, not a memo.** A bond bought between
        coupons pays accrued interest to the seller; that is a receivable the
        next coupon extinguishes, and it is not basis. Omitting it understates
        market value between coupons and produces a sawtooth return series that
        looks like the bond is oscillating.

        Accrual for interest-bearing instruments is *required* under
        Firms 2.A.10 / AO 22.A.7 -- not merely recommended, which is the
        asymmetry with dividends worth keeping straight.
        """
        bond = holding.instrument.bond
        if bond is None or bond.is_zero_coupon:
            return ZERO
        if holding.last_coupon_date is None or holding.next_coupon_date is None:
            return ZERO

        fraction = accrual_fraction(
            holding.last_coupon_date, as_of, holding.next_coupon_date, bond.day_count
        )
        with money_context():
            annual = bond.face_value * bond.coupon_rate * holding.quantity
            period = annual / Decimal(bond.coupon_frequency)
            return quantize_money(period * fraction)

    @staticmethod
    def accrued_dividends(
        declared: list[Transaction],
        as_of: date,
    ) -> Decimal:
        """Dividends entitled but not yet paid. ``PORT-GIPS-A06``.

        Entitlement is fixed on the **ex-date**; cash arrives on the
        **pay-date**. A dividend whose ex-date has passed and whose pay-date has
        not is owed to the portfolio and belongs in ending market value.
        Accruing on the wrong date shifts return across a period boundary --
        which shows up as one good quarter and one bad one rather than as an
        error.

        Note this is a **recommendation** (Firms 2.B.3), not a requirement, and
        must not be described as one. `portable` adopts it because the
        alternative moves return between periods for no reason.
        """
        with money_context():
            return quantize_money(
                sum(
                    (
                        t.gross_amount or ZERO
                        for t in declared
                        if t.ex_date is not None
                        and t.ex_date <= as_of
                        and (t.pay_date is None or t.pay_date > as_of)
                    ),
                    ZERO,
                )
            )

    # ── snapshots ────────────────────────────────────────────────────────────

    def build_snapshot(
        self,
        account: Account,
        as_of: date,
        *,
        priced: list[SnapshotPrice],
        cash_balance: Decimal,
        margin_loan: Decimal = ZERO,
        accrued_interest: Decimal = ZERO,
        accrued_dividends: Decimal = ZERO,
        beginning_market_value: Decimal = ZERO,
        transactions: list[Transaction] | None = None,
        policy: ReturnPolicy | None = None,
        income_amount: Decimal = ZERO,
        fees_amount: Decimal = ZERO,
        incomplete_symbols: tuple[str, ...] = (),
    ) -> ValuationSnapshot:
        """Assemble the snapshot for one account on one date.

        The ending market value is::

            securities + cash - margin_loan + accrued_income

        Each term is there for a reason recorded above: cash because
        ``PORT-GIPS-A07`` puts it in every return, the margin loan subtracted
        because ``PORT-GIPS-D04`` forbids grossing up leverage, and accrued
        income because ``PORT-GIPS-A06`` makes it part of value rather than a
        note beside it.

        Operating cash (``PORT-GIPS-A07`` / AO 22.B.9) is excluded from
        securities value only when the account carries that explicit flag --
        never implicitly.
        """
        with money_context():
            securities = quantize_money(sum((p.market_value for p in priced), ZERO))
            accrued_income = quantize_money(accrued_interest + accrued_dividends)

            includes_cash = account.cash_treatment is CashTreatment.INVESTED
            effective_cash = cash_balance if includes_cash else ZERO

            ending = quantize_money(securities + effective_cash - margin_loan + accrued_income)
            level5 = quantize_money(
                sum((p.market_value for p in priced if p.valuation_level == 5), ZERO)
            )

        flows = self._flows_for(
            transactions or [],
            as_of,
            policy=policy,
            portfolio_value=beginning_market_value or ending,
        )

        with money_context():
            account_flow = quantize_money(
                sum((f.amount for f in flows[FlowLevel.ACCOUNT]), ZERO)
            )
            portfolio_flow = quantize_money(
                sum((f.amount for f in flows[FlowLevel.PORTFOLIO]), ZERO)
            )

        return ValuationSnapshot(
            account_id=account.account_id,
            snapshot_date=as_of,
            beginning_market_value=beginning_market_value,
            ending_market_value=ending,
            securities_value=securities,
            cash_balance=cash_balance,
            margin_loan=margin_loan,
            accrued_interest=accrued_interest,
            accrued_dividends=accrued_dividends,
            accrued_income=accrued_income,
            external_flow_account=account_flow,
            external_flow_portfolio=portfolio_flow,
            income_amount=income_amount,
            fees_amount=fees_amount,
            level5_market_value=level5,
            is_complete=not incomplete_symbols,
            uses_estimates=any(p.is_estimate for p in priced),
            prices=tuple(priced),
            flows=tuple(flows[FlowLevel.ACCOUNT]),
        )

    @staticmethod
    def _flows_for(
        transactions: list[Transaction],
        as_of: date,
        *,
        policy: ReturnPolicy | None,
        portfolio_value: Decimal,
    ) -> dict[FlowLevel, list[CashFlow]]:
        """External flows on *as_of*, at both levels.

        Classification comes from :mod:`portable_core.services.cash_flow` and
        is not re-derived here -- that is the whole point of ADR 0007, and this
        is the call site most likely to be tempted.
        """
        result: dict[FlowLevel, list[CashFlow]] = {
            FlowLevel.ACCOUNT: [],
            FlowLevel.PORTFOLIO: [],
        }
        for txn in transactions:
            if txn.trade_date != as_of:
                continue
            for level in FlowLevel:
                classified = cash_flow.classify(txn, level)
                if not classified.is_external:
                    continue
                result[level].append(
                    CashFlow(
                        txn_id=txn.txn_id,
                        account_id=txn.account_id,
                        flow_date=classified.flow_date,
                        amount=classified.amount,
                        is_large=(
                            policy.is_large_flow(classified.amount, portfolio_value)
                            if policy is not None
                            else False
                        ),
                        is_in_kind=classified.is_in_kind,
                    )
                )
        return result

    @staticmethod
    def level5_percentage(snapshot: ValuationSnapshot) -> Decimal:
        """Share of market value priced on unobservable inputs.

        ``PORT-GIPS-H05``. For a listed portfolio this is zero -- and it is
        rendered anyway, because "0%" is information and a missing row is not.
        """
        if snapshot.ending_market_value == 0:
            return ZERO
        with money_context():
            return snapshot.level5_market_value / snapshot.ending_market_value
