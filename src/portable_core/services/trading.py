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

from portable_core.decimals import is_whole, money_context, quantize_money, quantize_quantity
from portable_core.domain.enums import (
    AccountStatus,
    BasisSource,
    FeeClass,
    ReliefMethod,
    TransactionSource,
    TransactionType,
)
from portable_core.domain.models import Account, Instrument, Transaction
from portable_core.errors import ValidationError
from portable_core.errors.kinds import (
    E_ACCOUNT_CLOSED,
    E_BASIS_SOURCE_INVALID,
    E_CASH_INSUFFICIENT,
    E_FEE_CLASS_MISSING,
    E_FRACTIONAL_SHARE,
    E_WITHHOLDING_INVALID,
)
from portable_core.persistence.repositories import Repositories
from portable_core.services.lots import LotEngine, ReliefPlan, parse_lot_selection
from portable_core.services.replay import ReplayEngine

__all__ = ["CommitResult", "TradeIntent", "TradePlan", "TradingService"]

ZERO = Decimal("0.00")

#: The rungs of ADR 0017's ladder that rest on an assumption rather than on a
#: figure somebody stated. Each must say what the assumption was.
_APPROXIMATE = frozenset(
    {BasisSource.RECONSTRUCTED, BasisSource.ESTIMATED, BasisSource.UNAVAILABLE}
)

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
    #: Where this row came from. `manual` unless an importer says otherwise --
    #: an imported row that claims to be hand-entered is untraceable back to the
    #: document that produced it (`PORT-GIPS-J03`).
    source: TransactionSource = TransactionSource.MANUAL


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


@dataclass(frozen=True, slots=True)
class CommitResult:
    """What committing a plan did.

    ``rebuilt`` is carried rather than discarded because a back-dated entry
    re-deriving the whole book is something the user should see -- particularly
    when it changes a realized gain already reported (ADR 0016).
    """

    transaction: Transaction
    rebuilt: bool


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
        self.check_external_ref(intent.account, intent.external_ref)

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
            source=intent.source,
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

    def commit(self, plan: TradePlan) -> CommitResult:
        """Append the ledger row and derive state from it.

        The caller wraps this in a database transaction, so a ledger row whose
        derived state failed to land does not exist -- which would otherwise
        break the replay invariant with no error to point at.
        """
        txn_id = self.repos.transactions.append(plan.transaction)
        stored = replace(plan.transaction, txn_id=txn_id)
        # The same derivation a rebuild uses, and a full rebuild where this row
        # is back-dated. See the module docstring and ADR 0016.
        rebuilt = self.replay.apply_or_rebuild(stored)
        return CommitResult(transaction=stored, rebuilt=rebuilt)

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
        source: TransactionSource = TransactionSource.MANUAL,
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

        self.check_external_ref(account, external_ref)

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
            source=source,
            created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    # ── income ───────────────────────────────────────────────────────────────

    def record_income(
        self,
        account: Account,
        instrument: Instrument,
        txn_type: TransactionType,
        gross: Decimal,
        pay_date: date,
        *,
        ex_date: date | None = None,
        taxes_withheld: Decimal = ZERO,
        withholding_reclaimable: Decimal | None = None,
        is_qualified: bool | None = None,
        reinvested_units: Decimal | None = None,
        note: str | None = None,
        external_ref: str | None = None,
        source: TransactionSource = TransactionSource.MANUAL,
    ) -> Transaction:
        """Build an income transaction. The caller commits it.

        Recognition is on the pay date; the ex-date drives the accrual
        `ValuationEngine` picks up between the two (`PORT-GIPS-A06`).

        **A reinvested distribution is income and a lot, in one row.** With
        ``txn_type`` `DIVIDEND_REINVEST` and ``reinvested_units``, the gross is
        the income earned and also the cost of the units bought with it, so
        the row opens a lot (`ReplayEngine` treats it as an opening) and moves
        no cash. Two rows -- a dividend and a buy -- would say the same thing
        and put an external-flow-shaped pair in the cash ledger that never
        happened. Withholding on a reinvestment is refused rather than netted:
        a fund that withholds does not also reinvest the gross.

        **Withholding is tax, not a fee.** `gross_amount` stays the income the
        instrument paid and `net_cash_effect` is what actually landed, because
        those are two different facts and a report needs both: the return is
        earned on the gross, and the cash balance moved by the net. Netting them
        at entry would make the withholding unrecoverable from the ledger.

        Reclaimable and non-reclaimable withholding are stored separately
        because they behave differently -- reclaimable is accrued, and
        non-reclaimable reduces return (`PORT-GIPS-A06`). One combined figure
        cannot answer both, which is why the schema carries two columns.
        """
        if gross <= 0:
            raise ValidationError(
                f"income amount must be positive, got {gross}",
                remedy="Direction comes from the command, not from the sign.",
                amount=str(gross),
            )

        ex = ex_date if ex_date is not None else pay_date
        if ex > pay_date:
            raise ValidationError(
                f"ex-date {ex.isoformat()} is after pay-date {pay_date.isoformat()}",
                remedy=(
                    "Entitlement is fixed on the ex-date and cash arrives on the "
                    "pay-date, so the ex-date comes first."
                ),
            )

        self.check_external_ref(account, external_ref)
        self._check_withholding(gross, taxes_withheld, withholding_reclaimable)

        # A dividend taken in units is its own type; a capital-gain
        # distribution keeps its type (the character is the type) and carries
        # the units on the row.
        may_reinvest = txn_type in {
            TransactionType.DIVIDEND_REINVEST,
            TransactionType.CAPITAL_GAIN_LT,
            TransactionType.CAPITAL_GAIN_ST,
        }
        reinvested = txn_type is TransactionType.DIVIDEND_REINVEST or (
            may_reinvest and reinvested_units is not None
        )
        if reinvested and (reinvested_units is None or reinvested_units <= 0):
            raise ValidationError(
                "a reinvested distribution states the units it bought",
                remedy="Pass the units received; the gross is what they cost.",
                amount=str(gross),
            )
        if not may_reinvest and reinvested_units is not None:
            raise ValidationError(
                f"units are stated on a {txn_type.value}, which reinvests nothing",
                remedy="Use dividend_reinvest for a distribution taken in units.",
            )
        if reinvested and taxes_withheld:
            raise ValidationError(
                "a reinvested distribution cannot also have tax withheld from it",
                remedy=(
                    "Record the gross as a dividend with --withheld and the units "
                    "as a buy, if that is what the custodian did."
                ),
            )

        with money_context():
            net = ZERO if reinvested else quantize_money(gross - taxes_withheld)
            price = (
                quantize_quantity(gross / reinvested_units)
                if reinvested and reinvested_units
                else None
            )

        return Transaction(
            txn_id=0,
            account_id=account.account_id,
            trade_date=pay_date,
            seq=self.repos.transactions.next_seq(pay_date),
            txn_type=txn_type,
            net_cash_effect=net,
            instrument_id=instrument.instrument_id,
            quantity=reinvested_units if reinvested else None,
            price=price,
            gross_amount=quantize_money(gross),
            taxes_withheld=quantize_money(taxes_withheld),
            withholding_reclaimable=(
                None
                if withholding_reclaimable is None
                else quantize_money(withholding_reclaimable)
            ),
            ex_date=ex,
            pay_date=pay_date,
            is_qualified=is_qualified,
            note=note,
            external_ref=external_ref,
            source=source,
            created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def check_external_ref(self, account: Account, external_ref: str | None) -> None:
        """Refuse a duplicate reference *before* the work, not at the insert.

        `TransactionRepository.append` refuses too, and that is what binds every
        writer -- including the corporate-action and options commands, which
        build their rows directly rather than through a service. This runs
        earlier so `--dry-run` reports the duplicate rather than planning a
        trade that could never be committed, and so the message can name the
        account rather than its id.
        """
        self.repos.transactions.refuse_if_ref_taken(
            account.account_id, external_ref, account_name=account.name
        )

    @staticmethod
    def _check_withholding(
        gross: Decimal, withheld: Decimal, reclaimable: Decimal | None
    ) -> None:
        """Refuse a withholding split that cannot be true.

        Each of these would otherwise produce a plausible number: withholding
        larger than the payment inverts the cash effect, and a reclaimable
        portion larger than what was withheld accrues a receivable that does not
        exist.
        """
        if withheld < 0:
            raise ValidationError(
                f"taxes withheld cannot be negative, got {withheld}",
                code=E_WITHHOLDING_INVALID,
                remedy="Withholding is stated as a positive amount deducted from the gross.",
                withheld=str(withheld),
            )
        if withheld > gross:
            raise ValidationError(
                f"withholding {withheld} exceeds the gross payment {gross}",
                code=E_WITHHOLDING_INVALID,
                remedy=(
                    "Check which figure is the gross. `--amount` is the payment before "
                    "withholding, not the cash that arrived."
                ),
                withheld=str(withheld),
                gross=str(gross),
            )
        if reclaimable is None:
            return
        if reclaimable < 0:
            raise ValidationError(
                f"reclaimable withholding cannot be negative, got {reclaimable}",
                code=E_WITHHOLDING_INVALID,
                remedy="State the reclaimable portion as a positive amount.",
                reclaimable=str(reclaimable),
            )
        if reclaimable > withheld:
            raise ValidationError(
                f"reclaimable withholding {reclaimable} exceeds the {withheld} withheld",
                code=E_WITHHOLDING_INVALID,
                remedy=(
                    "The reclaimable portion is part of the withholding, not additional "
                    "to it. Reclaimable is accrued and non-reclaimable reduces return "
                    "(PORT-GIPS-A06), so the split has to sit inside the total."
                ),
                reclaimable=str(reclaimable),
                withheld=str(withheld),
            )

    # ── In-kind transfers (ADR 0015) ─────────────────────────────────────────

    def record_transfer_in(
        self,
        account: Account,
        instrument: Instrument,
        quantity: Decimal,
        on: date,
        *,
        value: Decimal,
        original_basis: Decimal | None,
        original_acquired_date: date | None,
        basis_source: BasisSource,
        basis_assumption: str | None = None,
        note: str | None = None,
        external_ref: str | None = None,
        source: TransactionSource = TransactionSource.MANUAL,
    ) -> Transaction:
        """Securities arriving without being bought. The caller commits it.

        Two numbers travel on this row and **must not be conflated** -- the
        failure mode ADR 0015 exists to prevent:

        - ``value`` is the market value on the transfer date. It is the flow
          amount (``PORT-GIPS-C02``) and has nothing to do with tax.
        - ``original_basis`` and ``original_acquired_date`` are what the owner
          paid and when, at the delivering custodian. They are unrelated to the
          transfer and are what the tax engine uses forever after.

        Use the value as basis and every future sale reports the gain since the
        transfer rather than since the purchase. Use the basis as the flow
        amount and the period's return is wrong by the whole unrealized gain.

        ``net_cash_effect`` is zero. Nothing moved but securities, which is why
        this is a transaction type and not a back-dated buy plus an invented
        deposit -- that would fabricate an external cash flow for every seeded
        position, the one error class ADR 0007 exists to prevent.
        """
        self.check_external_ref(account, external_ref)
        self._check_in_kind(
            quantity,
            value,
            original_basis,
            original_acquired_date,
            basis_source,
            basis_assumption,
            on,
        )
        if not account.allows_fractional and not is_whole(quantity):
            raise ValidationError(
                f"{account.name} cannot hold fractional shares, got {quantity}",
                code=E_FRACTIONAL_SHARE,
                remedy="Transfer a whole number of shares, or allow fractions on the account.",
                quantity=str(quantity),
            )
        with money_context():
            gross = quantize_money(value)
        return Transaction(
            txn_id=0,
            account_id=account.account_id,
            trade_date=on,
            # Every other write path assigns this; these two did not, so two
            # in-kind transfers on one date collided on UNIQUE(trade_date,
            # seq). Seeding a cutover creates dozens on a single date, which
            # is the case this type exists for.
            seq=self.repos.transactions.next_seq(on),
            txn_type=TransactionType.TRANSFER_IN,
            instrument_id=instrument.instrument_id,
            quantity=quantity,
            price=(gross / quantity if quantity else ZERO),
            gross_amount=gross,
            net_cash_effect=ZERO,
            original_basis=(None if original_basis is None else quantize_money(original_basis)),
            original_acquired_date=original_acquired_date,
            basis_source=basis_source,
            basis_assumption=basis_assumption,
            note=note,
            external_ref=external_ref,
            source=source,
        )

    def record_transfer_out(
        self,
        account: Account,
        instrument: Instrument,
        quantity: Decimal,
        on: date,
        *,
        value: Decimal,
        relief_method: ReliefMethod | None = None,
        lot_selection: str | None = None,
        note: str | None = None,
        external_ref: str | None = None,
        source: TransactionSource = TransactionSource.MANUAL,
    ) -> Transaction:
        """Securities leaving without being sold. The caller commits it.

        The mirror of :meth:`record_transfer_in`, and simpler: the lots being
        relieved already carry their own basis and acquisition dates, so
        nothing external is asserted. ``value`` is the market value on the
        transfer date, which is the outward flow amount.

        A transfer out is **not a disposition**: no gain is realized, because
        nothing was sold. The lot relief that follows is a movement of
        securities out of the portfolio, and `pt tax` must not report it as a
        sale.
        """
        self.check_external_ref(account, external_ref)
        if quantity <= 0:
            raise ValidationError(
                f"quantity must be positive, got {quantity}",
                remedy="Direction comes from the command, not from the sign.",
                quantity=str(quantity),
            )
        if value < 0:
            raise ValidationError(
                f"transfer value cannot be negative, got {value}",
                remedy="State the market value on the transfer date as a positive amount.",
                value=str(value),
            )
        with money_context():
            gross = quantize_money(value)
        return Transaction(
            txn_id=0,
            account_id=account.account_id,
            trade_date=on,
            # Every other write path assigns this; these two did not, so two
            # in-kind transfers on one date collided on UNIQUE(trade_date,
            # seq). Seeding a cutover creates dozens on a single date, which
            # is the case this type exists for.
            seq=self.repos.transactions.next_seq(on),
            txn_type=TransactionType.TRANSFER_OUT,
            instrument_id=instrument.instrument_id,
            quantity=quantity,
            price=(gross / quantity if quantity else ZERO),
            gross_amount=gross,
            net_cash_effect=ZERO,
            # Which lots leave is the same question a sale asks, so it gets the
            # same controls. ADR 0017's consequence applies: a reconstructed
            # block is one lot, nameable but not divisible.
            relief_method=relief_method,
            lot_selection=lot_selection,
            note=note,
            external_ref=external_ref,
            source=source,
        )

    @staticmethod
    def _check_in_kind(
        quantity: Decimal,
        value: Decimal,
        original_basis: Decimal | None,
        original_acquired_date: date | None,
        basis_source: BasisSource,
        basis_assumption: str | None,
        on: date,
    ) -> None:
        """Everything about an in-kind transfer that cannot be true."""
        if quantity <= 0:
            raise ValidationError(
                f"quantity must be positive, got {quantity}",
                remedy="Direction comes from the command -- transfer in, transfer out.",
                quantity=str(quantity),
            )
        if value < 0:
            raise ValidationError(
                f"transfer value cannot be negative, got {value}",
                remedy="State the market value on the transfer date as a positive amount.",
                value=str(value),
            )
        if basis_source is BasisSource.DERIVED:
            raise ValidationError(
                "a transferred lot's basis cannot be 'derived'",
                code=E_BASIS_SOURCE_INVALID,
                remedy=(
                    "'derived' means portable computed it from its own ledger, and this "
                    "basis came from somewhere else. Use 'custodian_asserted' for a "
                    "lot-detail report, or one of the reconstruction sources (ADR 0017)."
                ),
                basis_source=str(basis_source),
            )
        if basis_source is BasisSource.UNAVAILABLE:
            if original_basis is not None:
                raise ValidationError(
                    "basis_source 'unavailable' carries no basis, but one was given",
                    code=E_BASIS_SOURCE_INVALID,
                    remedy=(
                        "'unavailable' means no equation constrains this block, so any "
                        "figure would be invented. If a basis IS known, say where it "
                        "came from instead."
                    ),
                    original_basis=str(original_basis),
                )
        elif original_basis is None:
            raise ValidationError(
                f"basis_source '{basis_source}' asserts a basis, but none was given",
                code=E_BASIS_SOURCE_INVALID,
                remedy=(
                    "Supply the basis, or use 'unavailable' if the evidence supports no "
                    "figure. Absent and unavailable are different claims."
                ),
                basis_source=str(basis_source),
            )
        if original_basis is not None and original_basis < 0:
            raise ValidationError(
                f"cost basis cannot be negative, got {original_basis}",
                code=E_BASIS_SOURCE_INVALID,
                remedy=(
                    "A genuine zero basis is legitimate -- a contra or CVR security from "
                    "an acquisition -- and is recorded as zero, not as a negative."
                ),
                original_basis=str(original_basis),
            )
        if basis_source in _APPROXIMATE and not (basis_assumption or "").strip():
            # ADR 0017 §2 asks for the assumption on the three approximate
            # rungs, not on `custodian_asserted` -- which is a figure somebody
            # else stated rather than one this code worked out. Enforced by a
            # CHECK on `lot` as well, but a service refusal is a sentence and an
            # IntegrityError is not.
            raise ValidationError(
                f"basis_source '{basis_source}' needs a stated assumption",
                code=E_BASIS_SOURCE_INVALID,
                remedy=(
                    "Say in a sentence how the basis was arrived at, so it can be "
                    "re-derived and re-argued later rather than merely trusted. "
                    "`custodian_asserted` does not need one."
                ),
                basis_source=str(basis_source),
            )
        if original_acquired_date is not None and original_acquired_date > on:
            raise ValidationError(
                f"acquired {original_acquired_date.isoformat()}, after the transfer "
                f"on {on.isoformat()}",
                code=E_BASIS_SOURCE_INVALID,
                remedy=(
                    "The acquisition date is when the OWNER bought the shares, at the "
                    "delivering custodian. It precedes the transfer by definition."
                ),
                acquired=original_acquired_date.isoformat(),
                transferred=on.isoformat(),
            )
