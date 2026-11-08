"""Wash sales, detected in replay across every account. ADR 0037.

IRC §1091 disallows a loss on a sale of stock or securities when substantially
identical ones are acquired within 30 days before or after it. The disallowed
loss is added to the replacement shares' basis (§1091(d)), and their holding
period absorbs the sold shares' (§1223). A replacement bought in a sheltered
account takes the loss away for good (Rev. Rul. 2008-5).

**Every pairing is made when the later of its two rows is applied.** A loss
sale looks back for acquisitions, and an acquisition looks back for losses not
yet matched. Replay applies rows in ledger order, so a rebuild and a live
append make the same pairings (ADR 0010, ADR 0016), and nothing here reads a
row that sorts after the one being applied.

What counts, and what does not, is ADR 0037's decision 2:

* a **loss sale** is a disposition at a loss of a long lot, in a taxable
  account, by a sale, an expiration, or the stock a written call delivers;
* a **replacement** is a long lot whose basis `portable` derived, opened by a
  buy, a reinvestment, a distribution taken in units, or stock acquired by an
  option, in any account, of the same instrument or one declared
  substantially identical. A lot transferred in, seeded from a custodian, or
  derived by a corporate action is never matched automatically (decision 7);
* the shares a lot acquired are never a replacement for a loss on shares the
  same row acquired: they are one purchase.

The matching order is decision 3: losses in ledger order, and within one sale
the earliest-acquired sold lot first; replacement shares in order of
acquisition. Each replacement share washes at most one sold share.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from portable_core.decimals import money_context, quantize_money
from portable_core.domain.enums import (
    BasisAdjustmentReason,
    BasisSource,
    LotStatus,
    TransactionType,
    WashSaleStatus,
)
from portable_core.domain.models import (
    Account,
    BasisAdjustment,
    Lot,
    LotDisposition,
    Transaction,
    WashSale,
)
from portable_core.persistence.repositories import Repositories
from portable_core.services.tax import TaxEngine

__all__ = [
    "LOSS_SALE_TYPES",
    "REPLACEMENT_TYPES",
    "WINDOW",
    "Candidate",
    "CandidateReason",
    "Notice",
    "WashSaleMatcher",
    "absorbed_start",
    "candidates",
    "custodian_overlaps",
    "split_key",
]

ZERO = Decimal("0.00")

#: 30 days before or after the sale, inclusive, in calendar days on trade
#: dates (decision 1, invariant 7).
WINDOW: Final = timedelta(days=30)

#: Rows whose lot can wash a loss: an acquisition by purchase (§1091(a)).
REPLACEMENT_TYPES: Final = frozenset(
    {
        TransactionType.BUY,
        TransactionType.DIVIDEND_REINVEST,
        TransactionType.CAPITAL_GAIN_LT,
        TransactionType.CAPITAL_GAIN_ST,
        # The stock a long call's exercise or a written put's assignment
        # acquires (ADR 0038).
        TransactionType.OPTION_EXERCISE,
        TransactionType.OPTION_ASSIGNMENT,
    }
)

#: Rows whose loss dispositions a wash sale can disallow. A cover is absent:
#: short sales are §1091(e)'s, and not detected in this version.
LOSS_SALE_TYPES: Final = frozenset(
    {
        TransactionType.SELL,
        TransactionType.OPTION_EXPIRATION,
        TransactionType.OPTION_ASSIGNMENT,
    }
)


def split_key(replacement_key: str, sale_txn_id: int, sold_lot_key: str) -> str:
    """The key of the shares split off a replacement lot (decision 6).

    Unique by construction: a replacement is always a lot an opening row
    created, so its key has no ``~``; matched shares never wash a second loss,
    so no split lot is split again by this rule; and the sale's transaction
    and the sold lot's key name the disposition.
    """
    return f"{replacement_key}~{sale_txn_id}.{sold_lot_key}"


def absorbed_start(
    *, sold_start: date, sale_date: date, replacement_start: date, replacement_date: date
) -> date:
    """The replacement's holding-period start once it absorbs the sold shares'.

    §1223: the replacement's holding period includes the period the sold shares
    were held, and no day is counted twice (decision 5).

    * Bought **after** the sale: its start moves earlier by the days the sold
      shares were held. The days between the sale and the purchase are not
      counted.
    * Bought **on or before** the sale: the periods overlap, and the start is
      the earlier of the two.
    """
    if replacement_date > sale_date:
        return replacement_start - (sale_date - sold_start)
    return min(replacement_start, sold_start)


@dataclass(frozen=True, slots=True)
class Notice:
    """Something a pairing did that the person entering the row should see."""

    txn_id: int
    message: str


@dataclass(slots=True)
class _Loss:
    """A loss disposition, with what of it is still unmatched."""

    disposition: LotDisposition
    sold: Lot
    account: Account
    matched: Decimal

    @property
    def open_quantity(self) -> Decimal:
        return self.disposition.quantity - self.matched


class WashSaleMatcher:
    """Makes ADR 0037's pairings as replay applies rows.

    Writes derived state only: the `wash_sale` rows, the split replacement
    lots and their basis adjustments, and the restated disposition and gain.
    """

    def __init__(self, repos: Repositories, tax: TaxEngine) -> None:
        self.repos = repos
        self.tax = tax
        self.notices: list[Notice] = []

    def notices_for(self, txn_id: int) -> tuple[str, ...]:
        """What applying *txn_id* paired, for the command that appended it."""
        return tuple(n.message for n in self.notices if n.txn_id == txn_id)

    # ── the two entry points ─────────────────────────────────────────────────

    def after_disposals(self, txn: Transaction, stored: list[LotDisposition]) -> None:
        """A sale was applied: look back for replacements bought before it."""
        if txn.txn_type not in LOSS_SALE_TYPES:
            return
        losses = [loss for d in stored if (loss := self._loss(d)) is not None]
        # Within one sale, the earliest-acquired sold lot first (decision 3).
        losses.sort(key=lambda x: (x.sold.open_date, x.sold.open_txn_id, x.sold.lot_key))
        for loss in losses:
            candidates = self.repos.lots.acquired_between(
                self._identical(loss.disposition.instrument_id),
                loss.disposition.disposition_date - WINDOW,
                loss.disposition.disposition_date,
            )
            for lot in candidates:
                if loss.open_quantity <= 0:
                    break
                if not self._replaces(lot, loss):
                    continue
                self._match_existing(txn, loss, lot, stored)

    def after_acquisition(self, txn: Transaction, lot: Lot) -> None:
        """A replacement was opened: look back for losses it disallows."""
        if txn.txn_type not in REPLACEMENT_TYPES or lot.is_short:
            return
        if lot.basis_source is not BasisSource.DERIVED:
            return
        found = self.repos.lots.dispositions_between(
            self._identical(lot.instrument_id), lot.open_date - WINDOW, lot.open_date
        )
        losses = [loss for d in found if (loss := self._loss(d)) is not None]
        losses = [
            x for x in losses if x.open_quantity > 0 and x.sold.open_txn_id != lot.open_txn_id
        ]
        losses.sort(key=self._ledger_order)
        current = lot
        for loss in losses:
            if current.remaining_quantity <= 0:
                break
            quantity = min(loss.open_quantity, current.remaining_quantity)
            current = self._split(txn, loss, current, quantity)

    # ── matching ─────────────────────────────────────────────────────────────

    def _match_existing(
        self, txn: Transaction, loss: _Loss, lot: Lot, stored: list[LotDisposition]
    ) -> None:
        """Match a loss against a lot bought before it: open shares, then gone ones."""
        open_shares = min(loss.open_quantity, lot.remaining_quantity)
        if open_shares > 0:
            lot = self._split(txn, loss, lot, open_shares)
        if loss.open_quantity <= 0:
            return
        # Shares of the lot disposed of before this sale. The ones this very
        # sale disposed of are the shares being sold, never a replacement.
        sold_here = sum((d.quantity for d in stored if d.lot_id == lot.lot_id), Decimal(0))
        reported = sum(
            (
                w.quantity
                for w in self.repos.wash_sales.for_replacement(lot.lot_key)
                if w.status is WashSaleStatus.MANUAL
            ),
            Decimal(0),
        )
        gone = lot.original_quantity - lot.remaining_quantity - sold_here - reported
        quantity = min(loss.open_quantity, gone)
        if quantity > 0:
            self._report_manual(txn, loss, lot, quantity)

    def _split(self, txn: Transaction, loss: _Loss, lot: Lot, quantity: Decimal) -> Lot:
        """Split *quantity* shares off *lot* to carry the loss they wash.

        Returns the lot as it stands after the split, for the next pairing.
        """
        replacement_account = self.repos.accounts.get(lot.account_id)
        assert replacement_account is not None
        sheltered = not replacement_account.is_taxable
        part = self._disallow(loss, quantity)
        sale = loss.disposition.disposition_date
        if sheltered:
            # Rev. Rul. 2008-5: no basis in the IRA is increased, and the
            # holding period does not matter there.
            start = lot.holding_period_start
            deferral = ZERO
            status = WashSaleStatus.PERMANENT
        else:
            start = absorbed_start(
                sold_start=loss.sold.holding_period_start,
                sale_date=sale,
                replacement_start=lot.holding_period_start,
                replacement_date=lot.open_date,
            )
            deferral = part
            status = WashSaleStatus.DEFERRED

        # The pairing is made when the later of the two rows is applied.
        split_date = max(sale, lot.open_date)
        key = split_key(lot.lot_key, loss.disposition.txn_id, loss.sold.lot_key)
        with money_context():
            moved_original = quantize_money(
                lot.original_cost_basis * quantity / lot.original_quantity
            )
            moved_adjusted = quantize_money(
                lot.adjusted_cost_basis * quantity / lot.remaining_quantity
            )
            moved_fees = quantize_money(lot.allocated_fees * quantity / lot.original_quantity)
            child_basis = moved_adjusted + deferral

        remaining = lot.remaining_quantity - quantity
        rest = replace(
            lot,
            original_quantity=lot.original_quantity - quantity,
            remaining_quantity=remaining,
            original_cost_basis=lot.original_cost_basis - moved_original,
            adjusted_cost_basis=lot.adjusted_cost_basis - moved_adjusted,
            allocated_fees=lot.allocated_fees - moved_fees,
            per_unit_price=(
                (lot.adjusted_cost_basis - moved_adjusted) / remaining if remaining else ZERO
            ),
            # Nothing of it is open once its last open shares are split off.
            status=LotStatus.CLOSED if remaining == 0 else lot.status,
            closed_date=split_date if remaining == 0 else lot.closed_date,
        )
        self.repos.lots.update_split(rest)
        self.repos.lots.add_adjustment(
            BasisAdjustment(
                adjustment_id=0,
                lot_id=lot.lot_id,
                adjustment_date=split_date,
                reason=BasisAdjustmentReason.WASH_SALE,
                basis_delta=-moved_adjusted,
                quantity_delta=-quantity,
                holding_period_start_after=lot.holding_period_start,
                txn_id=loss.disposition.txn_id,
                note=f"{quantity} units split to {key}: they replace shares of "
                f"{loss.sold.lot_key} sold at a loss on {sale.isoformat()}",
            )
        )
        child_id = self.repos.lots.add(
            replace(
                lot,
                lot_id=0,
                lot_key=key,
                original_quantity=quantity,
                remaining_quantity=quantity,
                original_cost_basis=moved_original,
                adjusted_cost_basis=child_basis,
                allocated_fees=moved_fees,
                per_unit_price=child_basis / quantity,
                holding_period_start=start,
                status=LotStatus.OPEN,
                closed_date=None,
                adjustments=(),
            )
        )
        absorbed = (lot.holding_period_start - start).days
        self.repos.lots.add_adjustment(
            BasisAdjustment(
                adjustment_id=0,
                lot_id=child_id,
                adjustment_date=split_date,
                reason=BasisAdjustmentReason.WASH_SALE,
                basis_delta=deferral,
                holding_period_start_after=start,
                txn_id=loss.disposition.txn_id,
                note=(
                    f"loss of {part} on {loss.sold.lot_key} sold {sale.isoformat()} "
                    + (
                        "permanently disallowed: a sheltered replacement"
                        if sheltered
                        else f"disallowed and added to basis; holding period "
                        f"includes {absorbed} days of the shares sold"
                    )
                ),
            )
        )
        self._record(
            txn,
            loss,
            WashSale(
                disposition_id=loss.disposition.disposition_id,
                txn_id=loss.disposition.txn_id,
                account_id=loss.disposition.account_id,
                sold_lot_key=loss.sold.lot_key,
                sale_date=sale,
                replacement_lot_key=key,
                replacement_date=lot.open_date,
                quantity=quantity,
                disallowed=part,
                status=status,
                days_absorbed=absorbed,
            ),
            replacement_account,
        )
        return rest

    def _report_manual(
        self, txn: Transaction, loss: _Loss, lot: Lot, quantity: Decimal
    ) -> None:
        """Replacement shares already disposed of: reported, not applied (answer 1)."""
        part = self._disallow(loss, quantity)
        account = self.repos.accounts.get(lot.account_id)
        assert account is not None
        self._record(
            txn,
            loss,
            WashSale(
                disposition_id=loss.disposition.disposition_id,
                txn_id=loss.disposition.txn_id,
                account_id=loss.disposition.account_id,
                sold_lot_key=loss.sold.lot_key,
                sale_date=loss.disposition.disposition_date,
                replacement_lot_key=lot.lot_key,
                replacement_date=lot.open_date,
                quantity=quantity,
                disallowed=part,
                status=WashSaleStatus.MANUAL,
                days_absorbed=0,
            ),
            account,
        )

    def _record(
        self, txn: Transaction, loss: _Loss, wash: WashSale, replacement_account: Account
    ) -> None:
        """Store the pairing, restate the loss it disallows, and say so."""
        self.repos.wash_sales.add(wash)
        loss.matched += wash.quantity
        symbol = self._symbol(loss.disposition.instrument_id)
        sale = wash.sale_date.isoformat()
        if wash.status is WashSaleStatus.MANUAL:
            self.notices.append(
                Notice(
                    txn.txn_id,
                    f"{symbol}: {wash.quantity} units bought on "
                    f"{wash.replacement_date.isoformat()} in {replacement_account.name} "
                    f"fall in the wash-sale window of the loss realized on {sale}, but "
                    "were disposed of before it. Reported for manual handling, and tax "
                    f"year {wash.sale_date.year} is marked incomplete.",
                )
            )
            return

        stored = self.repos.lots.disposition(loss.disposition.disposition_id)
        assert stored is not None
        with money_context():
            deferred = quantize_money((stored.wash_sale_deferred or ZERO) + wash.disallowed)
        restated = replace(stored, wash_sale_deferred=deferred)
        self.repos.lots.set_wash_sale_deferred(stored.disposition_id, deferred)
        schedules = self.repos.accounts.rate_schedules(loss.account.account_id)
        self.repos.lots.replace_realized_gain(
            self.tax.estimate(restated, loss.account, schedules)
        )
        loss.disposition = restated

        if wash.status is WashSaleStatus.PERMANENT:
            message = (
                f"{symbol}: {wash.disallowed} of the loss realized on {sale} in "
                f"{loss.account.name} is permanently disallowed. The replacement "
                f"{wash.quantity} units were bought in {replacement_account.name}, a "
                "sheltered account (Rev. Rul. 2008-5, applied by portable to every "
                "sheltered account), and no basis is increased."
            )
        else:
            message = (
                f"{symbol}: a wash sale. {wash.disallowed} of the loss realized on {sale} "
                f"in {loss.account.name} is disallowed and added to the basis of "
                f"{wash.quantity} units bought on {wash.replacement_date.isoformat()} in "
                f"{replacement_account.name}."
            )
        if txn.trade_date.year > wash.sale_date.year:
            message += f" This restates tax year {wash.sale_date.year}."
        self.notices.append(Notice(txn.txn_id, message))

    # ── rules ────────────────────────────────────────────────────────────────

    def _loss(self, disposition: LotDisposition) -> _Loss | None:
        """The disposition as a loss a wash sale can disallow, or None."""
        if disposition.realized_gain >= 0 or disposition.quantity <= 0:
            return None
        sale = self.repos.transactions.get(disposition.txn_id)
        if sale is None or sale.txn_type not in LOSS_SALE_TYPES:
            return None
        account = self.repos.accounts.get(disposition.account_id)
        if account is None or not account.is_taxable:
            return None
        sold = self.repos.lots.get(disposition.lot_id)
        if sold is None or sold.is_short:
            return None
        pairings = self.repos.wash_sales.for_disposition(disposition.disposition_id)
        matched = sum((w.quantity for w in pairings), Decimal(0))
        return _Loss(disposition=disposition, sold=sold, account=account, matched=matched)

    def _replaces(self, lot: Lot, loss: _Loss) -> bool:
        """Whether *lot* can be matched automatically against *loss*."""
        if lot.is_short or "~" in lot.lot_key:
            return False
        if lot.basis_source is not BasisSource.DERIVED:
            return False
        if lot.open_txn_id == loss.sold.open_txn_id:
            return False  # one purchase: the shares sold and the shares kept
        opening = self.repos.transactions.get(lot.open_txn_id)
        return opening is not None and opening.txn_type in REPLACEMENT_TYPES

    def _identical(self, instrument_id: int) -> set[int]:
        """The instrument, and those declared substantially identical to it."""
        return {instrument_id, *self.repos.equivalences.equivalents_of(instrument_id)}

    def _ledger_order(self, loss: _Loss) -> tuple[object, ...]:
        sale = self.repos.transactions.get(loss.disposition.txn_id)
        assert sale is not None
        return (
            sale.trade_date,
            sale.seq,
            sale.txn_id,
            loss.sold.open_date,
            loss.sold.open_txn_id,
            loss.sold.lot_key,
        )

    @staticmethod
    def _disallow(loss: _Loss, quantity: Decimal) -> Decimal:
        """The loss on *quantity* more of the sold shares, to the cent.

        Cumulative, so a disposition matched in parts disallows exactly its
        whole loss once fully matched, with no cent lost or invented.
        """
        whole = -loss.disposition.realized_gain
        sold = loss.disposition.quantity
        with money_context():
            before = quantize_money(whole * loss.matched / sold)
            after = quantize_money(whole * (loss.matched + quantity) / sold)
            return after - before

    def _symbol(self, instrument_id: int) -> str:
        instrument = self.repos.instruments.get(instrument_id)
        return instrument.symbol if instrument is not None else str(instrument_id)


# ── candidates (decision 8) ──────────────────────────────────────────────────


class CandidateReason(enum.StrEnum):
    """Why an acquisition in a loss's window was not matched automatically."""

    #: Another instrument, not declared substantially identical.
    DIFFERENT_INSTRUMENT = "different_instrument"
    #: An option on the loss's instrument: a contract to acquire (§1091(a)).
    OPTION_ON_UNDERLYING = "option_on_underlying"
    #: The same instrument, but transferred in or seeded with a custodian's
    #: basis, which may already carry the custodian's adjustment (decision 7).
    HELD_BACK = "in_kind_or_custodian_basis"


@dataclass(frozen=True, slots=True)
class Candidate:
    """An acquisition in a loss's window that ADR 0037 does not match itself.

    Proposed, never applied: whether it is a replacement is the owner's call,
    made with `pt instrument equivalent` or by hand.
    """

    disposition: LotDisposition
    sold: Lot
    unmatched: Decimal
    lot: Lot
    reason: CandidateReason


def candidates(repos: Repositories, *, year: int) -> list[Candidate]:
    """Every acquisition in the window of a loss realized in *year*, not matched.

    Across every account. Complete rather than filtered by asset class, which
    is optional free text and would hide whatever has none (ADR 0037, answer
    4): the caller filters for reading.
    """
    matcher = WashSaleMatcher(repos, TaxEngine())
    found: list[Candidate] = []
    for disposition in repos.lots.dispositions(start=date(year, 1, 1), end=date(year, 12, 31)):
        loss = matcher._loss(disposition)
        if loss is None:
            continue
        identical = matcher._identical(disposition.instrument_id)
        sale = disposition.disposition_date
        for lot in repos.lots.acquired_between(None, sale - WINDOW, sale + WINDOW):
            if lot.open_txn_id == loss.sold.open_txn_id or "~" in lot.lot_key:
                continue
            reason = _candidate_reason(repos, matcher, lot, loss, identical)
            if reason is not None:
                found.append(
                    Candidate(
                        disposition=disposition,
                        sold=loss.sold,
                        unmatched=loss.open_quantity,
                        lot=lot,
                        reason=reason,
                    )
                )
    return found


def _candidate_reason(
    repos: Repositories,
    matcher: WashSaleMatcher,
    lot: Lot,
    loss: _Loss,
    identical: set[int],
) -> CandidateReason | None:
    opening = repos.transactions.get(lot.open_txn_id)
    if opening is None:
        return None
    if lot.instrument_id in identical:
        if matcher._replaces(lot, loss):
            return None  # matched automatically: not a candidate
        if opening.txn_type is TransactionType.TRANSFER_IN or (
            lot.basis_source is not BasisSource.DERIVED
        ):
            return CandidateReason.HELD_BACK
        return None  # derived by a corporate action: not an acquisition
    if opening.txn_type not in REPLACEMENT_TYPES:
        return None
    instrument = repos.instruments.get(lot.instrument_id)
    if (
        instrument is not None
        and instrument.option is not None
        and instrument.option.underlier_instrument_id in identical
    ):
        return CandidateReason.OPTION_ON_UNDERLYING
    return CandidateReason.DIFFERENT_INSTRUMENT


# ── custodian-stated bases (decision 7) ──────────────────────────────────────

#: Rungs anchored to the custodian's stated present basis (ADR 0017), which
#: already carries whatever wash-sale adjustments the custodian made.
_ANCHORED: Final = frozenset({BasisSource.RECONSTRUCTED, BasisSource.ESTIMATED})


def custodian_overlaps(repos: Repositories) -> tuple[str, ...]:
    """Blocks whose seeded basis may already contain a deferral replay applied.

    ADR 0037, decision 7. A reconstructed or estimated block is anchored to
    the custodian's present basis, and a custodian adjusts for the wash sales
    it can see: one account, one security. Where such a block shares an
    account and an instrument with a replacement replay deferred a loss into,
    the custodian's figure may already include that deferral, and it would
    then be counted twice. Which it is, the custodian's lot detail says;
    nothing here nets it.
    """
    found: list[str] = []
    seen: set[tuple[str, str]] = set()
    for wash in repos.wash_sales.all():
        if wash.status is not WashSaleStatus.DEFERRED:
            continue
        replacement = repos.lots.by_key(wash.replacement_lot_key)
        if replacement is None:
            continue
        for block in repos.lots.acquired_between(
            {replacement.instrument_id}, date.min, date.max
        ):
            if block.account_id != replacement.account_id:
                continue
            if block.basis_source not in _ANCHORED:
                continue
            if (block.lot_key, replacement.lot_key) in seen:
                continue
            seen.add((block.lot_key, replacement.lot_key))
            account = repos.accounts.get(block.account_id)
            instrument = repos.instruments.get(block.instrument_id)
            found.append(
                f"{instrument.symbol if instrument else block.instrument_id} in "
                f"{account.name if account else block.account_id}: the "
                f"{block.basis_source} block {block.lot_key} is anchored to the "
                "custodian's stated basis, which may already include the "
                f"{wash.disallowed} deferred into {replacement.lot_key} for the loss "
                f"of {wash.sale_date.isoformat()}. If the custodian's lot detail "
                "shows it, correct the seeded row with a reversing entry and a new "
                "one (ADR 0037, decision 7)."
            )
    return tuple(found)
