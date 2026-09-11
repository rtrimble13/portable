"""Turning a custodian's records into a reviewable batch. ADR 0012, stage one.

The last piece of the pipeline. `pt import inspect` says what a custodian's
exports support; `pt import reconstruct` derives what was held before the
history begins; this puts the two together into the batch a person reads and
`pt import batch` commits.

A batch built here has two halves, and they answer different questions:

**The seed.** One `transfer_in` per position held at the cutover, dated at the
cutover, carrying the basis the reconstruction recovered and the rung it sits
on (ADR 0015, ADR 0017). These exist because a position that predates every
available record has to enter the ledger somehow, and every other lot-creating
transaction type moves cash — a back-dated `buy` would invent an outflow, which
would need an invented deposit, which is an external cash flow, which is how a
track record gets silently rewritten.

**The history.** One row per custodian transaction after the cutover, with its
activity already resolved by the adapter.

Nothing here writes. The output is a file, and the review of that file is the
point: the ledger is append-only, so the cheap place to catch a mistake is
before it becomes a reversing entry that stays visible for the life of the
portfolio.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from portable_core.domain.enums import ReliefMethod, TransactionType
from portable_core.domain.import_records import MappedTransaction, TransactionRecord
from portable_core.errors import DataUnavailableError, ValidationError
from portable_core.errors.kinds import E_IMPORT_SOURCE_INVALID, E_PRICE_MISSING
from portable_core.services.import_batch import (
    SUPPORTED_TYPES,
    BatchRow,
    BatchSource,
    ImportBatch,
)
from portable_core.services.reconstruction import (
    CutoverCash,
    CutoverPosition,
    Reconstruction,
)

__all__ = [
    "ExtractResult",
    "InLedger",
    "build_batch",
    "build_incremental_batch",
    "cutover_prices_needed",
]

ZERO: Final = Decimal("0")

#: How many characters of the digest an `external_ref` carries. ADR 0012.
_REF_WIDTH: Final = 16

#: Whether the ledger already holds ``(account, external_ref)``. Supplied by
#: the command, which has the portfolio open; the service stays free of SQL.
InLedger = Callable[[str, str], bool]


def _nothing_in_ledger(_account: str, _external_ref: str) -> bool:
    return False


#: Trades that consume lots, and therefore need a relief method stated.
_CLOSING: Final = frozenset(
    {TransactionType.SELL, TransactionType.BUY_TO_COVER, TransactionType.TRANSFER_OUT}
)


@dataclass(frozen=True, slots=True)
class ExtractResult:
    """The batch, and what building it decided."""

    batch: ImportBatch
    seeded: int
    appended: int
    skipped: int
    dropped: int
    #: Transaction types the history contains that format version 1 cannot
    #: carry -- corporate actions, the options lifecycle. Recorded as `skip`
    #: rows so the batch shows them, and named here so the operator knows to
    #: record them by hand rather than discovering the gap in a reconciliation.
    unsupported: tuple[str, ...] = ()


def cutover_prices_needed(reconstruction: Reconstruction) -> tuple[str, ...]:
    """Instruments whose cutover market value the batch will require.

    Every seeded position needs one. The value is **not** the basis: it is what
    the shares were worth on the cutover date, and it is the flow amount that
    establishes the account's beginning market value (ADR 0015). Substituting
    the basis for it would make the first period's return wrong by the whole
    unrealized gain at cutover, which is the single most common way this is got
    wrong elsewhere.

    Asked for separately so a caller can gather the prices, or report what is
    missing, before anything is built.
    """
    return tuple(sorted({p.identifier for p in reconstruction.positions}))


def build_batch(
    *,
    broker: str,
    reconstruction: Reconstruction,
    mapped: Sequence[MappedTransaction],
    cutover_prices: Mapping[str, Decimal],
    files: Sequence[tuple[str, str]] = (),
    capabilities: Sequence[str] = (),
    in_ledger: InLedger = _nothing_in_ledger,
    price_sources: Mapping[str, str] | None = None,
) -> ExtractResult:
    """Build the reviewable batch. Writes nothing.

    Args:
        reconstruction: the opening state, from `reconstruct`.
        mapped: every custodian transaction with its activity resolved by the
            adapter. Rows on or before the cutover are dropped -- the seed
            already accounts for them, and appending them too would double the
            position.
        cutover_prices: market value per unit on the cutover date, per
            instrument. Required, never defaulted: see
            :func:`cutover_prices_needed`.
        in_ledger: whether a history row's reference is already recorded in
            its account. Such a row is written as a `skip` naming the reason,
            so an overlapping export shows its overlap in the file under
            review rather than refusing at commit (ADR 0012).
        price_sources: where each cutover price came from, in words, for the
            seed row's source. A price the portfolio's own table supplied
            needs no note; one read off the custodian's same-day receipt is a
            different provenance and the reviewer has to be able to see it.

    Raises:
        DataUnavailableError: when a seeded position has no cutover price. Exit
            5, not 4: the request is well formed and the data is simply not in
            the file yet, which is a different thing for a caller to handle.
            Refusing
            is the point -- there is no honest substitute, and the two numbers
            this batch keeps apart are exactly the ones a substitute would
            conflate.
    """
    missing = [
        symbol
        for symbol in cutover_prices_needed(reconstruction)
        if cutover_prices.get(symbol) is None
    ]
    if missing:
        raise DataUnavailableError(
            f"no price on the cutover {reconstruction.cutover.isoformat()} for "
            + ", ".join(missing),
            code=E_PRICE_MISSING,
            remedy=(
                "Load prices for the cutover date (`pt price import`), or move the "
                "cutover to a date you have prices for. The transfer's value is the "
                "market value on that day and establishes the account's beginning "
                "market value -- the cost basis is a different number and cannot "
                "stand in for it."
            ),
            cutover=reconstruction.cutover.isoformat(),
            instruments=missing,
        )

    rows: list[BatchRow] = []
    # The accounted-for part of a block before its vanished part, so that FIFO
    # relieves the lots the custodian's own report says were sold first.
    for position in sorted(
        reconstruction.positions, key=lambda p: (p.account, p.identifier, p.part)
    ):
        rows.append(
            _seed_row(
                position,
                reconstruction.cutover,
                cutover_prices,
                len(rows) + 1,
                price_source=(price_sources or {}).get(position.identifier),
            )
        )
    for balance in reconstruction.cash:
        cash_row = _seed_cash_row(balance, reconstruction.cutover, len(rows) + 1)
        if cash_row is not None:
            rows.append(cash_row)
    seeded = len(rows)

    unsupported: set[str] = set()
    appended = skipped = dropped = 0
    history = _after(mapped, reconstruction.cutover)
    ordinals = _Ordinals()
    for entry in history:
        row = _history_row(entry, len(rows) + 1, unsupported, ordinals, in_ledger)
        rows.append(row)
        if row.action == "append":
            appended += 1
        elif row.action == "skip":
            skipped += 1
        else:
            dropped += 1

    # The span the batch actually covers: the cutover, where the seed sits,
    # through the last row it carries. Not the reconstruction's `as_of`, which
    # is the snapshot date and later than anything in here.
    dates = [reconstruction.cutover, *(e.record.trade_date for e in history)]
    period = (min(dates), max(dates))
    return ExtractResult(
        batch=ImportBatch(
            source=BatchSource(
                broker=broker,
                files=tuple(files),
                capabilities=tuple(capabilities),
                period=period,
            ),
            rows=tuple(rows),
        ),
        seeded=seeded,
        appended=appended,
        skipped=skipped,
        dropped=dropped,
        unsupported=tuple(sorted(unsupported)),
    )


def build_incremental_batch(
    *,
    broker: str,
    mapped: Sequence[MappedTransaction],
    inception: Mapping[str, date],
    in_ledger: InLedger,
    files: Sequence[tuple[str, str]] = (),
    capabilities: Sequence[str] = (),
) -> ExtractResult:
    """Build the batch for a periodic update to accounts already in the ledger.

    The other shape an extract takes, and the one every import after the first
    takes. There is no seed: the accounts already hold their opening positions,
    and seeding them again would double every one. What remains is the history,
    with two kinds of row set aside and shown:

    - a row **already recorded** -- the custodian's window overlaps the last
      export, which is ordinary and on purpose -- is a `skip` naming the
      reference the ledger already carries;
    - a row **on or before the account's first ledger date** is inside the
      seeded position from the initial extract, and appending it would count
      it twice. Also a `skip`, saying so.

    Args:
        inception: each account's first ledger date, from the portfolio. An
            account the export mentions that is not here is refused: an
            incremental extract has nothing to extend for it, and the initial
            extract is the command for that.
    """
    missing = sorted({m.record.account for m in mapped} - set(inception))
    if missing:
        raise ValidationError(
            "the export mentions "
            + ", ".join(missing)
            + ", which carries no ledger rows. An incremental extract extends a "
            "history that is already there; run the initial extract for an "
            "account that has none",
            code=E_IMPORT_SOURCE_INVALID,
            accounts=missing,
        )

    rows: list[BatchRow] = []
    unsupported: set[str] = set()
    appended = skipped = dropped = 0
    ordinals = _Ordinals()
    for entry in sorted(mapped, key=lambda m: m.record.trade_date):
        record = entry.record
        if record.trade_date <= inception[record.account]:
            row = BatchRow(
                index=len(rows) + 1,
                action="skip",
                rule="ledger:before-inception",
                source_row=dict(record.source_row),
                note=(
                    f"dated on or before {record.account}'s first ledger row "
                    f"({inception[record.account].isoformat()}), so it is inside the "
                    f"position seeded at the cutover and would be counted twice"
                ),
            )
        else:
            row = _history_row(entry, len(rows) + 1, unsupported, ordinals, in_ledger)
        rows.append(row)
        if row.action == "append":
            appended += 1
        elif row.action == "skip":
            skipped += 1
        else:
            dropped += 1

    dates = [e.record.trade_date for e in mapped]
    return ExtractResult(
        batch=ImportBatch(
            source=BatchSource(
                broker=broker,
                files=tuple(files),
                capabilities=tuple(capabilities),
                period=(min(dates), max(dates)) if dates else None,
            ),
            rows=tuple(rows),
        ),
        seeded=0,
        appended=appended,
        skipped=skipped,
        dropped=dropped,
        unsupported=tuple(sorted(unsupported)),
    )


# ── the seed ─────────────────────────────────────────────────────────────────


def _seed_row(
    position: CutoverPosition,
    cutover: date,
    prices: Mapping[str, Decimal],
    index: int,
    *,
    price_source: str | None = None,
) -> BatchRow:
    """One `transfer_in` for a position held before the ledger begins."""
    price = prices[position.identifier]
    provenance = {"price_source": price_source} if price_source else {}
    if position.part:
        provenance["part"] = position.part
    return BatchRow(
        index=index,
        action="append",
        rule=f"cutover:{position.basis_source.value} (ADR 0017)",
        # There is no custodian row behind a seed: it is derived from the
        # roll-back. The arithmetic is put here verbatim so a reviewer can
        # check it without re-running the reconstruction.
        source_row={
            "derived_by": "roll-back from the holdings snapshot",
            "cutover": cutover.isoformat(),
            "quantity_at_cutover": str(position.quantity),
            "added_after": str(position.added_after),
            "disposed_after": str(position.disposed_after),
            "still_held": str(position.still_held),
            "assumption": position.assumption or "",
            **provenance,
        },
        external_ref=_seed_ref(position, cutover),
        account=position.account,
        txn_type=TransactionType.TRANSFER_IN,
        trade_date=cutover,
        symbol=position.identifier,
        quantity=position.quantity,
        price=price,
        # The FLOW amount: market value on the cutover date. Not the basis.
        amount=price * position.quantity,
        original_basis=position.cost_basis,
        # The block's earliest acquisition where the custodian states one, and
        # absent otherwise. Absent, the lot dates at the cutover, which makes
        # holding-period character conservative by construction -- everything
        # seeded reads short-term until a year past it, the safe direction to
        # be wrong in (ADR 0018). Stated, it is the block's *earliest* date and
        # biases the other way, which is why the reconstruction enumerates the
        # dispositions within a year of the cutover for review (ADR 0017 §4).
        original_acquired_date=position.acquired,
        basis_source=position.basis_source,
        basis_assumption=position.assumption,
        note=f"seeded at the cutover {cutover.isoformat()}",
    )


def _seed_cash_row(balance: CutoverCash, cutover: date, index: int) -> BatchRow | None:
    """The cash an account already held when the ledger begins.

    Without this nothing reconciles. The positions seed and the history
    appends, so the account starts from zero cash and every purchase drives it
    negative -- by exactly the balance that was there at the cutover, which the
    reconciler reports as a break somebody then has to explain.

    Recorded as a `deposit` on the cutover date, which needs saying because the
    obvious objection is right in general: inventing an external flow is how a
    track record gets silently rewritten (ADR 0007), and it is precisely why
    positions get `transfer_in` rather than a back-dated `buy`.

    The difference is the date. ADR 0015 decides that **a flow on the account's
    opening date establishes the account's beginning market value rather than a
    flow into it** -- there is no prior period for capital to flow from, and
    treating it as a contribution would make the first period's return a
    division against a zero beginning value. The cutover *is* the reporting
    inception (ADR 0017 §1), so this is that case.

    That exception is stated and **not yet implemented**: it belongs to the
    return engine, which does not exist. Until it does no first-period return is
    computed, so nothing reads the classification. When it lands, this row is
    what it has to except -- which is why the rule names the ADR rather than
    hiding behind "opening balance".

    ``None`` where the balance is zero: a row asserting nothing was there is
    noise in a file somebody reads line by line.
    """
    if balance.amount == ZERO:
        return None
    outward = balance.amount < ZERO
    return BatchRow(
        index=index,
        action="append",
        rule=f"cutover:cash ({'margin' if outward else 'balance'}, ADR 0015)",
        source_row={
            "derived_by": "roll-back from the holdings snapshot",
            "cutover": cutover.isoformat(),
            "balance_at_cutover": str(balance.amount),
            "moved_after": str(balance.moved_after),
        },
        external_ref=_cash_ref(balance.account, cutover),
        account=balance.account,
        # A negative rolled-back balance is a margin loan that existed at the
        # cutover, not a contribution. A withdrawal keeps the sign honest:
        # `record_cash` takes the magnitude and reads the direction from the
        # type, and letting a negative amount mean "withdrawal" is exactly what
        # that service refuses.
        txn_type=(TransactionType.WITHDRAWAL if outward else TransactionType.DEPOSIT),
        trade_date=cutover,
        amount=abs(balance.amount),
        note=(
            f"cash held at the cutover {cutover.isoformat()}; establishes the "
            f"account's beginning value rather than a flow into the period "
            f"(ADR 0015)"
        ),
    )


def _cash_ref(account: str, cutover: date) -> str:
    """A deterministic reference for a seeded cash balance."""
    material = "|".join([account, cutover.isoformat(), "cutover-cash"])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:_REF_WIDTH]
    return f"cutover:{digest}"


def _seed_ref(position: CutoverPosition, cutover: date) -> str:
    """A deterministic reference for a seed row. ADR 0012's rule, adapted.

    There is no source row to hash, so the identity is the thing the seed
    *is*: this account, this instrument, at this cutover. Re-running the
    extract produces the same reference, so a second commit is refused as a
    duplicate rather than doubling the position -- which is the whole reason
    references are synthesized at all.
    """
    material = "|".join(
        [position.account, position.identifier, cutover.isoformat(), "cutover", position.part]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:_REF_WIDTH]
    return f"cutover:{digest}"


# ── the history ──────────────────────────────────────────────────────────────


def _after(mapped: Sequence[MappedTransaction], cutover: date) -> list[MappedTransaction]:
    """Rows the seed does not already account for.

    Everything on or before the cutover is already inside the rolled-back
    position, so appending it as well would count it twice. Dropped silently
    would be wrong; they are simply not in the batch, and the batch's period
    says where it begins.
    """
    return [m for m in mapped if m.record.trade_date > cutover]


class _Ordinals:
    """ADR 0012's ordinal: a row's index among otherwise-identical rows.

    Two identical dividends on one day are not hypothetical, and without an
    ordinal the second would collide with the first and be refused as a
    duplicate of it. Counted over the *identity material* rather than the
    batch position, so that the same source row gets the same reference in an
    initial extract and in every incremental one after it -- which is what
    lets an overlapping export be recognised as an overlap.
    """

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def next(self, material: str) -> int:
        ordinal = self._seen.get(material, 0)
        self._seen[material] = ordinal + 1
        return ordinal


def _history_row(
    entry: MappedTransaction,
    index: int,
    unsupported: set[str],
    ordinals: _Ordinals,
    in_ledger: InLedger,
) -> BatchRow:
    record = entry.record
    if entry.is_skipped:
        return BatchRow(
            index=index,
            action="skip",
            rule=entry.rule,
            source_row=dict(record.source_row),
            note=entry.reason,
        )

    assert entry.txn_type is not None
    if entry.txn_type not in SUPPORTED_TYPES:
        # Recorded rather than refused, and never silently dropped. Format
        # version 1 carries the types with a service behind them; a corporate
        # action needs position context a typed command gathers, and an
        # importer deriving basis by a second unreviewed route is the failure
        # `CLAUDE.md` invariant 10 describes. The operator records these by
        # hand, and the batch shows exactly which.
        unsupported.add(entry.txn_type.value)
        return BatchRow(
            index=index,
            action="skip",
            rule=f"unsupported:{entry.txn_type.value} (batch format version 1)",
            source_row=dict(record.source_row),
            note=(
                f"{entry.txn_type.value} cannot be carried by a batch; record it "
                f"with the typed command after committing this one"
            ),
        )

    external_ref = _history_ref(record, ordinals)
    if in_ledger(record.account, external_ref):
        # The overlap belongs in the artifact under review, not in a refusal
        # at commit and not in a `--skip-duplicates` flag that would hide it.
        return BatchRow(
            index=index,
            action="skip",
            rule="ledger:already-recorded",
            source_row=dict(record.source_row),
            external_ref=external_ref,
            account=record.account,
            note=f"{record.account} already carries a row with reference {external_ref}",
        )

    return BatchRow(
        index=index,
        action="append",
        rule=entry.rule,
        source_row=dict(record.source_row),
        external_ref=external_ref,
        account=record.account,
        txn_type=entry.txn_type,
        trade_date=record.trade_date,
        settlement_date=record.settlement_date,
        symbol=record.identifier or None,
        quantity=abs(record.quantity) if record.quantity is not None else None,
        price=_unit_price(record),
        # The batch states direction by type and carries a magnitude: the cash
        # the account moved, or -- where none moved -- what the event was worth.
        amount=_magnitude(record),
        fee_class=entry.fee_class,
        counter_account=entry.counter_account,
        taxes_withheld=entry.taxes_withheld,
        # ADR 0017 §2a. The reconstruction solved every seeded basis under an
        # assumed FIFO relief; the ledger must relieve the same way or a block
        # solved for FIFO gets relieved spec-ID and yields a basis the solve
        # never computed. Written into the batch rather than left to the
        # account default so it is visible and a reviewer can change it -- and
        # so that changing it here is understood to invalidate the solve.
        relief_method=(ReliefMethod.FIFO if entry.txn_type in _CLOSING else None),
        note=record.note,
    )


def _magnitude(record: TransactionRecord) -> Decimal | None:
    """The batch row's amount: cash moved, or the event's stated value."""
    if record.amount:
        return abs(record.amount)
    if record.value:
        return abs(record.value)
    return None


def _unit_price(record: TransactionRecord) -> Decimal | None:
    """Price per unit, where the row has both a quantity and an amount.

    Derived rather than read: custodians commonly state the trade's total and
    the share count and leave the price implied. Where the quantity is zero
    there is no price to state, and `None` says so rather than a zero saying
    the shares were free.
    """
    magnitude = _magnitude(record)
    if not record.quantity or magnitude is None:
        return None
    return magnitude / abs(record.quantity)


def _history_ref(record: TransactionRecord, ordinals: _Ordinals) -> str:
    """ADR 0012's synthesized identity, over the source row's raw text.

    Raw text and not the mapped values, deliberately: a change to the activity
    map must not change the identity of a row already committed, or a re-import
    after a mapping fix would duplicate everything it touched.

    Where the custodian supplies its own identifier (`TRANSACTION_ID`), that is
    used instead — it is a better key than any hash, and it survives the
    custodian re-exporting the same period in a different row order.
    """
    if record.external_id:
        return record.external_id
    material = "|".join(
        [
            record.account,
            record.trade_date.isoformat(),
            record.activity,
            *(f"{k}={v}" for k, v in sorted(record.source_row.items())),
        ]
    )
    material = f"{material}|{ordinals.next(material)}"
    return "row:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:_REF_WIDTH]
