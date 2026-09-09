"""Rolling a custodian's history back to the state before it begins. ADR 0017.

Most custodians will not hand over history to account inception -- a two-year
window is common and often not negotiable -- so a portfolio built from their
exports has to start somewhere the records do not. The cutover is the
transaction history's **first date**, and the position set on the day before it
is *derived, not read*: apply the history in reverse to the dated holdings
snapshot and what remains is what was held.

Three properties make this worth doing rather than cutting over at today's
snapshot and discarding the history:

**Quantities come back exactly.** Every quantity in the roll-back is arithmetic
on numbers the custodian stated. Nothing is assumed.

**The roll-back is also the completeness check.** A position that rolls back to a
negative holding proves the history is missing something -- most often a
corporate action -- which is what makes `CORPORATE_ACTIONS` *detectable* rather
than something to take on trust. Those are reported by instrument, not smoothed
over.

**Basis comes back for most of it, and the rest is labelled.** The solve is
anchored to the custodian's stated *present* basis, so a block that contributes
nothing to the present holding has no anchor and no equation -- under FIFO or any
other relief method. Those get `BasisSource.UNAVAILABLE` and no number, because
the failure this repository is organised against is not approximation; it is an
approximation that cannot be told apart from an exact figure.

This module computes and reports. It writes nothing: seeding the ledger with
`transfer_in` rows (ADR 0015) is a separate step with its own refusals, and
keeping the arithmetic separable is what makes it re-runnable -- the correct
response to finding a mapping error is to re-derive the cutover state and
rebuild, never to patch lots.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from portable_core.domain.enums import BasisSource
from portable_core.domain.import_records import HoldingRecord, TransactionRecord
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_IMPORT_SOURCE_INVALID

__all__ = [
    "CutoverCash",
    "CutoverPosition",
    "Reconstruction",
    "ReconstructionFinding",
    "UncertainDisposition",
    "reconstruct",
]

ZERO: Final = Decimal("0")

#: Quantity difference tolerated between the rolled-back holding and zero before
#: it is reported. Custodians routinely state a transaction quantity to two
#: decimals and the same instrument's holding to three, so a sub-share residue
#: is a rounding artefact of their own reporting rather than a missing row. It
#: is still surfaced as a finding -- tolerated is not the same as unremarked.
SUBSHARE: Final = Decimal("1")


@dataclass(frozen=True, slots=True)
class CutoverPosition:
    """One instrument held in one account on the day before the ledger begins."""

    account: str
    identifier: str
    #: The holding at the cutover, derived by roll-back. Exact.
    quantity: Decimal
    basis_source: BasisSource
    #: Total cost basis of the block. ``None`` under `UNAVAILABLE`, where the
    #: evidence supports no figure -- and `None` is not zero.
    cost_basis: Decimal | None
    #: The arithmetic that produced the basis, in words, so it can be
    #: re-derived and re-argued later rather than merely trusted.
    assumption: str | None
    #: Whether any of the block is still held today. False for a position
    #: liquidated since the cutover, which is the case with no anchor at all.
    still_held: bool
    added_after: Decimal
    disposed_after: Decimal

    @property
    def needs_cutover_price(self) -> bool:
        """`UNAVAILABLE` blocks are seeded at cutover market value.

        That value makes cash conservation close (invariant 4) and the position
        engine able to relieve a lot. It is **not** a basis claim, and nothing
        here invents it: the seeding step fetches it from price history, or
        refuses.
        """
        return self.basis_source is BasisSource.UNAVAILABLE


@dataclass(frozen=True, slots=True)
class CutoverCash:
    """One account's cash balance at the cutover, rolled back the same way.

    Reported because it is the reconciliation anchor's other half: quantities
    that reconcile and cash that does not is the signature of a sign error or a
    dropped row, and those are exactly the errors a share count cannot catch.
    """

    account: str
    amount: Decimal
    #: Cash the history moved after the cutover. `amount` is today's less this.
    moved_after: Decimal


@dataclass(frozen=True, slots=True)
class ReconstructionFinding:
    """Something the roll-back proves about the history, rather than about a
    position: a hole in the record, not a number to carry forward."""

    account: str
    identifier: str
    kind: str
    detail: str


@dataclass(frozen=True, slots=True)
class UncertainDisposition:
    """A sale within a year of the cutover, whose character rests on a date.

    ADR 0017 §4. Every seeded lot was acquired on or before the cutover, so a
    disposition **more than** a year after it is long-term whatever the seeded
    date says -- certain, not assumed. Inside that first year the character
    depends on the seeded acquisition date being right, and the date available
    is the block's *earliest* acquisition, which biases toward long-term.

    That is the wrong direction to be relaxed about, so these are enumerated
    individually rather than counted. The list is generated because it changes
    with the cutover date, and a copy written into a document would go stale
    silently.
    """

    account: str
    identifier: str
    trade_date: date
    quantity: Decimal
    days_after_cutover: int


@dataclass(frozen=True, slots=True)
class Reconstruction:
    """The proposed opening state, and everything qualifying it."""

    cutover: date
    as_of: date
    positions: tuple[CutoverPosition, ...]
    cash: tuple[CutoverCash, ...]
    #: Instruments the history opened after the cutover. Named rather than
    #: merely absent: "held nothing" and "was not looked at" differ.
    opened_after: tuple[tuple[str, str], ...]
    findings: tuple[ReconstructionFinding, ...]
    uncertain_dispositions: tuple[UncertainDisposition, ...]

    def by_source(self) -> dict[BasisSource, int]:
        tally: dict[BasisSource, int] = dict.fromkeys(BasisSource, 0)
        for position in self.positions:
            tally[position.basis_source] += 1
        return {source: count for source, count in tally.items() if count}

    @property
    def exact_basis_share(self) -> Decimal | None:
        """The share of reconstructed basis that is exact as an aggregate.

        `None` when no basis was recovered at all, which is not zero: zero would
        say the evidence supported nothing, and `None` says there was nothing to
        take a share of.
        """
        total = sum((p.cost_basis for p in self.positions if p.cost_basis is not None), ZERO)
        if total == ZERO:
            return None
        exact = sum(
            (
                p.cost_basis
                for p in self.positions
                if p.cost_basis is not None and p.basis_source is BasisSource.RECONSTRUCTED
            ),
            ZERO,
        )
        return exact / total


def reconstruct(
    holdings: Sequence[HoldingRecord],
    transactions: Sequence[TransactionRecord],
    *,
    cutover: date | None = None,
) -> Reconstruction:
    """Roll the history back from the snapshot to the state before it begins.

    ``cutover`` defaults to the day before the history's first trade date, which
    is ADR 0017 §1's definition and the only one the evidence supports. It is
    overridable because a later cutover is a legitimate choice -- it trades
    track record for precision -- and because the tests need to exercise both.
    """
    if not transactions:
        raise ValidationError(
            "there is no transaction history to roll back. The reconstruction "
            "derives the opening state from the history applied in reverse; "
            "with no history there is nothing to reverse and nothing to check "
            "the snapshot against",
            code=E_IMPORT_SOURCE_INVALID,
        )
    if not holdings:
        raise ValidationError(
            "there is no holdings snapshot to roll back from. The snapshot is "
            "the anchor: without it the ledger has never been checked against "
            "an independently produced position statement (ADR 0018 §1)",
            code=E_IMPORT_SOURCE_INVALID,
        )

    first = min(t.trade_date for t in transactions)
    boundary = cutover if cutover is not None else first - timedelta(days=1)
    if boundary >= max(t.trade_date for t in transactions):
        raise ValidationError(
            f"the cutover {boundary.isoformat()} is at or after the history's "
            f"last trade date. Everything would roll back and nothing would be "
            f"left to reconcile against",
            code=E_IMPORT_SOURCE_INVALID,
            cutover=boundary.isoformat(),
        )

    as_of = max(h.as_of for h in holdings)
    after = [t for t in transactions if t.trade_date > boundary]

    positions, opened, findings, held_keys = _positions(holdings, after)
    return Reconstruction(
        cutover=boundary,
        as_of=as_of,
        positions=positions,
        cash=_cash(holdings, after),
        opened_after=opened,
        findings=findings,
        uncertain_dispositions=_uncertain(after, boundary, held_keys),
    )


# ── positions ────────────────────────────────────────────────────────────────


def _positions(
    holdings: Sequence[HoldingRecord], after: Sequence[TransactionRecord]
) -> tuple[
    tuple[CutoverPosition, ...],
    tuple[tuple[str, str], ...],
    tuple[ReconstructionFinding, ...],
    frozenset[tuple[str, str]],
]:
    """Roll every (account, instrument) pair back and classify its basis."""
    today = {
        (h.account, h.identifier): h
        for h in holdings
        if not h.is_cash_equivalent  # ADR 0013: a sweep vehicle is cash
    }
    moves: dict[tuple[str, str], list[TransactionRecord]] = {}
    for txn in after:
        if txn.identifier is None or txn.quantity is None:
            continue  # a cash event: no position to roll back
        moves.setdefault((txn.account, txn.identifier), []).append(txn)

    positions: list[CutoverPosition] = []
    opened: list[tuple[str, str]] = []
    findings: list[ReconstructionFinding] = []

    for key in sorted(set(today) | set(moves)):
        account, identifier = key
        holding = today.get(key)
        rows = sorted(moves.get(key, []), key=lambda t: t.trade_date)

        now = holding.quantity if holding is not None else ZERO
        added = sum((t.quantity for t in rows if t.quantity and t.quantity > 0), ZERO)
        disposed = -sum((t.quantity for t in rows if t.quantity and t.quantity < 0), ZERO)
        at_cutover = now - added + disposed

        if at_cutover <= ZERO:
            if at_cutover < -SUBSHARE:
                # The completeness check. A holding that rolls back below zero
                # cannot be a rounding artefact: the history is missing an event
                # that changed the quantity, and a split is the usual candidate.
                findings.append(
                    ReconstructionFinding(
                        account=account,
                        identifier=identifier,
                        kind="negative_rollback",
                        detail=(
                            f"rolls back to {at_cutover}: holding {now} today, "
                            f"{added} added and {disposed} disposed since the "
                            f"cutover. The history is missing an event that "
                            f"changed the quantity — most often a corporate "
                            f"action, so CORPORATE_ACTIONS is not supported "
                            f"here whatever the source declares"
                        ),
                    )
                )
            elif at_cutover < ZERO:
                findings.append(
                    ReconstructionFinding(
                        account=account,
                        identifier=identifier,
                        kind="subshare_residue",
                        detail=(
                            f"rolls back to {at_cutover}, within a share of "
                            f"zero: the custodian states transaction quantities "
                            f"and holdings to different precisions. Treated as "
                            f"opened after the cutover"
                        ),
                    )
                )
            opened.append(key)
            continue

        positions.append(
            _classify(
                account=account,
                identifier=identifier,
                at_cutover=at_cutover,
                added=added,
                disposed=disposed,
                holding=holding,
                rows=rows,
                findings=findings,
            )
        )

    return (
        tuple(positions),
        tuple(opened),
        tuple(findings),
        frozenset((p.account, p.identifier) for p in positions),
    )


def _classify(
    *,
    account: str,
    identifier: str,
    at_cutover: Decimal,
    added: Decimal,
    disposed: Decimal,
    holding: HoldingRecord | None,
    rows: Sequence[TransactionRecord],
    findings: list[ReconstructionFinding],
) -> CutoverPosition:
    """Which rung of the ladder this block's basis sits on, and why.

    One formula serves the top two rungs. The custodian's present basis is
    ``surviving_block * unit_cost + cost of every surviving addition``, so the
    block's unit cost is what is left when the additions are taken out and the
    remainder is divided by what survives. With no disposals the whole block
    survives and it degenerates to "today's basis less what was added since".
    """
    still_held = holding is not None and holding.quantity > ZERO
    surviving = at_cutover - disposed  # FIFO: disposals consume the block first
    added_cost = _cost_of_additions(rows)

    if not still_held or surviving <= ZERO:
        # No anchor. Today's basis constrains the block only through what
        # survives of it; where nothing survives there is no equation to solve,
        # under FIFO or any other relief method. ADR 0017 §2a: no assumption
        # reaches this case, so none is recorded as if it had.
        why = (
            "the position was fully liquidated after the cutover"
            if not still_held
            else f"{disposed} of the {at_cutover}-share block was disposed of, "
            f"consuming it entirely under FIFO"
        )
        return CutoverPosition(
            account=account,
            identifier=identifier,
            quantity=at_cutover,
            basis_source=BasisSource.UNAVAILABLE,
            cost_basis=None,
            assumption=(
                f"{why}, so nothing of it survives to anchor a solve. Its basis "
                f"is not recoverable from the available evidence; the lot is "
                f"seeded at cutover market value so the arithmetic closes, and "
                f"that value is not a basis claim (ADR 0017 §2b)"
            ),
            still_held=still_held,
            added_after=added,
            disposed_after=disposed,
        )

    if holding is None or holding.cost_basis is None:
        # COST_BASIS was not declared, or this line carried none. Refusing here
        # would decline to build the portfolio over a capability ADR 0018
        # already says is optional; the honest outcome is the same lot with no
        # basis claim attached to it.
        return CutoverPosition(
            account=account,
            identifier=identifier,
            quantity=at_cutover,
            basis_source=BasisSource.UNAVAILABLE,
            cost_basis=None,
            assumption=(
                "the holdings snapshot states no cost basis for this position, "
                "so there is nothing to anchor the solve to (ImportCapability."
                "COST_BASIS)"
            ),
            still_held=True,
            added_after=added,
            disposed_after=disposed,
        )

    block_basis = (holding.cost_basis - added_cost) / surviving * at_cutover
    if block_basis < ZERO:
        # Arithmetically possible, financially not: it means the additions cost
        # more than the whole present basis, so one of the two numbers is wrong.
        # Reported rather than clamped -- a clamp would produce a plausible
        # figure out of a contradiction.
        findings.append(
            ReconstructionFinding(
                account=account,
                identifier=identifier,
                kind="negative_basis",
                detail=(
                    f"solves to a negative cutover basis ({block_basis}): the "
                    f"{added_cost} added since the cutover exceeds the "
                    f"{holding.cost_basis} the custodian states today. Either "
                    f"an activity is mapped with the wrong cash sign or the "
                    f"snapshot and the history disagree"
                ),
            )
        )
        return CutoverPosition(
            account=account,
            identifier=identifier,
            quantity=at_cutover,
            basis_source=BasisSource.UNAVAILABLE,
            cost_basis=None,
            assumption=(
                "the solve produced a negative basis, which is a contradiction "
                "in the inputs rather than a small number"
            ),
            still_held=True,
            added_after=added,
            disposed_after=disposed,
        )

    if disposed == ZERO:
        return CutoverPosition(
            account=account,
            identifier=identifier,
            quantity=at_cutover,
            basis_source=BasisSource.RECONSTRUCTED,
            cost_basis=block_basis,
            assumption=(
                f"untouched since the cutover: the custodian's stated basis "
                f"{holding.cost_basis} less {added_cost} added since. Exact as "
                f"an aggregate, averaged within the block — so specific "
                f"identification inside it is not available"
            ),
            still_held=True,
            added_after=added,
            disposed_after=disposed,
        )

    return CutoverPosition(
        account=account,
        identifier=identifier,
        quantity=at_cutover,
        basis_source=BasisSource.ESTIMATED,
        cost_basis=block_basis,
        assumption=(
            f"FIFO assumed: the {disposed} disposed of since the cutover came "
            f"out of the {at_cutover}-share block first, leaving {surviving} of "
            f"it to anchor the solve against the custodian's stated basis "
            f"{holding.cost_basis} less {added_cost} added since. Nothing in "
            f"the exports states the custodian's actual relief method"
        ),
        still_held=True,
        added_after=added,
        disposed_after=disposed,
    )


def _cost_of_additions(rows: Iterable[TransactionRecord]) -> Decimal:
    """What was paid for everything added after the cutover.

    The cash effect of an addition is negative (money out), so its cost is the
    magnitude. A row that adds units without moving cash — an in-kind transfer
    recorded in the history — contributes nothing here, which is right for the
    solve and wrong as a basis; that is the custodian's gap, not an arithmetic
    one, and it surfaces as a negative-basis finding if it matters.
    """
    return sum(
        (-t.amount for t in rows if t.quantity and t.quantity > ZERO and t.amount < ZERO),
        ZERO,
    )


# ── cash ─────────────────────────────────────────────────────────────────────


def _cash(
    holdings: Sequence[HoldingRecord], after: Sequence[TransactionRecord]
) -> tuple[CutoverCash, ...]:
    """Today's cash less everything the history moved since the cutover.

    A sweep line's market value is its balance. Where the snapshot states no
    market value the quantity is used instead, which is right for a fund priced
    at par and is the only reading available -- ADR 0013's point is that the
    sweep *is* cash, and a cash line the snapshot values differently from its
    unit count is a contradiction the reconciliation will surface.
    """
    today: dict[str, Decimal] = {}
    for holding in holdings:
        today.setdefault(holding.account, ZERO)
        if holding.is_cash_equivalent:
            balance = (
                holding.market_value if holding.market_value is not None else holding.quantity
            )
            today[holding.account] += balance

    moved: dict[str, Decimal] = {}
    for txn in after:
        moved[txn.account] = moved.get(txn.account, ZERO) + txn.amount

    return tuple(
        CutoverCash(
            account=account,
            amount=today.get(account, ZERO) - moved.get(account, ZERO),
            moved_after=moved.get(account, ZERO),
        )
        for account in sorted(set(today) | set(moved))
    )


# ── holding-period certainty ─────────────────────────────────────────────────


def _uncertain(
    after: Sequence[TransactionRecord],
    cutover: date,
    seeded: frozenset[tuple[str, str]],
) -> tuple[UncertainDisposition, ...]:
    """Dispositions inside the first year after the cutover. ADR 0017 §4.

    Only of *seeded* positions: a disposition of something bought after the
    cutover has a real acquisition date in the ledger and its character is
    exact.
    """
    horizon = cutover + timedelta(days=365)
    return tuple(
        UncertainDisposition(
            account=txn.account,
            identifier=txn.identifier or "",
            trade_date=txn.trade_date,
            quantity=-txn.quantity if txn.quantity else ZERO,
            days_after_cutover=(txn.trade_date - cutover).days,
        )
        for txn in sorted(after, key=lambda t: (t.trade_date, t.account, t.identifier or ""))
        if txn.quantity is not None
        and txn.quantity < ZERO
        and txn.trade_date <= horizon
        and (txn.account, txn.identifier or "") in seeded
    )
