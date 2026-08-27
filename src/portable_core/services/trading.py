"""Recording transactions: the service `pt`'s trading commands call.

One responsibility: validate a proposed transaction, build the ledger row, and
apply it to derived state -- **through the same
:meth:`ReplayEngine.apply_transaction` a rebuild uses**. That shared path is
what guarantees a live command and a later `pt rebuild` produce the same
numbers. If they had separate implementations, the symptom of a divergence
would be that rebuilding silently changes your book, which is the failure
ADR 0010 exists to make impossible.

``--dry-run`` cuts between :meth:`plan` and :meth:`commit`, so a dry run runs
the same validation and the same arithmetic as the real thing and merely does
not write.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal

from portable_core.decimals import money_context, quantize_money
from portable_core.domain.enums import (
    AccountStatus,
    FeeClass,
    ReliefMethod,
    TransactionSource,
    TransactionType,
)
from portable_core.domain.models import Account, Instrument, Transaction
from portable_core.errors import ValidationError
from portable_core.errors.kinds import (
    E_ACCOUNT_CLOSED,
    E_CASH_INSUFFICIENT,
    E_FEE_CLASS_MISSING,
    E_FRACTIONAL_SHARE,
)
from portable_core.persistence.repositories import Repositories
from portable_core.services.lots import LotEngine, ReliefPlan, parse_lot_selection
from portable_core.services.replay import ReplayEngine

__all__ = ["TradeIntent", "TradePlan", "TradingService"]

ZERO = Decimal("0.00")

#: Trade types that open or add. The rest reduce.
_OPENING = {TransactionType.BUY, TransactionType.SELL_SHORT}


@dataclass(frozen=True, slots=True)
class TradeIntent:
    """What the user asked for, before it is checked."""

    account: Account
    instrument: Instrument
    txn_type: TransactionType
    quantity: Decimal
    price: Decimal
    trade_date: date
    fees: Decimal = ZERO
    commissions: Decimal = ZERO
    fee_class: FeeClass | None = None
    relief_method: ReliefMethod | None = None
    lot_selection: str | None = None
    settlement_date: date | None = None
    position_id: int | None = None
    note: str | None = None
    external_ref: str | None = None


@dataclass(frozen=True, slots=True)
class TradePlan:
    """What will happen. Rendered by ``--dry-run``, then committed unchanged."""

    intent: TradeIntent
    transaction: Transaction
    gross_amount: Decimal
    net_cash_effect: Decimal
    relief_plan: ReliefPlan | None = None
    warnings: tuple[str, ...] = ()

    @property
    def is_closing(self) -> bool:
        return self.relief_plan is not None


class TradingService:
    """Validates and records trades."""

    def __init__(self, repos: Repositories) -> None:
        self.repos = repos
        self.lots = LotEngine()
        self.replay = ReplayEngine(repos)

    # ── planning ─────────────────────────────────────────────────────────────

    def plan(self, intent: TradeIntent) -> TradePlan:
        """Validate and compute, writing nothing.

        Every refusal in this method is a case where guessing would produce a
        plausible number: a closed account, an unclassified fee, a fractional
        share the custodian cannot hold, a sale with no matching lot.
        """
        warnings: list[str] = []
        self._check_account(intent)
        self._check_quantity(intent)
        self._check_fee_class(intent)

        with money_context():
            gross = quantize_money(
                intent.price * intent.quantity * intent.instrument.contract_size
            )
            costs = intent.fees + intent.commissions
            # An opening trade pays out gross plus costs; a closing trade
            # receives gross minus costs. Costs always reduce the cash the
            # trader ends up with, in both directions.
            if intent.txn_type in _OPENING:
                net_cash = quantize_money(-(gross + costs))
            else:
                net_cash = quantize_money(gross - costs)

        relief_plan: ReliefPlan | None = None
        if intent.txn_type not in _OPENING:
            relief_plan = self._plan_relief(intent)

        if intent.txn_type is TransactionType.BUY:
            balance, _margin = self.repos.valuations.cash(
                intent.account.account_id, currency=intent.account.currency
            )
            if balance + net_cash < 0:
                warnings.append(
                    f"this purchase takes {intent.account.name} to "
                    f"{balance + net_cash} — a margin balance. Record the margin loan "
                    "explicitly if that is not what you meant."
                )

        transaction = Transaction(
            txn_id=0,
            account_id=intent.account.account_id,
            trade_date=intent.trade_date,
            seq=self.repos.transactions.next_seq(intent.trade_date),
            txn_type=intent.txn_type,
            net_cash_effect=net_cash,
            settlement_date=intent.settlement_date,
            instrument_id=intent.instrument.instrument_id,
            quantity=intent.quantity,
            price=intent.price,
            gross_amount=gross,
            fees=quantize_money(intent.fees),
            commissions=quantize_money(intent.commissions),
            fee_class=intent.fee_class,
            position_id=intent.position_id,
            lot_selection=intent.lot_selection,
            relief_method=intent.relief_method,
            note=intent.note,
            external_ref=intent.external_ref,
            source=TransactionSource.MANUAL,
            created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

        return TradePlan(
            intent=intent,
            transaction=transaction,
            gross_amount=gross,
            net_cash_effect=net_cash,
            relief_plan=relief_plan,
            warnings=tuple(warnings),
        )

    # ── committing ───────────────────────────────────────────────────────────

    def commit(self, plan: TradePlan) -> Transaction:
        """Append the ledger row and derive state from it.

        The caller wraps this in a database transaction, so a ledger row whose
        derived state failed to land does not exist -- which would otherwise
        break the replay invariant with no error to point at.
        """
        txn_id = self.repos.transactions.append(plan.transaction)
        stored = replace(plan.transaction, txn_id=txn_id)
        # The same method a rebuild uses. See the module docstring.
        self.replay.apply_transaction(stored)
        return stored

    # ── checks ───────────────────────────────────────────────────────────────

    @staticmethod
    def _check_account(intent: TradeIntent) -> None:
        if intent.account.status is AccountStatus.CLOSED:
            raise ValidationError(
                f"account {intent.account.name!r} is closed",
                code=E_ACCOUNT_CLOSED,
                remedy=(
                    "Trade in an open account. A closed account keeps its history "
                    "but takes no new entries."
                ),
                account=intent.account.name,
            )
        if intent.trade_date < intent.account.opened_date:
            raise ValidationError(
                f"trade date {intent.trade_date.isoformat()} precedes the opening of "
                f"{intent.account.name!r} on {intent.account.opened_date.isoformat()}",
                remedy="Check the date, or correct the account's opened date.",
                account=intent.account.name,
            )

    @staticmethod
    def _check_quantity(intent: TradeIntent) -> None:
        if intent.quantity <= 0:
            raise ValidationError(
                f"quantity must be positive, got {intent.quantity}",
                remedy="Direction comes from the command (buy/sell/short/cover), "
                "not from the sign of the quantity.",
                quantity=str(intent.quantity),
            )
        from portable_core.decimals import is_whole

        if intent.instrument.is_option and not is_whole(intent.quantity):
            raise ValidationError(
                f"cannot trade {intent.quantity} option contracts",
                code=E_FRACTIONAL_SHARE,
                remedy="Option contracts are whole. Check the quantity.",
                quantity=str(intent.quantity),
            )
        if not intent.account.allows_fractional and not is_whole(intent.quantity):
            raise ValidationError(
                f"{intent.account.name} does not hold fractional shares ({intent.quantity})",
                code=E_FRACTIONAL_SHARE,
                remedy=(
                    "Set --allows-fractional on the account if the custodian permits "
                    "it, or round the order yourself -- portable will not round a "
                    "quantity on your behalf."
                ),
                quantity=str(intent.quantity),
            )

    @staticmethod
    def _check_fee_class(intent: TradeIntent) -> None:
        """PORT-GIPS-D01: a fee with no classification is refused, not guessed.

        The three return bases are derived from this classification, so an
        unclassified fee makes every one of them unanswerable. The schema
        enforces it too; this is the layer that can explain it.
        """
        if intent.fees == 0 and intent.commissions == 0:
            return
        if intent.fee_class is not None:
            return
        raise ValidationError(
            "this trade has fees but no fee classification",
            code=E_FEE_CLASS_MISSING,
            remedy=(
                "Pass --fee-class. A brokerage commission is `transaction_cost`. "
                "Note that a custody fee is NOT a transaction cost: under the Asset "
                "Owner ladder portable follows it is an `internal_mgmt_cost` and "
                "reduces net-of-fees returns only. The three return bases are "
                "derived from this, so portable will not guess it (PORT-GIPS-D01)."
            ),
            fees=str(intent.fees),
            commissions=str(intent.commissions),
            choices=[str(f) for f in FeeClass],
        )

    def _plan_relief(self, intent: TradeIntent) -> ReliefPlan:
        method = intent.relief_method or intent.account.default_relief_method
        prior = {
            ReliefMethod(m)
            for m in self.repos.lots.methods_used_for(intent.instrument.instrument_id)
        }
        LotEngine.check_method_consistency(intent.instrument.instrument_id, method, prior)

        open_lots = self.repos.lots.open_lots(
            intent.account.account_id, intent.instrument.instrument_id
        )
        selection = parse_lot_selection(intent.lot_selection) if intent.lot_selection else None
        return self.lots.select(
            open_lots, intent.quantity, method, intent.trade_date, selection=selection
        )

    # ── cash ─────────────────────────────────────────────────────────────────

    def record_cash(
        self,
        account: Account,
        txn_type: TransactionType,
        amount: Decimal,
        on: date,
        *,
        counter_account: Account | None = None,
        fee_class: FeeClass | None = None,
        note: str | None = None,
        external_ref: str | None = None,
        allow_overdraft: bool = False,
    ) -> Transaction:
        """Build a cash transaction. The caller commits it.

        Sign convention: *amount* is always positive and the direction comes
        from *txn_type*. Letting a negative amount mean "withdrawal" would make
        `pt cash deposit --amount -500` a silent withdrawal.
        """
        if amount <= 0:
            raise ValidationError(
                f"amount must be positive, got {amount}",
                remedy=(
                    "Direction comes from the command -- deposit, withdraw, transfer "
                    "-- not from the sign."
                ),
                amount=str(amount),
            )

        outward = txn_type in {
            TransactionType.WITHDRAWAL,
            TransactionType.FEE,
            TransactionType.MARGIN_INTEREST,
        } or (txn_type is TransactionType.TRANSFER)
        net = quantize_money(-amount if outward else amount)

        if outward and not allow_overdraft:
            balance, _margin = self.repos.valuations.cash(
                account.account_id, currency=account.currency
            )
            if balance + net < 0:
                raise ValidationError(
                    f"{account.name} holds {balance} but this would take it to {balance + net}",
                    code=E_CASH_INSUFFICIENT,
                    remedy=(
                        "Check the amount, or pass --allow-overdraft if the account "
                        "genuinely runs a margin balance."
                    ),
                    account=account.name,
                    balance=str(balance),
                    amount=str(amount),
                )

        if txn_type is TransactionType.FEE and fee_class is None:
            raise ValidationError(
                "a fee needs a classification",
                code=E_FEE_CLASS_MISSING,
                remedy=(
                    "Pass --fee-class. Custody is `internal_mgmt_cost` under the "
                    "Asset Owner ladder, not a transaction cost; a wire fee is "
                    "`other_admin` and reduces no GIPS return basis at all "
                    "(PORT-GIPS-D01)."
                ),
                choices=[str(f) for f in FeeClass],
            )

        return Transaction(
            txn_id=0,
            account_id=account.account_id,
            trade_date=on,
            seq=self.repos.transactions.next_seq(on),
            txn_type=txn_type,
            net_cash_effect=net,
            gross_amount=quantize_money(amount),
            fees=quantize_money(amount) if txn_type is TransactionType.FEE else ZERO,
            fee_class=fee_class,
            counter_account_id=(
                counter_account.account_id if counter_account is not None else None
            ),
            note=note,
            external_ref=external_ref,
            source=TransactionSource.MANUAL,
            created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
