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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Generic, TypeVar

from portable_core.decimals import money_context, quantize_money, quantize_quantity, to_text
from portable_core.domain.enums import (
    BasisSource,
    LegRole,
    LotStatus,
    PositionStatus,
    StrategyType,
    TransactionType,
)
from portable_core.domain.models import (
    CorporateAction,
    Lot,
    Position,
    PositionLeg,
    Transaction,
)
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_CASH_NOT_CONSERVED, E_REPLAY_MISMATCH
from portable_core.persistence.connection import scratch_savepoint
from portable_core.persistence.repositories import Repositories
from portable_core.services.corporate_actions import CorporateActionEngine
from portable_core.services.lots import LotEngine
from portable_core.services.positions import PositionEngine, leg_role_for
from portable_core.services.tax import TaxEngine

__all__ = [
    "ReplayEngine",
    "ReplayResult",
    "Walk",
    "derived_state_digest",
    "derived_state_digests",
    "effective_ledger",
    "over_dates",
    "state_on",
]

T = TypeVar("T")

ZERO = Decimal("0.00")

#: Corporate-action transaction types replay must reapply, mapped to the
#: `corporate_action.action_type` each corresponds to.
_ACTION_TYPE_FOR: dict[TransactionType, str] = {
    TransactionType.SPLIT: "split",
    TransactionType.REVERSE_SPLIT: "reverse_split",
    TransactionType.SPINOFF: "spinoff",
    TransactionType.MERGER_STOCK: "merger",
}
_CORPORATE_ACTIONS = frozenset(_ACTION_TYPE_FOR)


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
    #: Ledger rows the replay could not apply, one message each.
    #:
    #: Two audiences, deliberately: `pt rebuild` renders these as warnings,
    #: because the rebuild did happen and reporting every problem in one pass is
    #: more useful than stopping at the first. `pt validate` renders the same
    #: facts as **problems** and exits non-zero, because a ledger row that
    #: produces no derived state means derived state is not a function of the
    #: whole ledger.
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Walk(Generic[T]):
    """What :meth:`ReplayEngine.walk` saw: one result per date, in date order."""

    results: tuple[T, ...]
    #: Ledger rows the replay could not apply -- the same facts `pt rebuild`
    #: reports as warnings, because the walk replays the same rows.
    warnings: tuple[str, ...] = ()


def effective_ledger(transactions: Iterable[Transaction]) -> list[Transaction]:
    """The ledger with every reversal, and the entry it reverses, netted out.

    **This is the one place that netting is defined** (ADR 0021). Replay
    derives state from these rows, the cash-conservation check sums them, the
    historical walk truncates them, and a valuation snapshot takes its flows
    from them -- so a snapshot's market value and its flows cannot disagree
    about whether a reversed entry happened.

    Pass the **whole** ledger and truncate afterwards. A reversal dated after D
    still nets its original out of the state as of D: a reversal corrects the
    record, it is not an economic event on its own date. Both rows remain in
    the ledger, which is the point -- history shows the mistake, the reversal
    and the correction (CLAUDE.md invariant 2), while derived state reflects
    only the net effect.

    Netting the pair out is stronger than trying to unwind the original's
    effect after the fact. Unwinding would have to decide what to do with lots
    that a later trade had already consumed; netting surfaces that as an
    unmatched closing trade, which is the honest answer -- reversing a purchase
    whose shares were subsequently sold is a conflict a person has to resolve,
    not something replay should paper over.
    """
    rows = list(transactions)
    reversed_ids = {
        txn.reverses_txn_id
        for txn in rows
        if txn.txn_type is TransactionType.REVERSAL and txn.reverses_txn_id
    }
    return [
        txn
        for txn in rows
        if txn.txn_id not in reversed_ids and txn.txn_type is not TransactionType.REVERSAL
    ]


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
    for table, digest in sorted(derived_state_digests(repos).items()):
        hasher.update(table.encode())
        hasher.update(digest.encode())
    return hasher.hexdigest()


def derived_state_digests(repos: Repositories) -> dict[str, str]:
    """The same digest, per derived table.

    `pt validate` compares stored derived state against a replay of the ledger
    and has to say *what* differs when they disagree. One digest over everything
    answers "something", which is the least useful thing a diagnostic can say
    about a book somebody files taxes from (ADR 0016).
    """
    digests: dict[str, str] = {}
    for table in sorted(Repositories.DERIVED_DIGEST_TABLES):
        columns, rows = repos.derived_rows(table)
        if not columns:
            continue
        hasher = hashlib.sha256()
        hasher.update(table.encode())
        for row in rows:
            hasher.update(json.dumps(list(row), separators=(",", ":")).encode())
        digests[table] = hasher.hexdigest()
    return digests


class ReplayEngine:
    """Rebuilds derived state from the ledger."""

    def __init__(self, repos: Repositories) -> None:
        self.repos = repos
        self.lots = LotEngine()
        self.corporate_actions = CorporateActionEngine()
        self.positions = PositionEngine()
        self.tax = TaxEngine()

    def rebuild(self) -> ReplayResult:
        """Drop every derived row and replay the ledger.

        Safe to run at any time, and the standard response to any suspected
        derived-state bug: after a fix, a rebuild recovers correct state from a
        ledger that was never wrong.

        There is deliberately no ``until``. State as of an earlier date is
        :meth:`walk`'s question, and a rebuild truncated at D answers a
        different one -- the ledger *as booked* through D, with a reversal
        dated after D not yet netting its original out (ADR 0021).
        """
        self.repos.clear_derived()

        transactions = self.repos.transactions.in_ledger_order()
        counters = {"positions": 0, "lots": 0, "dispositions": 0}
        warnings: list[str] = []
        cash: dict[tuple[int, str], Decimal] = {}

        # A reversal and the entry it reverses net to zero in DERIVED state;
        # effective_ledger() says why, once.
        for txn in effective_ledger(transactions):
            self._apply_reporting(txn, counters, cash, warnings)
        self._write_cash(cash)

        return ReplayResult(
            transactions_replayed=len(transactions),
            positions_created=counters["positions"],
            lots_created=counters["lots"],
            dispositions_created=counters["dispositions"],
            digest=derived_state_digest(self.repos),
            warnings=tuple(warnings),
        )

    def _apply_reporting(
        self,
        txn: Transaction,
        counters: dict[str, int],
        cash: dict[tuple[int, str], Decimal],
        warnings: list[str],
    ) -> None:
        """Apply one row during a full replay, reporting rather than raising.

        A replay that cannot apply a transaction reports it and continues, so
        that `pt rebuild` surfaces every problem in one pass rather than one per
        run. `pt validate` is what turns these into a non-zero exit.
        """
        try:
            self.apply_transaction(txn, counters=counters, cash=cash)
        except ValidationError as exc:
            warnings.append(f"txn {txn.txn_id} ({txn.txn_type}): {exc.message}")

    def _write_cash(self, cash: dict[tuple[int, str], Decimal]) -> None:
        for (account_id, currency), balance in sorted(cash.items()):
            self.repos.valuations.set_cash(account_id, balance, currency=currency)

    # ── state as of a past date ──────────────────────────────────────────────

    def walk(self, dates: Iterable[date], visit: Callable[[date], T]) -> Walk[T]:
        """Call *visit* with derived state as it stood at the end of each date.

        ADR 0021. One forward replay of the effective ledger, through the same
        :meth:`apply_transaction` every other path uses, pausing at the end of
        each requested date for *visit* to read state through the repositories
        as it normally would. A whole range costs one replay, not one per day.

        The replay runs inside :func:`scratch_savepoint`, so stored derived
        state is byte-identical afterwards -- inside the caller's transaction
        when there is one, so `pt value` can persist what it computed in the
        same transaction. *visit* must therefore return plain values, not
        anything that reads the database lazily.

        Prefer :func:`over_dates`, which skips the replay when stored state
        already is the answer.
        """
        wanted = sorted(set(dates))
        if not wanted:
            return Walk(())

        transactions = effective_ledger(self.repos.transactions.in_ledger_order())
        counters = {"positions": 0, "lots": 0, "dispositions": 0}
        warnings: list[str] = []
        cash: dict[tuple[int, str], Decimal] = {}
        results: list[T] = []

        with scratch_savepoint(self.repos.con):
            self.repos.clear_derived()
            pending = iter(transactions)
            following = next(pending, None)
            for on in wanted:
                while following is not None and following.trade_date <= on:
                    self._apply_reporting(following, counters, cash, warnings)
                    following = next(pending, None)
                self._write_cash(cash)
                results.append(visit(on))

        return Walk(tuple(results), tuple(warnings))

    # ── the live append path ─────────────────────────────────────────────────

    def apply_or_rebuild(self, txn: Transaction) -> bool:
        """Derive state for a just-appended *txn*. Returns True if it rebuilt.

        **Every live append goes through this, not through
        :meth:`apply_transaction` directly.**

        :meth:`apply_transaction` applies a row against derived state *as it
        currently stands*, which equals replaying the ledger only when the row
        sorts last in ledger order. A back-dated entry sorts before rows already
        applied, so incremental derivation consumes the wrong lots -- and the
        symptom is that a later `pt rebuild` silently changes realized gains and
        the tax they imply. ADR 0016 has the reproduction.

        Rebuilding is therefore not optional and not behind a flag: leaving the
        book in a state a rebuild would change is exactly the silently-wrong-
        number failure this repository is organised against, and "remember to
        run `pt rebuild`" is not a control.

        The caller is inside a database transaction, so the append and the
        rebuild land together or not at all.
        """
        if self.repos.transactions.count_after(txn.trade_date, txn.seq) == 0:
            self.apply_transaction(txn)
            return False
        self.rebuild()
        return True

    # ── per-transaction application ──────────────────────────────────────────

    def apply_transaction(
        self,
        txn: Transaction,
        *,
        counters: dict[str, int] | None = None,
        cash: dict[tuple[int, str], Decimal] | None = None,
    ) -> None:
        """Apply one ledger row to derived state.

        **This is the only code that derives state from a transaction**, and
        both paths go through it: `rebuild()` calls it for every row in ledger
        order, and a live `pt buy` calls it once for the row it just appended.

        That is deliberate and it is the whole reason the method is public. If
        the live path had its own implementation, the two could disagree -- and
        the symptom would be that `pt rebuild` silently changes your numbers,
        which is the failure ADR 0010 exists to make impossible.

        Args:
            counters: incremented in place when supplied, for `rebuild()`'s
                summary.
            cash: when supplied, balances accumulate here and the caller writes
                them once at the end -- which is what makes a full rebuild one
                write per account rather than one per transaction. When None,
                the balance is read and written immediately, which is what a
                live command needs.
        """
        counters = counters if counters is not None else {}
        for key in ("positions", "lots", "dispositions"):
            counters.setdefault(key, 0)

        account = self.repos.accounts.get(txn.account_id)
        if account is None:  # pragma: no cover -- foreign key prevents it
            raise ValidationError(
                f"unknown account {txn.account_id}", code="PT-E-ACCOUNT-NOT-FOUND"
            )

        self._move_cash(txn.account_id, account.currency, txn.net_cash_effect, cash)

        # A transfer's other side moves the counter account's cash too. This is
        # the one transaction type that touches two accounts, and it is one row
        # rather than two precisely so that this stays a single fact (ADR 0007).
        if txn.txn_type is TransactionType.TRANSFER and txn.counter_account_id is not None:
            counter = self.repos.accounts.get(txn.counter_account_id)
            if counter is not None:
                self._move_cash(
                    txn.counter_account_id, counter.currency, -txn.net_cash_effect, cash
                )

        if txn.instrument_id is None:
            return  # a pure cash event: nothing to position

        # ADR 0023. A change of intent: carries an instrument and no quantity,
        # so it is dispatched before the quantity check below.
        if txn.txn_type is TransactionType.POSITION_REGROUP:
            self._regroup(txn, counters)
            return

        # Dispatched BEFORE the quantity check, because a spinoff carries an
        # instrument but no quantity -- the quantity is a consequence of the
        # action, not an input to it. Ordering these the other way round meant
        # spinoffs were silently skipped on rebuild while splits, which do
        # carry a quantity, were not.
        if txn.txn_type in _CORPORATE_ACTIONS:
            self._corporate_action(txn, counters)
            return

        if txn.quantity is None:
            return

        if txn.txn_type in {
            TransactionType.BUY,
            TransactionType.SELL_SHORT,
            TransactionType.DIVIDEND_REINVEST,
            # A distribution taken in units is income and a lot in one row:
            # the gross is what the units cost, and no cash moved.
            TransactionType.CAPITAL_GAIN_LT,
            TransactionType.CAPITAL_GAIN_ST,
            # ADR 0015. A lot-creating path that consumes no cash: the cash
            # movement above was '0.00' and nothing else here has to change,
            # which is the whole argument for a transaction type rather than a
            # back-dated buy plus an invented deposit.
            TransactionType.TRANSFER_IN,
        }:
            self._open(txn, counters)
        elif txn.txn_type in {
            TransactionType.SELL,
            TransactionType.BUY_TO_COVER,
            TransactionType.TRANSFER_OUT,
        }:
            self._close(txn, counters)

    def _move_cash(
        self,
        account_id: int,
        currency: str,
        delta: Decimal,
        accumulator: dict[tuple[int, str], Decimal] | None,
    ) -> None:
        """Apply a cash movement, batched during a rebuild and immediate live."""
        key = (account_id, currency)
        if accumulator is not None:
            with money_context():
                accumulator[key] = quantize_money(accumulator.get(key, ZERO) + delta)
            return
        current, _margin = self.repos.valuations.cash(account_id, currency=currency)
        with money_context():
            self.repos.valuations.set_cash(
                account_id, quantize_money(current + delta), currency=currency
            )

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

        in_kind = txn.txn_type is TransactionType.TRANSFER_IN
        with money_context():
            if in_kind:
                # ADR 0015, and the distinction the whole type exists for. The
                # lot's basis is what the OWNER paid at the DELIVERING
                # custodian; the transfer's own value (price, gross_amount) is
                # the flow amount and has nothing to do with it. Reading gross
                # here would be the single most common way this is got wrong:
                # every future sale would report the gain since the transfer
                # rather than since the purchase.
                #
                # `original_basis` is None only under BasisSource.UNAVAILABLE,
                # where no equation constrains the block (ADR 0017 §2b). The
                # lot is then seeded at the transfer's stated value so the
                # arithmetic closes -- and that value is not a basis claim,
                # which is precisely what `basis_source` records.
                basis = quantize_money(
                    txn.original_basis
                    if txn.original_basis is not None
                    else abs(txn.gross_amount or ZERO)
                )
            else:
                gross = (
                    txn.gross_amount
                    if txn.gross_amount is not None
                    else ((txn.price or ZERO) * txn.quantity * instrument.contract_size)
                )
                # Commissions and fees are part of basis on an opening trade:
                # they are a cost of acquiring, not a separate deduction.
                basis = quantize_money(abs(gross) + txn.total_costs)
            per_unit = basis / txn.quantity if txn.quantity else ZERO

        # A change of custodian is not a disposition: the holding period runs
        # from the original acquisition. Restarting it would convert long-term
        # gains into short-term ones on the next sale -- a wrong number in the
        # direction of a larger tax bill, arrived at silently.
        #
        # So `trade_date` and `open_date` are no longer the same date for these
        # rows. Replay ordering still uses (trade_date, seq): the ledger's order
        # is the order things were recorded, while lot ageing uses open_date.
        acquired = txn.original_acquired_date if in_kind else None
        opened = acquired or txn.trade_date

        self.repos.lots.add(
            Lot(
                lot_id=0,
                leg_id=leg_id,
                position_id=position_id,
                instrument_id=txn.instrument_id,
                account_id=txn.account_id,
                open_date=opened,
                open_txn_id=txn.txn_id,
                original_quantity=txn.quantity,
                remaining_quantity=txn.quantity,
                per_unit_price=per_unit,
                original_cost_basis=basis,
                adjusted_cost_basis=basis,
                holding_period_start=opened,
                # ADR 0017. Carried from the ledger row rather than decided
                # here: invariant 3 requires derived state to be reproducible
                # by replay, and the difference between `reconstructed`,
                # `estimated` and `unavailable` is an assertion nobody can
                # recompute from the numbers.
                basis_source=txn.basis_source or BasisSource.DERIVED,
                basis_assumption=txn.basis_assumption,
                allocated_fees=txn.total_costs,
                is_short=is_short,
                status=LotStatus.OPEN,
            )
        )
        counters["lots"] += 1
        self._refresh_leg(leg_id, txn.trade_date)

    def _regroup(self, txn: Transaction, counters: dict[str, int]) -> None:
        """Move this account's leg in an instrument into another position.

        ADR 0023. The target is the position the row's ``related_txn_id``
        opened -- named by its opening transaction because ``position_id`` is a
        surrogate a rebuild renumbers -- or, when that is NULL, a new position
        this row opens. Nothing but the grouping changes: no quantity, no lot,
        no basis, no holding period (ADR 0009). A position its last leg left is
        closed that day; one that still has legs is renamed from them.

        Raises:
            ValidationError: there is no open leg to move, or no open position
                in this account for it to move into. Replay reports it as it
                reports any row it cannot apply.
        """
        assert txn.instrument_id is not None
        leg = self.repos.positions.leg_for(txn.account_id, txn.instrument_id)
        if leg is None:
            raise ValidationError(
                f"nothing to regroup: no open leg in instrument {txn.instrument_id} "
                f"in this account on {txn.trade_date.isoformat()}",
                txn_id=txn.txn_id,
            )
        source_id = leg.position_id

        if txn.related_txn_id is None:
            target_id = self.repos.positions.add(
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
        else:
            target = self.repos.positions.opened_by(txn.related_txn_id)
            if (
                target is None
                or target.account_id != txn.account_id
                or target.status is not PositionStatus.OPEN
            ):
                raise ValidationError(
                    f"no open position in this account was opened by transaction "
                    f"{txn.related_txn_id} on {txn.trade_date.isoformat()}",
                    txn_id=txn.txn_id,
                )
            target_id = target.position_id

        if target_id != source_id:
            self.repos.positions.move_leg(leg.leg_id, target_id)
            remaining = self.repos.positions.legs(source_id)
            if not any(other.status is PositionStatus.OPEN for other in remaining):
                self.repos.positions.close_position(source_id, txn.trade_date)
            else:
                self.repos.positions.set_strategy(
                    source_id, str(PositionEngine.infer_strategy(remaining))
                )

        strategy = txn.strategy_type or PositionEngine.infer_strategy(
            self.repos.positions.legs(target_id)
        )
        self.repos.positions.set_strategy(target_id, str(strategy))

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

    def _corporate_action(self, txn: Transaction, counters: dict[str, int]) -> None:
        """Reapply a corporate action during replay.

        The parameters come from the `corporate_action` REFERENCE row, not from
        the ledger entry: a ledger row saying "a split happened" is not enough
        to redo it, and a free-text note is not machine-readable. That is why
        `pt ca split` writes both.

        Without this, `pt rebuild` silently reverted a split -- 300 shares back
        to 100 -- which is exactly the class of failure CLAUDE.md invariant 3
        exists to make impossible. It went unnoticed until the walkthrough was
        run end to end, which is the argument for running it.
        """
        assert txn.instrument_id is not None
        action = self.repos.corporate_actions.for_instrument(
            txn.instrument_id, _ACTION_TYPE_FOR[txn.txn_type], txn.trade_date
        )
        if action is None:
            raise ValidationError(
                f"transaction {txn.txn_id} records a {txn.txn_type} but no matching "
                "corporate_action row carries its parameters, so it cannot be "
                "reapplied",
                code=E_REPLAY_MISMATCH,
                remedy=(
                    "Re-enter the action with `pt ca ...`, which records both the "
                    "ledger entry and the parameters replay needs."
                ),
                txn_id=txn.txn_id,
            )

        if txn.txn_type in {TransactionType.SPLIT, TransactionType.REVERSE_SPLIT}:
            self._replay_split(txn, action)
        elif txn.txn_type is TransactionType.SPINOFF:
            self._replay_spinoff(txn, action, counters)
        elif txn.txn_type is TransactionType.MERGER_STOCK:
            self._replay_conversion(txn, action, counters)

    def _replay_split(self, txn: Transaction, action: CorporateAction) -> None:
        assert txn.instrument_id is not None
        account = self.repos.accounts.get(txn.account_id)
        if account is None or action.split_numerator is None:
            return

        lots = self.repos.lots.open_lots(
            txn.account_id, txn.instrument_id, as_of=txn.trade_date
        )
        if not lots:
            return

        result = self.corporate_actions.split(
            lots,
            numerator=action.split_numerator,
            denominator=action.split_denominator or Decimal(1),
            ex_date=txn.trade_date,
            txn_id=txn.txn_id,
            allows_fractional=account.allows_fractional,
        )
        for adjusted, adjustment in zip(result.lots, result.adjustments, strict=True):
            self.repos.lots.update_basis(adjusted)
            self.repos.lots.add_adjustment(replace(adjustment, adjustment_id=0))
        for leg_id in {lot.leg_id for lot in lots}:
            self._refresh_leg(leg_id, txn.trade_date)

    def _replay_spinoff(
        self, txn: Transaction, action: CorporateAction, counters: dict[str, int]
    ) -> None:
        assert txn.instrument_id is not None
        account = self.repos.accounts.get(txn.account_id)
        if (
            account is None
            or action.target_instrument_id is None
            or action.target_ratio is None
        ):
            return

        lots = self.repos.lots.open_lots(
            txn.account_id, txn.instrument_id, as_of=txn.trade_date
        )
        if not lots:
            return

        position_id = self.repos.positions.add(
            Position(
                position_id=0,
                account_id=txn.account_id,
                strategy_type=StrategyType.SINGLE,
                opened_date=txn.trade_date,
                status=PositionStatus.OPEN,
                opened_txn_id=txn.txn_id,
            )
        )
        counters["positions"] += 1
        leg_id = self.repos.positions.add_leg(
            PositionLeg(
                leg_id=0,
                position_id=position_id,
                instrument_id=action.target_instrument_id,
                role=LegRole.LONG_STOCK,
                sign=1,
                quantity=ZERO,
                opened_date=txn.trade_date,
            )
        )

        outcome = self.corporate_actions.spinoff(
            lots,
            ratio=action.target_ratio,
            parent_fmv=action.parent_fmv or ZERO,
            spun_fmv=action.target_fmv or ZERO,
            ex_date=txn.trade_date,
            spun_instrument_id=action.target_instrument_id,
            spun_leg_id=leg_id,
            spun_position_id=position_id,
            txn_id=txn.txn_id,
            allows_fractional=account.allows_fractional,
        )
        for adjusted, adjustment in zip(outcome.parent_lots, outcome.adjustments, strict=True):
            self.repos.lots.update_basis(adjusted)
            self.repos.lots.add_adjustment(replace(adjustment, adjustment_id=0))
        for spun in outcome.spun_lots:
            self.repos.lots.add(replace(spun, lot_id=0))
            counters["lots"] += 1

        self._refresh_leg(leg_id, txn.trade_date)
        for parent_leg in {lot.leg_id for lot in lots}:
            self._refresh_leg(parent_leg, txn.trade_date)

    def _replay_conversion(
        self, txn: Transaction, action: CorporateAction, counters: dict[str, int]
    ) -> None:
        """Reapply a share-class exchange: every old lot becomes a new one.

        The units delivered are on the reference row (`target_ratio` times the
        quantity converted, recorded by `pt ca convert`), so the replay lands
        on exactly the count the custodian stated rather than a rounding of it.
        """
        assert txn.instrument_id is not None
        if action.target_instrument_id is None or action.target_ratio is None:
            return
        lots = self.repos.lots.open_lots(
            txn.account_id, txn.instrument_id, as_of=txn.trade_date
        )
        if not lots:
            return
        with money_context():
            total_old = sum((lot.remaining_quantity for lot in lots), ZERO)
            target_units = quantize_quantity(total_old * action.target_ratio)
        if txn.quantity is not None:
            # The ledger row carries the delivered count exactly; the ratio on
            # the reference row is its derivation, kept for the record.
            target_units = abs(txn.quantity)

        position_id, leg_id = self._leg_for_target(
            txn.account_id, action.target_instrument_id, txn, counters
        )
        outcome = self.corporate_actions.convert(
            lots,
            target_units=target_units,
            ex_date=txn.trade_date,
            target_instrument_id=action.target_instrument_id,
            target_leg_id=leg_id,
            target_position_id=position_id,
            txn_id=txn.txn_id,
        )
        for closed, adjustment in zip(outcome.closed_lots, outcome.adjustments, strict=True):
            self.repos.lots.update_after_disposition(closed)
            self.repos.lots.add_adjustment(replace(adjustment, adjustment_id=0))
        for opened in outcome.new_lots:
            self.repos.lots.add(replace(opened, lot_id=0))
            counters["lots"] += 1
        self._refresh_leg(leg_id, txn.trade_date)
        for old_leg in {lot.leg_id for lot in lots}:
            self._refresh_leg(old_leg, txn.trade_date)

    def _leg_for_target(
        self, account_id: int, instrument_id: int, txn: Transaction, counters: dict[str, int]
    ) -> tuple[int, int]:
        """The leg the converted units land on: the existing one, or a new position."""
        leg = self.repos.positions.leg_for(account_id, instrument_id)
        if leg is not None:
            return leg.position_id, leg.leg_id
        position_id = self.repos.positions.add(
            Position(
                position_id=0,
                account_id=account_id,
                strategy_type=StrategyType.SINGLE,
                opened_date=txn.trade_date,
                status=PositionStatus.OPEN,
                opened_txn_id=txn.txn_id,
                note=txn.note,
            )
        )
        counters["positions"] += 1
        leg_id = self.repos.positions.add_leg(
            PositionLeg(
                leg_id=0,
                position_id=position_id,
                instrument_id=instrument_id,
                role=LegRole.LONG_STOCK,
                sign=1,
                quantity=ZERO,
                opened_date=txn.trade_date,
            )
        )
        return position_id, leg_id

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
        # The same netting rebuild() applies, from the same function --
        # otherwise this check would disagree with the balances rebuild() just
        # wrote, and report a break that is really a difference of opinion
        # between two functions.
        effective = effective_ledger(self.repos.transactions.in_ledger_order())

        problems: list[str] = []
        for account in self.repos.accounts.all():
            with money_context():
                ledger_total = ZERO
                for txn in effective:
                    if txn.account_id == account.account_id:
                        ledger_total += txn.net_cash_effect
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


def over_dates(
    repos: Repositories, dates: Iterable[date], visit: Callable[[date], T]
) -> Walk[T]:
    """:meth:`ReplayEngine.walk`, skipping the replay when it cannot matter.

    When no ledger row is dated after the earliest of *dates*, stored derived
    state already is the state at every one of them: nothing remains to be
    replayed past it, and ADR 0016 keeps stored state equal to a replay (which
    `pt validate` checks). So *visit* reads it directly. Otherwise the walk
    rewinds derived state in a savepoint and replays forward (ADR 0021).
    """
    wanted = sorted(set(dates))
    if not wanted:
        return Walk(())
    if not repos.transactions.any_after(wanted[0]):
        return Walk(tuple(visit(on) for on in wanted))
    return ReplayEngine(repos).walk(wanted, visit)


def state_on(repos: Repositories, on: date, read: Callable[[], T]) -> Walk[T]:
    """Run *read* against derived state as it stood at the end of *on*.

    For a command that reports positions, lots, cash or gains "as of" a date:
    reading current state there and labelling it with the past date is the
    defect ADR 0021 records -- today's book at yesterday's prices, marked
    complete. ``results[0]`` is what *read* returned.
    """
    return over_dates(repos, (on,), lambda _on: read())
