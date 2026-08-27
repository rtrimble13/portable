"""Ledger replay: `pt rebuild`, and the audit that makes derived state credible.

ADR 0010. Positions, legs, lots, dispositions, balances, and snapshots are
materialized for speed but must be **exactly reproducible** by replaying the
ledger from inception. This module is what does the replaying, and the fact
that it can is what makes the materialized state worth trusting.

Three properties, all property-tested in
``tests/property/test_replay_reproduces_state.py``:

* **Deterministic** -- same ledger, reference, and config produce byte-identical
  derived state. This is what makes ``PORT-GIPS-J01``'s report content hash
  meaningful, and it is why ``PORT-GIPS-J06`` records determinism as evidence
  rather than convenience.
* **Idempotent** -- rebuilding twice equals rebuilding once.
* **Order-stable** -- transactions inserted in a different wall-clock order but
  with the same ``(trade_date, seq)`` produce identical state.

Replay order is ``(trade_date, seq, txn_id)``. Never ``created_at``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from portable_core.decimals import money_context, quantize_money, to_text
from portable_core.domain.enums import (
    LotStatus,
    PositionStatus,
    StrategyType,
    TransactionType,
)
from portable_core.domain.models import Lot, Position, PositionLeg, Transaction
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_CASH_NOT_CONSERVED
from portable_core.persistence.repositories import Repositories
from portable_core.services.lots import LotEngine
from portable_core.services.positions import PositionEngine, leg_role_for
from portable_core.services.tax import TaxEngine

__all__ = ["ReplayEngine", "ReplayResult", "derived_state_digest"]

ZERO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """What a rebuild did."""

    transactions_replayed: int
    positions_created: int
    lots_created: int
    dispositions_created: int
    #: Digest of the resulting derived state. Two rebuilds of the same ledger
    #: must produce the same digest; that is the invariant in one value.
    digest: str
    warnings: tuple[str, ...] = ()


def derived_state_digest(repos: Repositories) -> str:
    """A stable digest of all derived state.

    Every derived table, every row, every column, in a fixed order, hashed.
    Comparing two digests is how the replay test asserts "byte-for-byte"
    without dumping the whole database into an assertion message.

    The rows come from :meth:`Repositories.derived_rows`, which owns the
    ordering and the exclusion of surrogate keys -- a rebuild legitimately
    re-assigns rowids, so it is the *content* that must match. This function
    does the hashing and no SQL, because ADR 0002 keeps queries in
    ``persistence/``.
    """
    hasher = hashlib.sha256()
    for table in sorted(Repositories.DERIVED_DIGEST_TABLES):
        columns, rows = repos.derived_rows(table)
        if not columns:
            continue
        hasher.update(table.encode())
        for row in rows:
            hasher.update(json.dumps(list(row), separators=(",", ":")).encode())
    return hasher.hexdigest()


class ReplayEngine:
    """Rebuilds derived state from the ledger."""

    def __init__(self, repos: Repositories) -> None:
        self.repos = repos
        self.lots = LotEngine()
        self.positions = PositionEngine()
        self.tax = TaxEngine()

    def rebuild(self, *, until: date | None = None) -> ReplayResult:
        """Drop every derived row and replay the ledger.

        Safe to run at any time, and the standard response to any suspected
        derived-state bug: after a fix, a rebuild recovers correct state from a
        ledger that was never wrong.
        """
        self.repos.clear_derived()

        transactions = self.repos.transactions.in_ledger_order(until=until)
        counters = {"positions": 0, "lots": 0, "dispositions": 0}
        warnings: list[str] = []
        cash: dict[tuple[int, str], Decimal] = {}

        for txn in transactions:
            try:
                self._apply(txn, counters, cash)
            except ValidationError as exc:
                # A replay that cannot apply a transaction reports it and
                # continues, so that `pt rebuild` surfaces every problem in one
                # pass rather than one per run. `pt validate` is what turns
                # these into a non-zero exit.
                warnings.append(f"txn {txn.txn_id} ({txn.txn_type}): {exc.message}")

        for (account_id, currency), balance in sorted(cash.items()):
            self.repos.valuations.set_cash(account_id, balance, currency=currency)

        return ReplayResult(
            transactions_replayed=len(transactions),
            positions_created=counters["positions"],
            lots_created=counters["lots"],
            dispositions_created=counters["dispositions"],
            digest=derived_state_digest(self.repos),
            warnings=tuple(warnings),
        )

    # ── per-transaction application ──────────────────────────────────────────

    def _apply(
        self,
        txn: Transaction,
        counters: dict[str, int],
        cash: dict[tuple[int, str], Decimal],
    ) -> None:
        account = self.repos.accounts.get(txn.account_id)
        if account is None:  # pragma: no cover -- foreign key prevents it
            raise ValidationError(
                f"unknown account {txn.account_id}", code="PT-E-ACCOUNT-NOT-FOUND"
            )

        key = (txn.account_id, account.currency)
        with money_context():
            cash[key] = quantize_money(cash.get(key, ZERO) + txn.net_cash_effect)

        # A transfer's other side moves the counter account's cash too. This is
        # the one transaction type that touches two accounts, and it is one row
        # rather than two precisely so that this stays a single fact (ADR 0007).
        if txn.txn_type is TransactionType.TRANSFER and txn.counter_account_id is not None:
            counter = self.repos.accounts.get(txn.counter_account_id)
            if counter is not None:
                counter_key = (txn.counter_account_id, counter.currency)
                with money_context():
                    cash[counter_key] = quantize_money(
                        cash.get(counter_key, ZERO) - txn.net_cash_effect
                    )

        if txn.instrument_id is None or txn.quantity is None:
            return  # a pure cash event: nothing to position

        if txn.txn_type in {
            TransactionType.BUY,
            TransactionType.SELL_SHORT,
            TransactionType.DIVIDEND_REINVEST,
        }:
            self._open(txn, counters)
        elif txn.txn_type in {TransactionType.SELL, TransactionType.BUY_TO_COVER}:
            self._close(txn, counters)

    def _open(self, txn: Transaction, counters: dict[str, int]) -> None:
        """Create or extend a position, and open a lot."""
        assert txn.instrument_id is not None and txn.quantity is not None
        instrument = self.repos.instruments.get(txn.instrument_id)
        if instrument is None:  # pragma: no cover
            return

        is_short = txn.txn_type is TransactionType.SELL_SHORT
        leg = self.repos.positions.leg_for(txn.account_id, txn.instrument_id)

        if leg is None:
            position_id = self.repos.positions.add(
                Position(
                    position_id=0,
                    account_id=txn.account_id,
                    strategy_type=StrategyType.SINGLE,
                    opened_date=txn.trade_date,
                    status=PositionStatus.OPEN,
                    opened_txn_id=txn.txn_id,
                    note=txn.note,
                )
            )
            counters["positions"] += 1
            role = leg_role_for(
                txn.txn_type,
                is_option=instrument.is_option,
                is_short=is_short,
                option_right=(
                    str(instrument.option.option_right) if instrument.option else None
                ),
            )
            leg_id = self.repos.positions.add_leg(
                PositionLeg(
                    leg_id=0,
                    position_id=position_id,
                    instrument_id=txn.instrument_id,
                    role=role,
                    sign=-1 if is_short else 1,
                    quantity=ZERO,
                    opened_date=txn.trade_date,
                )
            )
        else:
            position_id, leg_id = leg.position_id, leg.leg_id

        with money_context():
            gross = (
                txn.gross_amount
                if txn.gross_amount is not None
                else ((txn.price or ZERO) * txn.quantity * instrument.contract_size)
            )
            # Commissions and fees are part of basis on an opening trade: they
            # are a cost of acquiring, not a separate deduction.
            basis = quantize_money(abs(gross) + txn.total_costs)
            per_unit = basis / txn.quantity if txn.quantity else ZERO

        self.repos.lots.add(
            Lot(
                lot_id=0,
                leg_id=leg_id,
                position_id=position_id,
                instrument_id=txn.instrument_id,
                account_id=txn.account_id,
                open_date=txn.trade_date,
                open_txn_id=txn.txn_id,
                original_quantity=txn.quantity,
                remaining_quantity=txn.quantity,
                per_unit_price=per_unit,
                original_cost_basis=basis,
                adjusted_cost_basis=basis,
                holding_period_start=txn.trade_date,
                allocated_fees=txn.total_costs,
                is_short=is_short,
                status=LotStatus.OPEN,
            )
        )
        counters["lots"] += 1
        self._refresh_leg(leg_id, txn.trade_date)

    def _close(self, txn: Transaction, counters: dict[str, int]) -> None:
        """Consume lots under the relief method and record the dispositions."""
        assert txn.instrument_id is not None and txn.quantity is not None
        account = self.repos.accounts.get(txn.account_id)
        instrument = self.repos.instruments.get(txn.instrument_id)
        if account is None or instrument is None:  # pragma: no cover
            return

        method = txn.relief_method or account.default_relief_method
        open_lots = self.repos.lots.open_lots(txn.account_id, txn.instrument_id)

        selection = None
        if txn.lot_selection:
            from portable_core.services.lots import parse_lot_selection

            selection = parse_lot_selection(txn.lot_selection)

        plan = self.lots.select(
            open_lots, txn.quantity, method, txn.trade_date, selection=selection
        )

        with money_context():
            gross = (
                txn.gross_amount
                if txn.gross_amount is not None
                else ((txn.price or ZERO) * txn.quantity * instrument.contract_size)
            )
            proceeds = abs(quantize_money(gross))

        dispositions = self.lots.realize(
            plan,
            txn_id=txn.txn_id,
            account_id=txn.account_id,
            instrument_id=txn.instrument_id,
            disposition_date=txn.trade_date,
            gross_proceeds=proceeds,
            fees=txn.total_costs,
        )

        schedules = self.repos.accounts.rate_schedules(txn.account_id)
        leg_ids: set[int] = set()

        for consumption, disposition in zip(plan.consumptions, dispositions, strict=True):
            disposition_id = self.repos.lots.add_disposition(disposition)
            counters["dispositions"] += 1

            updated = self.positions.apply_disposition(
                consumption.lot, consumption.quantity, txn.trade_date
            )
            self.repos.lots.update_after_disposition(updated)
            leg_ids.add(consumption.lot.leg_id)

            # The realized_gain row has a foreign key onto the STORED
            # disposition id, not the provisional one the engine assigned
            # before the insert. `replace` is the right tool: these are
            # slots dataclasses, so there is no __dict__ to splat.
            stored = replace(disposition, disposition_id=disposition_id)
            self.repos.lots.add_realized_gain(self.tax.estimate(stored, account, schedules))

        for leg_id in sorted(leg_ids):
            self._refresh_leg(leg_id, txn.trade_date)

    def _refresh_leg(self, leg_id: int, on: date) -> None:
        """Re-materialize a leg's quantity from its lots, and close it if empty.

        This is where `CLAUDE.md` invariant 5 is *maintained*; the property test
        is where it is *checked*.
        """
        lots = self.repos.lots.by_leg(leg_id)
        quantity = self.positions.leg_quantity(lots)
        if quantity > 0:
            self.repos.positions.update_leg_quantity(leg_id, quantity)
            return

        self.repos.positions.close_leg(leg_id, on)
        position_id = self.repos.positions.position_id_for_leg(leg_id)
        if position_id is None:  # pragma: no cover
            return
        legs = self.repos.positions.legs(position_id)
        if all(leg.status is PositionStatus.CLOSED for leg in legs):
            self.repos.positions.close_position(position_id, on)

    # ── invariants ───────────────────────────────────────────────────────────

    def check_cash_conservation(self) -> list[str]:
        """CLAUDE.md invariant 4, checked against the ledger.

        For every account, the sum of ``net_cash_effect`` over the ledger must
        equal the materialized balance. A divergence means a transaction's cash
        effect was computed one way when written and another when replayed --
        which is a wrong number that nothing else would surface.
        """
        problems: list[str] = []
        for account in self.repos.accounts.all():
            ledger_total = ZERO
            with money_context():
                for txn in self.repos.transactions.in_ledger_order(
                    account_id=account.account_id
                ):
                    ledger_total += txn.net_cash_effect
                for txn in self.repos.transactions.in_ledger_order():
                    if (
                        txn.txn_type is TransactionType.TRANSFER
                        and txn.counter_account_id == account.account_id
                    ):
                        ledger_total -= txn.net_cash_effect
                ledger_total = quantize_money(ledger_total)

            balance, _margin = self.repos.valuations.cash(
                account.account_id, currency=account.currency
            )
            if balance != ledger_total:
                problems.append(
                    f"account {account.name!r}: materialized cash {to_text(balance)} "
                    f"but the ledger sums to {to_text(ledger_total)}"
                )
        return problems

    def check_leg_invariants(self) -> list[str]:
        """``sum(lot.remaining_quantity) == leg.quantity``, for every leg."""
        problems: list[str] = []
        for position in self.repos.positions.all():
            for leg in position.legs:
                lots = self.repos.lots.by_leg(leg.leg_id)
                try:
                    self.positions.check_leg_invariant(leg, lots)
                except ValidationError as exc:
                    problems.append(exc.message)
        return problems

    def raise_if_cash_not_conserved(self) -> None:
        problems = self.check_cash_conservation()
        if problems:
            raise ValidationError(
                "cash is not conserved: " + "; ".join(problems),
                code=E_CASH_NOT_CONSERVED,
                remedy="Run `pt rebuild`, then `pt validate` again.",
                problems=problems,
            )
