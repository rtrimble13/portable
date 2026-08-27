"""The position engine: lifecycle, legs, and the long/short flip.

ADR 0009. A position is the container and the unit of trader *intent*; a leg
binds one instrument to it with a role and a sign; lots hang off legs.

The engine is pure. It decides *what should happen* to positions and legs given
a transaction, and returns that decision; persistence applies it. That is what
makes ``--dry-run`` show exactly what the real run does rather than an estimate
of it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from portable_core.decimals import money_context
from portable_core.domain.enums import (
    LegRole,
    LotStatus,
    PositionStatus,
    StrategyType,
    TransactionType,
)
from portable_core.domain.models import Lot, Position, PositionLeg
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_INVARIANT_BROKEN, E_POSITION_CLOSED

__all__ = ["FlipSplit", "PositionEngine", "leg_role_for"]

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class FlipSplit:
    """A trade that crosses zero, split into its two ledger effects.

    A sell of 150 against a long of 100 is *not* one trade: it closes 100 and
    opens a short of 50. They have different lots, different holding periods,
    and -- because a short sale is always short-term -- different tax
    treatment. Modelling it as one trade produces a single disposition of 150
    against 100 shares of basis, which is both an unmatched-lot error waiting
    to happen and a wrong gain when it does not error.
    """

    closing_quantity: Decimal
    opening_quantity: Decimal

    @property
    def is_flip(self) -> bool:
        return self.opening_quantity > 0


def leg_role_for(
    txn_type: TransactionType,
    *,
    is_option: bool,
    is_short: bool,
    option_right: str | None = None,
) -> LegRole:
    """The role a new leg takes, from the trade that created it.

    The role is not decoration: it is what lets the engines know that *this*
    short call is written against *that* stock, which is what makes assignment
    a within-position operation.
    """
    if is_option:
        if option_right == "call":
            return LegRole.SHORT_CALL if is_short else LegRole.LONG_CALL
        if option_right == "put":
            return LegRole.SHORT_PUT if is_short else LegRole.LONG_PUT
        return LegRole.OTHER
    if txn_type in {TransactionType.SELL_SHORT, TransactionType.BUY_TO_COVER}:
        return LegRole.SHORT_STOCK
    return LegRole.LONG_STOCK


class PositionEngine:
    """Position and leg lifecycle."""

    # ── the flip ─────────────────────────────────────────────────────────────

    @staticmethod
    def split_flip(held_quantity: Decimal, trade_quantity: Decimal) -> FlipSplit:
        """Split a trade that crosses zero into its closing and opening halves.

        Args:
            held_quantity: the leg's current quantity, unsigned.
            trade_quantity: the quantity being traded in the opposite
                direction, unsigned.
        """
        if trade_quantity <= 0:
            raise ValidationError(
                f"trade quantity must be positive, got {trade_quantity}",
                code=E_INVARIANT_BROKEN,
            )
        closing = min(held_quantity, trade_quantity)
        return FlipSplit(closing_quantity=closing, opening_quantity=trade_quantity - closing)

    # ── leg quantity, and the invariant ──────────────────────────────────────

    @staticmethod
    def leg_quantity(lots: list[Lot]) -> Decimal:
        """The quantity a leg should show: the sum of its open lots.

        This is the right-hand side of `CLAUDE.md` invariant 5, and the reason
        the invariant is stated **per leg** rather than per position: a
        position spanning two instruments has no scalar quantity, so the
        per-position form would be meaningless for exactly the case the model
        exists for (ADR 0009).
        """
        with money_context():
            return sum((lot.remaining_quantity for lot in lots if lot.is_open), ZERO)

    @staticmethod
    def check_leg_invariant(leg: PositionLeg, lots: list[Lot]) -> None:
        """Assert ``sum(lot.remaining_quantity) == leg.quantity``.

        Called after every mutation and by `pt validate`. A divergence means
        materialized state and the ledger disagree, which is the one thing this
        architecture exists to make impossible.
        """
        total = PositionEngine.leg_quantity(lots)
        if total != leg.quantity:
            raise ValidationError(
                f"leg {leg.leg_id} shows quantity {leg.quantity} but its open lots "
                f"sum to {total}",
                code=E_INVARIANT_BROKEN,
                remedy="Run `pt rebuild` to reconstruct derived state from the ledger.",
                leg_id=leg.leg_id,
                leg_quantity=str(leg.quantity),
                lot_total=str(total),
            )

    # ── lifecycle ────────────────────────────────────────────────────────────

    @staticmethod
    def apply_disposition(lot: Lot, quantity: Decimal, on: date) -> Lot:
        """Reduce a lot by *quantity*, closing it when nothing remains."""
        if quantity > lot.remaining_quantity:
            raise ValidationError(
                f"cannot dispose {quantity} from lot {lot.lot_id}, which holds "
                f"{lot.remaining_quantity}",
                code=E_INVARIANT_BROKEN,
                lot_id=lot.lot_id,
            )
        with money_context():
            remaining = lot.remaining_quantity - quantity
            if remaining == 0:
                basis = ZERO
            else:
                # Relieve basis proportionally so basis-per-unit is unchanged
                # by a partial disposition.
                basis = lot.adjusted_cost_basis * remaining / lot.remaining_quantity

        if remaining == 0:
            status = LotStatus.CLOSED
        elif remaining < lot.original_quantity:
            status = LotStatus.PARTIAL
        else:
            status = LotStatus.OPEN

        return replace(
            lot,
            remaining_quantity=remaining,
            adjusted_cost_basis=basis,
            status=status,
            closed_date=on if remaining == 0 else lot.closed_date,
        )

    @staticmethod
    def close_leg_if_empty(leg: PositionLeg, lots: list[Lot], on: date) -> PositionLeg:
        quantity = PositionEngine.leg_quantity(lots)
        if quantity > 0:
            return replace(leg, quantity=quantity)
        return replace(leg, quantity=ZERO, status=PositionStatus.CLOSED, closed_date=on)

    @staticmethod
    def close_position_if_empty(
        position: Position, legs: list[PositionLeg], on: date
    ) -> Position:
        """Close a position once every leg is closed.

        A multi-leg position stays open while any leg does: a covered call
        whose call has expired is still an open stock position, and closing the
        container would orphan the remaining lots.
        """
        if any(leg.status is PositionStatus.OPEN for leg in legs):
            return position
        return replace(position, status=PositionStatus.CLOSED, closed_date=on)

    # ── grouping ─────────────────────────────────────────────────────────────

    @staticmethod
    def regroup(
        legs: list[PositionLeg],
        target: Position,
        *,
        strategy_type: StrategyType | None = None,
    ) -> tuple[list[PositionLeg], Position]:
        """Move *legs* into *target*, changing the trader's stated intent.

        This is what happens when a long stock holding becomes a covered call
        because a call was written against it. Because lots hang off legs and
        legs carry ``position_id``, regrouping updates one column and touches
        no lot and no basis figure -- **a change of intent does not change tax
        history**, which is correct, and is a consequence of the structure
        rather than a rule anybody has to remember.
        """
        if target.status is PositionStatus.CLOSED:
            raise ValidationError(
                f"cannot move legs into closed position {target.position_id}",
                code=E_POSITION_CLOSED,
                position_id=target.position_id,
            )
        moved = [replace(leg, position_id=target.position_id) for leg in legs]
        updated = (
            replace(target, strategy_type=strategy_type)
            if strategy_type is not None
            else target
        )
        return moved, updated

    @staticmethod
    def infer_strategy(legs: list[PositionLeg]) -> StrategyType:
        """Name the strategy from the roles present.

        A convenience for `pt position group`, and deliberately conservative:
        anything it does not recognise is ``CUSTOM`` rather than a guess. A
        mislabelled strategy would misdescribe the position in every report
        that groups by it.
        """
        roles = {leg.role for leg in legs if leg.status is PositionStatus.OPEN}

        if len(roles) <= 1:
            return StrategyType.SINGLE
        if roles == {LegRole.LONG_STOCK, LegRole.SHORT_CALL}:
            return StrategyType.COVERED_CALL
        if roles == {LegRole.LONG_STOCK, LegRole.SHORT_CALL, LegRole.LONG_PUT}:
            return StrategyType.COLLAR
        if roles in (
            {LegRole.LONG_CALL, LegRole.SHORT_CALL},
            {LegRole.LONG_PUT, LegRole.SHORT_PUT},
        ):
            # Vertical vs. calendar turns on the expiries, which this function
            # does not see. The caller, which has the instruments, refines it.
            return StrategyType.VERTICAL
        if roles == {LegRole.LONG_CALL, LegRole.LONG_PUT}:
            return StrategyType.STRADDLE
        return StrategyType.CUSTOM
