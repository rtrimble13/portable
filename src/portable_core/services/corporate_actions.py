"""Corporate actions, and what they do to basis and holding period.

Every function here is pure: it takes lots and an action and returns the
adjusted lots plus a :class:`BasisAdjustment` explaining each change. Nothing
is applied silently, and every change is auditable afterwards from the lot's
adjustment log.

The traps this module exists to get right, each of which changes a **tax
rate** rather than merely a presentation:

* **A split does not reset the holding period.** Quantity and per-share basis
  change; total basis and ``holding_period_start`` do not.
* **A spinoff allocates basis by relative fair market value**, and the new
  shares **inherit the original holding period**.
* **Return of capital reduces basis**, and once basis reaches zero the excess
  becomes capital gain. It is not income for tax, and it is not an external
  cash flow for performance -- two different questions about one event.
* **Option premium follows the stock when the option resolves into stock.** A
  written call that is assigned adds premium to *proceeds*; a long call that is
  exercised adds premium to the acquired stock's *basis*. A written option that
  expires worthless is short-term gain regardless of how long it was open.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_DOWN, Decimal

from portable_core.decimals import (
    allocate,
    is_whole,
    money_context,
    quantize_money,
    quantize_quantity,
)
from portable_core.domain.enums import BasisAdjustmentReason, HoldingPeriod
from portable_core.domain.models import BasisAdjustment, Lot
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_FRACTIONAL_SHARE, E_INVARIANT_BROKEN

__all__ = ["CorporateActionEngine", "SpinoffResult", "SplitResult"]

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class SplitResult:
    lots: tuple[Lot, ...]
    adjustments: tuple[BasisAdjustment, ...]
    #: Fractional shares the account cannot hold, which the caller must resolve
    #: -- normally cash in lieu, which is a disposition, not a rounding.
    fractional_shares: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class SpinoffResult:
    parent_lots: tuple[Lot, ...]
    spun_lots: tuple[Lot, ...]
    adjustments: tuple[BasisAdjustment, ...]
    fractional_shares: Decimal = ZERO


class CorporateActionEngine:
    """Applies corporate actions to lots."""

    # ── splits ───────────────────────────────────────────────────────────────

    def split(
        self,
        lots: list[Lot],
        *,
        numerator: Decimal,
        denominator: Decimal,
        ex_date: date,
        txn_id: int | None = None,
        allows_fractional: bool = False,
        next_adjustment_id: int = 1,
    ) -> SplitResult:
        """Apply a forward or reverse split.

        A 3-for-1 split is ``numerator=3, denominator=1``; a 1-for-10 reverse
        split is ``numerator=1, denominator=10``.

        **Total cost basis is unchanged.** Only quantity and therefore
        per-share basis move. That is the whole economic content of a split:
        the same claim, divided differently.

        **The holding period is not reset.** ``holding_period_start`` is
        carried through untouched, and the adjustment row records it
        explicitly so the assertion is checkable rather than implicit. Getting
        this wrong turns a long-term gain into a short-term one -- a rate
        error, not a rounding.
        """
        if numerator <= 0 or denominator <= 0:
            raise ValidationError(
                f"split ratio must be positive, got {numerator}-for-{denominator}",
                code=E_INVARIANT_BROKEN,
            )

        adjusted: list[Lot] = []
        adjustments: list[BasisAdjustment] = []
        fractional_total = ZERO

        for offset, lot in enumerate(lots):
            with money_context():
                # Multiply THEN divide. Computing the ratio first and
                # multiplying by it is inexact for any ratio that does not
                # terminate in decimal: 100 shares through a 3-for-1 and back
                # through a 1-for-3 gives 99.999... and floors to 99, silently
                # destroying a share. This order is exact for every integer
                # ratio, which is every real split.
                exact_quantity = lot.remaining_quantity * numerator / denominator
                new_quantity = (
                    quantize_quantity(exact_quantity)
                    if allows_fractional
                    else exact_quantity.to_integral_value(rounding=ROUND_DOWN)
                )
                fractional = exact_quantity - new_quantity
                original_exact = lot.original_quantity * numerator / denominator
                new_original = (
                    quantize_quantity(original_exact)
                    if allows_fractional
                    else original_exact.to_integral_value(rounding=ROUND_DOWN)
                )
                # Basis is unchanged in total; per-unit follows the new count.
                per_unit = lot.adjusted_cost_basis / new_quantity if new_quantity else ZERO

            fractional_total += fractional

            adjusted.append(
                replace(
                    lot,
                    remaining_quantity=new_quantity,
                    original_quantity=new_original,
                    per_unit_price=per_unit,
                    # holding_period_start deliberately untouched.
                )
            )
            adjustments.append(
                BasisAdjustment(
                    adjustment_id=next_adjustment_id + offset,
                    lot_id=lot.lot_id,
                    adjustment_date=ex_date,
                    reason=(
                        BasisAdjustmentReason.SPLIT
                        if numerator > denominator
                        else BasisAdjustmentReason.REVERSE_SPLIT
                    ),
                    basis_delta=ZERO,
                    quantity_delta=new_quantity - lot.remaining_quantity,
                    holding_period_start_after=lot.holding_period_start,
                    txn_id=txn_id,
                    note=(
                        f"{numerator}-for-{denominator} split; total basis unchanged, "
                        "holding period not reset"
                    ),
                )
            )

        if fractional_total > 0 and not allows_fractional:
            # Not rounded away: cash in lieu is a disposition with its own
            # basis and holding period, and swallowing it here would hide a
            # taxable event (CLAUDE.md invariant 9).
            pass

        return SplitResult(
            lots=tuple(adjusted),
            adjustments=tuple(adjustments),
            fractional_shares=fractional_total,
        )

    # ── spinoffs ─────────────────────────────────────────────────────────────

    def spinoff(
        self,
        lots: list[Lot],
        *,
        ratio: Decimal,
        parent_fmv: Decimal,
        spun_fmv: Decimal,
        ex_date: date,
        spun_instrument_id: int,
        spun_leg_id: int,
        spun_position_id: int,
        txn_id: int | None = None,
        allows_fractional: bool = False,
        next_lot_id: int = 1,
        next_adjustment_id: int = 1,
    ) -> SpinoffResult:
        """Allocate basis across parent and spun shares by relative fair value.

        Two rules, both of which change a tax rate if got wrong:

        1. **Basis is allocated by relative fair market value**, not by share
           count and not by the spinoff ratio. If the parent is worth $90 and
           the spun shares $10 immediately after, 90% of the original basis
           stays with the parent. The FMVs are recorded on the action so the
           allocation is defensible rather than asserted.
        2. **The spun shares inherit the parent's holding period.** They are
           not newly acquired. A spinoff from a lot held for five years
           produces long-term spun shares on day one.

        Args:
            ratio: spun shares received per parent share held.
            parent_fmv: fair market value per parent share after the spinoff.
            spun_fmv: fair market value per spun share.
        """
        if parent_fmv < 0 or spun_fmv < 0:
            raise ValidationError(
                "fair market values for a spinoff basis allocation must be non-negative",
                code=E_INVARIANT_BROKEN,
                parent_fmv=str(parent_fmv),
                spun_fmv=str(spun_fmv),
            )

        parent_lots: list[Lot] = []
        spun_lots: list[Lot] = []
        adjustments: list[BasisAdjustment] = []
        fractional_total = ZERO
        lot_id = next_lot_id
        adjustment_id = next_adjustment_id

        for lot in lots:
            with money_context():
                spun_exact = lot.remaining_quantity * ratio
                spun_quantity = (
                    quantize_quantity(spun_exact)
                    if allows_fractional
                    else spun_exact.to_integral_value(rounding=ROUND_DOWN)
                )
                fractional_total += spun_exact - spun_quantity

                parent_value = lot.remaining_quantity * parent_fmv
                spun_value = spun_quantity * spun_fmv

            if parent_value + spun_value == 0:
                # No observable values: refuse rather than split the basis
                # arbitrarily. An arbitrary allocation is a wrong basis on both
                # sides, and it will not look wrong.
                raise ValidationError(
                    "cannot allocate spinoff basis: both fair market values are zero",
                    code=E_INVARIANT_BROKEN,
                    remedy=(
                        "Supply --parent-fmv and --spun-fmv from the company's "
                        "Form 8937 or the post-spinoff market prices."
                    ),
                    lot_id=lot.lot_id,
                )

            parent_basis, spun_basis = allocate(
                lot.adjusted_cost_basis, [parent_value, spun_value]
            )

            with money_context():
                parent_per_unit = (
                    parent_basis / lot.remaining_quantity if lot.remaining_quantity else ZERO
                )
                spun_per_unit = spun_basis / spun_quantity if spun_quantity else ZERO

            parent_lots.append(
                replace(lot, adjusted_cost_basis=parent_basis, per_unit_price=parent_per_unit)
            )
            adjustments.append(
                BasisAdjustment(
                    adjustment_id=adjustment_id,
                    lot_id=lot.lot_id,
                    adjustment_date=ex_date,
                    reason=BasisAdjustmentReason.SPINOFF,
                    basis_delta=parent_basis - lot.adjusted_cost_basis,
                    holding_period_start_after=lot.holding_period_start,
                    txn_id=txn_id,
                    note=(
                        f"spinoff: basis allocated by relative FMV "
                        f"(parent {parent_fmv}, spun {spun_fmv}); "
                        "holding period unchanged"
                    ),
                )
            )
            adjustment_id += 1

            if spun_quantity > 0:
                spun_lots.append(
                    Lot(
                        lot_id=lot_id,
                        leg_id=spun_leg_id,
                        position_id=spun_position_id,
                        instrument_id=spun_instrument_id,
                        account_id=lot.account_id,
                        open_date=ex_date,
                        open_txn_id=txn_id or lot.open_txn_id,
                        original_quantity=spun_quantity,
                        remaining_quantity=spun_quantity,
                        per_unit_price=spun_per_unit,
                        original_cost_basis=spun_basis,
                        adjusted_cost_basis=spun_basis,
                        # The inheritance rule. NOT ex_date.
                        holding_period_start=lot.holding_period_start,
                        is_short=lot.is_short,
                    )
                )
                lot_id += 1

        return SpinoffResult(
            parent_lots=tuple(parent_lots),
            spun_lots=tuple(spun_lots),
            adjustments=tuple(adjustments),
            fractional_shares=fractional_total,
        )

    # ── return of capital ────────────────────────────────────────────────────

    def return_of_capital(
        self,
        lots: list[Lot],
        *,
        amount_per_share: Decimal,
        pay_date: date,
        txn_id: int | None = None,
        next_adjustment_id: int = 1,
    ) -> tuple[tuple[Lot, ...], tuple[BasisAdjustment, ...], Decimal]:
        """Reduce basis by a return of capital; the excess becomes gain.

        Return of capital is **not income** for tax purposes: it is the issuer
        handing back part of what was invested, so it reduces basis. **Once
        basis reaches zero the excess is capital gain**, recognised
        immediately -- there is no negative basis.

        Note the separate question this does not answer: for *performance*
        purposes return of capital is not an external cash flow either. Both
        facts are true and they are about different things
        (:mod:`portable_core.services.cash_flow`).

        Returns:
            ``(lots, adjustments, excess_gain)``. The excess is the caller's to
            record as a realized gain.
        """
        adjusted: list[Lot] = []
        adjustments: list[BasisAdjustment] = []
        excess_total = ZERO

        for offset, lot in enumerate(lots):
            with money_context():
                distribution = quantize_money(amount_per_share * lot.remaining_quantity)
                reduction = min(distribution, lot.adjusted_cost_basis)
                excess = distribution - reduction
                new_basis = lot.adjusted_cost_basis - reduction
                per_unit = (
                    new_basis / lot.remaining_quantity if lot.remaining_quantity else ZERO
                )

            excess_total += excess
            adjusted.append(
                replace(lot, adjusted_cost_basis=new_basis, per_unit_price=per_unit)
            )
            adjustments.append(
                BasisAdjustment(
                    adjustment_id=next_adjustment_id + offset,
                    lot_id=lot.lot_id,
                    adjustment_date=pay_date,
                    reason=BasisAdjustmentReason.RETURN_OF_CAPITAL,
                    basis_delta=-reduction,
                    holding_period_start_after=lot.holding_period_start,
                    txn_id=txn_id,
                    note=(
                        f"return of capital {amount_per_share}/share"
                        + (f"; {excess} exceeded basis and is capital gain" if excess else "")
                    ),
                )
            )

        return tuple(adjusted), tuple(adjustments), excess_total

    # ── option premium ───────────────────────────────────────────────────────

    @staticmethod
    def premium_on_exercise(
        stock_basis: Decimal,
        premium_paid: Decimal,
        fees: Decimal = ZERO,
    ) -> Decimal:
        """A long call exercised: premium is added to the acquired stock's **basis**.

        The option does not produce independent P&L. Its cost becomes part of
        what the stock cost, and the gain is recognised when the stock is
        eventually sold.
        """
        with money_context():
            return quantize_money(stock_basis + premium_paid + fees)

    @staticmethod
    def premium_on_assignment(
        stock_proceeds: Decimal,
        premium_received: Decimal,
        fees: Decimal = ZERO,
    ) -> Decimal:
        """A written call assigned: premium is added to the stock sale's **proceeds**.

        The premium is not a separate short-term gain once the option resolves
        into stock. Treating it as one both double-counts it and applies the
        wrong holding period, because the stock's own holding period governs.
        """
        with money_context():
            return quantize_money(stock_proceeds + premium_received - fees)

    @staticmethod
    def premium_on_expiration(premium: Decimal) -> tuple[Decimal, HoldingPeriod]:
        """A written option expiring worthless: **short-term** gain, always.

        Regardless of how long the option was open. There is no long-term
        treatment for a lapsed written option.
        """
        return quantize_money(premium), HoldingPeriod.SHORT

    # ── fractional shares ────────────────────────────────────────────────────

    @staticmethod
    def require_whole_shares(
        quantity: Decimal,
        *,
        allows_fractional: bool,
        instrument_symbol: str,
        action: str,
    ) -> None:
        """Refuse a fractional share an account cannot hold.

        CLAUDE.md invariant 9. Rounding it away hides a taxable event: the
        broker pays cash in lieu, which is a disposition with its own basis and
        holding period.
        """
        if allows_fractional or is_whole(quantity):
            return
        raise ValidationError(
            f"{action} would create {quantity} shares of {instrument_symbol}, "
            "and this account does not hold fractional shares",
            code=E_FRACTIONAL_SHARE,
            remedy=(
                "Record the cash in lieu as a separate disposition with `pt sell`, "
                "or set --allows-fractional on the account if the custodian permits it. "
                "portable will not round the fraction away, because cash in lieu is a "
                "taxable event."
            ),
            quantity=str(quantity),
            symbol=instrument_symbol,
        )
