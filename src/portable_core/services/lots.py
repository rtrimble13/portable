"""The lot engine: relief methods, consumption, and realized gain.

Lots are the tax engine's atoms. This module decides **which lots a closing
trade consumes**, and that decision determines the basis relieved, the holding
period, and therefore the tax rate. It is not a detail.

The engine is pure: it is handed lots and returns a plan. Nothing here writes
to a database, so every rule below is unit-testable without one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from portable_core.decimals import allocate, money_context, quantize_money
from portable_core.domain.dates import days_between, holding_period
from portable_core.domain.enums import HoldingPeriod, ReliefMethod
from portable_core.domain.models import Lot, LotDisposition
from portable_core.errors import ValidationError
from portable_core.errors.kinds import (
    E_LOT_INSUFFICIENT,
    E_LOT_SELECTION_INVALID,
    E_LOT_UNMATCHED,
    E_POSITION_DIRECTION,
    E_TAX_METHOD_CONFLICT,
)

__all__ = [
    "KEY_PREFIX",
    "LotConsumption",
    "LotEngine",
    "ReliefPlan",
    "check_opening_side",
    "derived_lot_key",
    "designate",
    "format_keyed_selection",
    "is_keyed",
    "keyed_selection",
    "lots_of_side",
    "opening_lot_key",
    "parse_keyed_selection",
    "parse_lot_selection",
    "resolve_keyed_selection",
]

ZERO = Decimal("0")

# ── lineage keys (ADR 0036) ──────────────────────────────────────────────────
#
# A ledger row may not name a derived key (ADR 0023): a rebuild renumbers lot
# ids, so a designation by id names whichever lot the next replay happens to
# number that way. A lot is named by its lineage instead, which no later row
# can change.

#: Marks a designation that names lineage keys: ``@1234:10;@1500.1234:3``. A
#: designation without it predates schema 10 and names lot ids; its meaning is
#: recorded in ``lot_designation``.
KEY_PREFIX = "@"


def opening_lot_key(txn_id: int) -> str:
    """The key of the lot an opening transaction created."""
    return str(txn_id)


def derived_lot_key(txn_id: int, parent_key: str) -> str:
    """The key of a lot the action in *txn_id* derived from the lot *parent_key*.

    A spinoff gives each parent lot one spun lot, and a conversion one lot of
    the new instrument, all opened by the same transaction. The parent's key
    is what tells them apart, and it is stable for the same reason this is.
    """
    return f"{txn_id}.{parent_key}"


def is_keyed(spec: str) -> bool:
    """Whether *spec* names lineage keys rather than lot ids."""
    return spec.strip().startswith(KEY_PREFIX)


def parse_keyed_selection(spec: str) -> dict[str, Decimal]:
    """Parse ``"@1234:10;@1500.1234:3"`` into ``{key: quantity}``.

    Raises:
        ValidationError: on anything malformed, including a designation that
            mixes keys and lot ids.
    """
    selection: dict[str, Decimal] = {}
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        if not part.startswith(KEY_PREFIX) or ":" not in part:
            raise ValidationError(
                f"malformed lot designation {part!r}: expected '@key:quantity'",
                code=E_LOT_SELECTION_INVALID,
                spec=spec,
            )
        key_text, qty_text = part[len(KEY_PREFIX) :].rsplit(":", 1)
        key = key_text.strip()
        try:
            quantity = Decimal(qty_text.strip())
        except ArithmeticError as exc:
            raise ValidationError(
                f"malformed lot designation {part!r}: {exc}",
                code=E_LOT_SELECTION_INVALID,
                spec=spec,
            ) from exc
        if not key or quantity <= 0:
            raise ValidationError(
                f"malformed lot designation {part!r}",
                code=E_LOT_SELECTION_INVALID,
                spec=spec,
            )
        selection[key] = selection.get(key, ZERO) + quantity
    if not selection:
        raise ValidationError("empty lot designation", code=E_LOT_SELECTION_INVALID, spec=spec)
    return selection


def format_keyed_selection(selection: dict[str, Decimal]) -> str:
    """The stored form of a keyed designation, in the order given."""
    return ";".join(f"{KEY_PREFIX}{key}:{quantity}" for key, quantity in selection.items())


def keyed_selection(selection: dict[int, Decimal], lots: list[Lot]) -> str:
    """Name the lots in *selection* (by id, as a user sees them) by their keys.

    Done when a trade is planned, against the lots the user is looking at, so
    the ledger never stores an id.

    Raises:
        ValidationError: for an id that is not one of *lots*.
    """
    by_id = {lot.lot_id: lot for lot in lots}
    keyed: dict[str, Decimal] = {}
    for lot_id, quantity in selection.items():
        lot = by_id.get(lot_id)
        if lot is None:
            raise ValidationError(
                f"lot {lot_id} is not an open lot of this instrument in this account",
                code=E_LOT_SELECTION_INVALID,
                remedy="`pt lot list SYMBOL` shows the open lots and their ids.",
                lot_id=lot_id,
            )
        keyed[lot.lot_key] = keyed.get(lot.lot_key, ZERO) + quantity
    return format_keyed_selection(keyed)


def designate(spec: str, lots: list[Lot]) -> tuple[dict[int, Decimal], str]:
    """A designation as a writer received it, ready to plan and to store.

    *spec* is what the user typed (lot ids, as ``pt lot list`` shows them) or
    a keyed designation an importer built. Returns the lot ids this plan
    relieves and the keyed form the ledger stores, so no row ever records an
    id a rebuild could renumber (ADR 0036).
    """
    if is_keyed(spec):
        by_id = resolve_keyed_selection(spec, lots)
        return by_id, format_keyed_selection(parse_keyed_selection(spec))
    by_id = parse_lot_selection(spec)
    return by_id, keyed_selection(by_id, lots)


def resolve_keyed_selection(spec: str, lots: list[Lot]) -> dict[int, Decimal]:
    """The lot ids, in this replay, of the lots a keyed designation names.

    Raises:
        ValidationError: for a key that names no lot among *lots*: the lot was
            never opened, is already closed, or is in another account or
            instrument. Relieving something else instead is the substitution
            specific identification exists to prevent.
    """
    by_key = {lot.lot_key: lot for lot in lots}
    selection: dict[int, Decimal] = {}
    for key, quantity in parse_keyed_selection(spec).items():
        lot = by_key.get(key)
        if lot is None:
            raise ValidationError(
                f"designated lot @{key} is not open in this account and instrument",
                code=E_LOT_SELECTION_INVALID,
                remedy=(
                    "A designation names the lot by the transaction that opened it. "
                    "Check that lot with `pt lot list SYMBOL --as-of DATE`."
                ),
                lot_key=key,
            )
        selection[lot.lot_id] = selection.get(lot.lot_id, ZERO) + quantity
    return selection


# ── direction (ADR 0038) ─────────────────────────────────────────────────────


def lots_of_side(lots: list[Lot], *, short: bool) -> list[Lot]:
    """The lots a closing trade of one direction may relieve.

    A sale or a transfer out relieves long lots, and a cover short ones. Handed
    every open lot, relief would let `pt cover` consume a long lot as if it
    were covering it, and report a gain measured against the wrong thing.
    """
    return [lot for lot in lots if lot.is_short is short]


def check_opening_side(*, short: bool, open_lots: list[Lot], symbol: str) -> None:
    """Refuse opening one direction while the other is open. ADR 0038.

    A short sale against shares held long is a constructive sale (IRC §1259):
    the long position's gain is recognised as if it were sold. `portable` does
    not record one, so it refuses rather than open a short lot beside the long
    ones and report neither correctly. A purchase while short is a cover.
    """
    if short and any(not lot.is_short for lot in open_lots):
        raise ValidationError(
            f"{symbol} is held long in this account, so a short sale against it is "
            "a constructive sale (IRC §1259), which portable does not record",
            code=E_POSITION_DIRECTION,
            remedy="Sell the long position with `pt sell` before shorting.",
            symbol=symbol,
        )
    if not short and any(lot.is_short for lot in open_lots):
        raise ValidationError(
            f"{symbol} is held short in this account, so acquiring it covers the short",
            code=E_POSITION_DIRECTION,
            remedy="Close the short position with `pt cover`.",
            symbol=symbol,
        )


@dataclass(frozen=True, slots=True)
class LotConsumption:
    """One lot, and how much of it a closing trade takes."""

    lot: Lot
    quantity: Decimal
    cost_basis_relieved: Decimal
    holding_period: HoldingPeriod
    days_held: int


@dataclass(frozen=True, slots=True)
class ReliefPlan:
    """What a closing trade will consume, before anything is written.

    ``--dry-run`` renders this and stops. The real run renders the same object
    and then persists it, which is why a dry run shows exactly what will
    happen rather than an estimate of it.
    """

    method: ReliefMethod
    consumptions: tuple[LotConsumption, ...]

    @property
    def total_quantity(self) -> Decimal:
        return sum((c.quantity for c in self.consumptions), ZERO)

    @property
    def total_basis(self) -> Decimal:
        return sum((c.cost_basis_relieved for c in self.consumptions), ZERO)


def parse_lot_selection(spec: str) -> dict[int, Decimal]:
    """Parse a spec-ID designation: ``"12:100;15:50"``.

    Raises:
        ValidationError: on anything malformed. A mistyped lot designation must
            not silently fall back to FIFO -- that would change the tax
            treatment of the trade without saying so.
    """
    selection: dict[int, Decimal] = {}
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValidationError(
                f"malformed lot selection {part!r}: expected 'lot_id:quantity'",
                code=E_LOT_SELECTION_INVALID,
                remedy="Use --lots 12:100;15:50, or choose a method with --method.",
                spec=spec,
            )
        lot_text, qty_text = part.split(":", 1)
        try:
            lot_id = int(lot_text.strip())
            quantity = Decimal(qty_text.strip())
        except (ValueError, ArithmeticError) as exc:
            raise ValidationError(
                f"malformed lot selection {part!r}: {exc}",
                code=E_LOT_SELECTION_INVALID,
                spec=spec,
            ) from exc
        if quantity <= 0:
            raise ValidationError(
                f"lot selection quantity must be positive: {part!r}",
                code=E_LOT_SELECTION_INVALID,
                spec=spec,
            )
        selection[lot_id] = selection.get(lot_id, ZERO) + quantity
    if not selection:
        raise ValidationError(
            "empty lot selection",
            code=E_LOT_SELECTION_INVALID,
            spec=spec,
        )
    return selection


class LotEngine:
    """Selects and consumes lots under a relief method.

    Default is **specific identification**. The others exist because a broker
    default or a past election may require them, and because comparing them is
    exactly what `pt lot select` is for.
    """

    def select(
        self,
        lots: list[Lot],
        quantity: Decimal,
        method: ReliefMethod,
        disposition_date: date,
        *,
        selection: dict[int, Decimal] | None = None,
    ) -> ReliefPlan:
        """Plan which lots a disposition of *quantity* consumes.

        Args:
            lots: candidate open lots for one instrument in one account.
            quantity: the positive quantity being closed.
            method: the relief method in force for this trade.
            disposition_date: drives the holding-period determination.
            selection: explicit lot designation, required for
                :attr:`ReliefMethod.SPEC`.

        Raises:
            ValidationError: when there are no lots (``PT-E-LOT-UNMATCHED``),
                not enough quantity (``PT-E-LOT-INSUFFICIENT``), or a spec-ID
                designation that does not add up
                (``PT-E-LOT-SELECTION-INVALID``). It never guesses: an
                unmatched closing trade stops the command.
        """
        if quantity <= 0:
            raise ValidationError(
                f"disposition quantity must be positive, got {quantity}",
                code=E_LOT_SELECTION_INVALID,
                quantity=str(quantity),
            )

        open_lots = [lot for lot in lots if lot.is_open]
        if not open_lots:
            raise ValidationError(
                "no open lots to match this closing trade",
                code=E_LOT_UNMATCHED,
                remedy=(
                    "Record the opening trade first, or use --force-zero-basis to "
                    "declare explicitly that the basis is unknown. portable will not "
                    "assume a basis on your behalf."
                ),
                quantity=str(quantity),
                disposition_date=disposition_date.isoformat(),
            )

        available = sum((lot.remaining_quantity for lot in open_lots), ZERO)
        if available < quantity:
            raise ValidationError(
                f"closing trade of {quantity} exceeds the {available} available "
                f"across {len(open_lots)} open lot(s)",
                code=E_LOT_INSUFFICIENT,
                remedy=(
                    "Check the quantity, or record the missing opening trade. "
                    "A short position is opened with `pt short`, not by overselling."
                ),
                requested=str(quantity),
                available=str(available),
            )

        if method is ReliefMethod.SPEC:
            consumptions = self._select_specific(
                open_lots, quantity, disposition_date, selection
            )
        elif method is ReliefMethod.AVERAGE:
            consumptions = self._select_average(open_lots, quantity, disposition_date)
        else:
            consumptions = self._select_ordered(open_lots, quantity, method, disposition_date)

        return ReliefPlan(method=method, consumptions=tuple(consumptions))

    # ── method implementations ───────────────────────────────────────────────

    def _select_specific(
        self,
        lots: list[Lot],
        quantity: Decimal,
        disposition_date: date,
        selection: dict[int, Decimal] | None,
    ) -> list[LotConsumption]:
        """Specific identification. The designation must be exact.

        No partial credit and no fallback: a designation that does not add up
        to the trade quantity is an error, because silently making up the
        difference from another lot changes the tax treatment of the trade
        without telling anyone.
        """
        if not selection:
            raise ValidationError(
                "specific identification requires an explicit lot designation",
                code=E_LOT_SELECTION_INVALID,
                remedy=(
                    "Pass --lots 'lot_id:qty;lot_id:qty', or choose another method "
                    "with --method {fifo,lifo,hifo,lofo,avg}."
                ),
                available_lots=[lot.lot_id for lot in lots],
            )

        by_id = {lot.lot_id: lot for lot in lots}
        designated = sum(selection.values(), ZERO)
        if designated != quantity:
            raise ValidationError(
                f"lot designation totals {designated} but the trade is for {quantity}",
                code=E_LOT_SELECTION_INVALID,
                remedy="Designate exactly the traded quantity.",
                designated=str(designated),
                requested=str(quantity),
            )

        consumptions: list[LotConsumption] = []
        # Sorted so the plan, and therefore the written dispositions, are
        # deterministic regardless of the order the user typed the designation.
        for lot_id in sorted(selection):
            take = selection[lot_id]
            lot = by_id.get(lot_id)
            if lot is None:
                raise ValidationError(
                    f"lot {lot_id} is not an open lot for this instrument and account",
                    code=E_LOT_SELECTION_INVALID,
                    lot_id=lot_id,
                    available_lots=sorted(by_id),
                )
            if take > lot.remaining_quantity:
                raise ValidationError(
                    f"lot {lot_id} has {lot.remaining_quantity} remaining, "
                    f"but {take} was designated",
                    code=E_LOT_INSUFFICIENT,
                    lot_id=lot_id,
                    remaining=str(lot.remaining_quantity),
                    designated=str(take),
                )
            consumptions.append(self._consume(lot, take, disposition_date))
        return consumptions

    def _select_ordered(
        self,
        lots: list[Lot],
        quantity: Decimal,
        method: ReliefMethod,
        disposition_date: date,
    ) -> list[LotConsumption]:
        """FIFO, LIFO, HIFO, LOFO -- an ordering, then consume until satisfied.

        Every sort key ends in ``lot_id``. That is not decoration: two lots
        acquired the same day at the same price would otherwise order
        arbitrarily, and `CLAUDE.md` invariant 6 requires the same inputs to
        produce identical output. Money is compared as ``Decimal`` in Python
        rather than in SQL, because the canonical text form preserves trailing
        zeros and would not sort numerically.
        """
        ordered = self._order(lots, method)

        consumptions: list[LotConsumption] = []
        outstanding = quantity
        for lot in ordered:
            if outstanding <= 0:
                break
            take = min(outstanding, lot.remaining_quantity)
            consumptions.append(self._consume(lot, take, disposition_date))
            outstanding -= take

        assert outstanding == 0, "availability was checked before selection"
        return consumptions

    @staticmethod
    def _order(lots: list[Lot], method: ReliefMethod) -> list[Lot]:
        match method:
            case ReliefMethod.FIFO:
                return sorted(lots, key=lambda lot: (lot.open_date, lot.lot_id))
            case ReliefMethod.LIFO:
                return sorted(lots, key=lambda lot: (lot.open_date, lot.lot_id), reverse=True)
            case ReliefMethod.HIFO:
                # Highest basis first: realises the smallest gain, which is
                # usually why somebody chooses it.
                return sorted(lots, key=lambda lot: (-lot.basis_per_unit, lot.lot_id))
            case ReliefMethod.LOFO:
                return sorted(lots, key=lambda lot: (lot.basis_per_unit, lot.lot_id))
            case _:  # pragma: no cover -- guarded by the caller
                raise ValidationError(
                    f"not an ordered relief method: {method}",
                    code=E_LOT_SELECTION_INVALID,
                )

    def _select_average(
        self,
        lots: list[Lot],
        quantity: Decimal,
        disposition_date: date,
    ) -> list[LotConsumption]:
        """Average cost.

        Two things are true at once here, and getting either wrong is a
        familiar error:

        1. **Basis is averaged** across every open lot of the instrument, so
           each disposed share carries the same cost.
        2. **The holding period is still determined lot by lot, FIFO.** Average
           cost averages the *basis*, not the *dates*. The shares disposed of
           are the oldest ones, so a portfolio holding some long-term and some
           short-term shares produces a split disposition -- and reporting it
           all as long-term because the average lot "looks old" would be a
           wrong tax rate rather than a rounding.

        Average cost may not be mixed with specific identification for the same
        instrument; :meth:`check_method_consistency` enforces that separately,
        because it needs the account's history and this method does not.
        """
        with money_context():
            total_quantity = sum((lot.remaining_quantity for lot in lots), ZERO)
            total_basis = sum((lot.adjusted_cost_basis for lot in lots), ZERO)
            average = total_basis / total_quantity if total_quantity else ZERO

        ordered = sorted(lots, key=lambda lot: (lot.open_date, lot.lot_id))
        consumptions: list[LotConsumption] = []
        outstanding = quantity

        for lot in ordered:
            if outstanding <= 0:
                break
            take = min(outstanding, lot.remaining_quantity)
            with money_context():
                basis = quantize_money(average * take)
            period = holding_period(
                lot.holding_period_start, disposition_date, is_short_sale=lot.is_short
            )
            consumptions.append(
                LotConsumption(
                    lot=lot,
                    quantity=take,
                    cost_basis_relieved=basis,
                    holding_period=period,
                    days_held=days_between(lot.holding_period_start, disposition_date),
                )
            )
            outstanding -= take

        return consumptions

    @staticmethod
    def _consume(lot: Lot, quantity: Decimal, disposition_date: date) -> LotConsumption:
        """Take *quantity* from *lot*, relieving basis proportionally."""
        with money_context():
            if quantity == lot.remaining_quantity:
                # Relieve the whole remaining basis rather than recomputing it
                # from a per-unit figure: a per-unit multiplication would leave
                # a rounding residue on a fully closed lot, which then looks
                # like a lot that is closed but still has basis.
                basis = lot.adjusted_cost_basis
            else:
                basis = quantize_money(
                    lot.adjusted_cost_basis * quantity / lot.remaining_quantity
                )

        period = holding_period(
            lot.holding_period_start, disposition_date, is_short_sale=lot.is_short
        )
        return LotConsumption(
            lot=lot,
            quantity=quantity,
            cost_basis_relieved=basis,
            holding_period=period,
            days_held=days_between(lot.holding_period_start, disposition_date),
        )

    # ── realization ──────────────────────────────────────────────────────────

    def realize(
        self,
        plan: ReliefPlan,
        *,
        txn_id: int,
        account_id: int,
        instrument_id: int,
        disposition_date: date,
        gross_proceeds: Decimal,
        fees: Decimal = Decimal("0.00"),
        next_disposition_id: int = 1,
    ) -> list[LotDisposition]:
        """Turn a plan plus proceeds into dispositions with realized gains.

        Proceeds and fees are allocated across the consumed lots **by
        quantity, using largest-remainder** so the parts sum exactly to the
        whole. Rounding each lot's share independently would lose or invent a
        cent, and that cent becomes a basis error, then a realized-gain error,
        then a wrong tax figure.

        Fees **reduce proceeds**: they are a cost of the sale, not a separate
        deduction, which is both the tax treatment and what makes
        ``proceeds - basis`` the realized gain without a third term.
        """
        quantities = [c.quantity for c in plan.consumptions]
        proceeds_split = allocate(gross_proceeds, quantities)
        fees_split = allocate(fees, quantities)

        dispositions: list[LotDisposition] = []
        for index, consumption in enumerate(plan.consumptions):
            with money_context():
                net_proceeds = proceeds_split[index] - fees_split[index]
                gain = quantize_money(net_proceeds - consumption.cost_basis_relieved)
            dispositions.append(
                LotDisposition(
                    disposition_id=next_disposition_id + index,
                    lot_id=consumption.lot.lot_id,
                    txn_id=txn_id,
                    account_id=account_id,
                    instrument_id=instrument_id,
                    disposition_date=disposition_date,
                    quantity=consumption.quantity,
                    proceeds=net_proceeds,
                    cost_basis_relieved=consumption.cost_basis_relieved,
                    realized_gain=gain,
                    holding_period=consumption.holding_period,
                    days_held=consumption.days_held,
                    relief_method=plan.method,
                    allocated_fees=fees_split[index],
                )
            )
        return dispositions

    def realize_cover(
        self,
        plan: ReliefPlan,
        *,
        txn_id: int,
        account_id: int,
        instrument_id: int,
        disposition_date: date,
        gross_cost: Decimal,
        fees: Decimal = Decimal("0.00"),
        next_disposition_id: int = 1,
    ) -> list[LotDisposition]:
        """Close short lots: dispositions shaped as Form 8949 shapes a short sale.

        ADR 0038. A short lot's basis column holds what the short sale
        received, so here it is the **proceeds**, relieved by quantity. What
        the cover paid, gross plus its costs, is the **cost basis**. The gain
        is proceeds less basis, as for any disposition, and the holding period
        is short-term whatever the dates (the consumption already says so).

        A written option that lapses is a cover at zero cost, so its premium
        is all gain.
        """
        quantities = [c.quantity for c in plan.consumptions]
        cost_split = allocate(gross_cost, quantities)
        fees_split = allocate(fees, quantities)

        dispositions: list[LotDisposition] = []
        for index, consumption in enumerate(plan.consumptions):
            with money_context():
                proceeds = consumption.cost_basis_relieved
                basis = quantize_money(cost_split[index] + fees_split[index])
                gain = quantize_money(proceeds - basis)
            dispositions.append(
                LotDisposition(
                    disposition_id=next_disposition_id + index,
                    lot_id=consumption.lot.lot_id,
                    txn_id=txn_id,
                    account_id=account_id,
                    instrument_id=instrument_id,
                    disposition_date=disposition_date,
                    quantity=consumption.quantity,
                    proceeds=proceeds,
                    cost_basis_relieved=basis,
                    realized_gain=gain,
                    holding_period=consumption.holding_period,
                    days_held=consumption.days_held,
                    relief_method=plan.method,
                    allocated_fees=fees_split[index],
                )
            )
        return dispositions

    # ── method consistency ───────────────────────────────────────────────────

    @staticmethod
    def check_method_consistency(
        instrument_id: int,
        method: ReliefMethod,
        prior_methods: set[ReliefMethod],
    ) -> None:
        """Refuse to mix average cost with specific identification.

        The IRS requires an average-basis election to apply to all shares of
        the instrument. Once averaged, a share's individual basis is gone --
        so a later spec-ID designation against the same instrument is
        designating something that no longer exists, and the reverse leaves
        already-designated shares in an average that no longer describes them.

        `portable` refuses rather than picking one, because either choice
        silently changes the basis of shares the user already disposed of.
        """
        spec_like = {
            ReliefMethod.SPEC,
            ReliefMethod.FIFO,
            ReliefMethod.LIFO,
            ReliefMethod.HIFO,
            ReliefMethod.LOFO,
        }
        used_average = ReliefMethod.AVERAGE in prior_methods
        used_spec = bool(prior_methods & spec_like)

        conflict = (method is ReliefMethod.AVERAGE and used_spec) or (
            method in spec_like and used_average
        )
        if conflict:
            raise ValidationError(
                "average cost cannot be mixed with specific identification for the "
                "same instrument",
                code=E_TAX_METHOD_CONFLICT,
                remedy=(
                    "Use the method already applied to this instrument, or open a "
                    "separate account for the averaged holding. An average-basis "
                    "election applies to all shares of the instrument."
                ),
                instrument_id=instrument_id,
                requested_method=str(method),
                prior_methods=sorted(str(m) for m in prior_methods),
            )
