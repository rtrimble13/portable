"""What a lot designation written before schema 10 means. ADR 0036.

Before schema 10 a specific-identification row recorded its lots by id:
``lot_selection = "2:10"``. A lot id is a derived table's surrogate key, and
every rebuild renumbers lots, so a later back-dated lot-opening row, or a
reversal, re-pointed the row at another lot without a word. Rows written now
name lots by lineage key and need nothing here.

For each old row this recovers two readings and records them, by key, in
``lot_designation``:

* **as written** -- the lots the ids named when the row was appended. Stored
  state then equalled a replay of every row appended before it (ADR 0016), so
  replaying exactly those rows numbers the lots as the writer saw them.
* **as read** -- the lots the ids name when the whole ledger is replayed, which
  is how every report read them until now.

Where they agree the meaning is recorded. Where they differ, or the first
cannot be established, the row is pending, and replay refuses it until the
owner decides. Nothing here edits the ledger.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from portable_core.domain.enums import DesignationStatus, TransactionSource, TransactionType
from portable_core.domain.models import Lot, LotDesignation, Transaction
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_LOT_DESIGNATION_UNRESOLVED, E_USAGE
from portable_core.persistence.connection import scratch_savepoint
from portable_core.persistence.repositories import Repositories
from portable_core.services.lots import (
    format_keyed_selection,
    is_keyed,
    parse_lot_selection,
    resolve_keyed_selection,
)
from portable_core.services.replay import ReplayEngine

__all__ = [
    "Recovered",
    "Recovery",
    "gains_by_reading",
    "old_designations",
    "recover",
    "resolve",
    "unrecorded",
]

ZERO = Decimal("0.00")

#: Why a batch import's designation cannot be recovered. A batch applies its
#: rows one by one and rebuilds once at the end, so when its rows sorted before
#: rows already in the file, it designated against a numbering that no replay
#: of the ledger reproduces.
_INTERLEAVED = (
    "written by a batch import whose rows sort before rows already in the file; "
    "the numbering it designated against cannot be replayed"
)


@dataclass(frozen=True, slots=True)
class Recovered:
    """One old designation, with what was recorded for it."""

    txn_id: int
    trade_date: date
    spec: str
    status: DesignationStatus
    as_written: str | None
    as_read: str | None
    note: str | None


@dataclass(frozen=True, slots=True)
class Recovery:
    """What one pass of :func:`recover` recorded."""

    rows: tuple[Recovered, ...]

    @property
    def agreed(self) -> int:
        return sum(1 for r in self.rows if r.status is DesignationStatus.AGREED)

    @property
    def pending(self) -> tuple[Recovered, ...]:
        return tuple(r for r in self.rows if r.status is DesignationStatus.PENDING)


@dataclass(frozen=True, slots=True)
class _Reading:
    keyed: str | None
    problem: str | None


def _order(txn: Transaction) -> tuple[date, int, int]:
    """Ledger order, the replay contract (ADR 0010)."""
    return (txn.trade_date, txn.seq, txn.txn_id)


def old_designations(ledger: Iterable[Transaction]) -> list[Transaction]:
    """Rows whose designation names lot ids, in the order they were appended."""
    return sorted(
        (t for t in ledger if t.lot_selection and not is_keyed(t.lot_selection)),
        key=lambda t: t.txn_id,
    )


def unrecorded(repos: Repositories) -> list[Transaction]:
    """Old designations still applied by replay with no meaning recorded.

    A row a reversal netted out is never replayed, so it needs none.
    """
    ledger = repos.transactions.in_ledger_order()
    reversed_ids = {
        t.reverses_txn_id
        for t in ledger
        if t.txn_type is TransactionType.REVERSAL and t.reverses_txn_id
    }
    return [
        t
        for t in old_designations(ledger)
        if t.txn_id not in reversed_ids and repos.designations.get(t.txn_id) is None
    ]


def _read(row: Transaction, open_lots: list[Lot]) -> _Reading:
    """The lots *row*'s ids name among *open_lots*, by key."""
    assert row.lot_selection is not None
    by_id = {lot.lot_id: lot for lot in open_lots}
    keyed: dict[str, Decimal] = {}
    for lot_id, quantity in parse_lot_selection(row.lot_selection).items():
        lot = by_id.get(lot_id)
        if lot is None:
            return _Reading(
                None, f"lot {lot_id} is not an open lot of this instrument in this account"
            )
        # A unit count, not money: summed from a plain zero, so 10 stays 10.
        keyed[lot.lot_key] = keyed.get(lot.lot_key, Decimal(0)) + quantity
    return _Reading(format_keyed_selection(keyed), None)


class _ByNumber:
    """Reads an old designation by the ids as this replay numbers them.

    The behaviour before schema 10. Used only to recover readings, never to
    derive state that is kept.
    """

    def __init__(self, wanted: set[int]) -> None:
        self.wanted = wanted
        self.readings: dict[int, _Reading] = {}

    def __call__(self, txn: Transaction, open_lots: list[Lot]) -> dict[int, Decimal]:
        assert txn.lot_selection is not None
        if txn.txn_id in self.wanted:
            self.readings[txn.txn_id] = _read(txn, open_lots)
        return parse_lot_selection(txn.lot_selection)


def _disturbed(row: Transaction, ledger: list[Transaction]) -> bool:
    """Whether a row appended after *row* changed the lots replayed before it.

    If none did, the lots before *row* are created in the same order whether
    or not the later rows are replayed, so the whole-ledger reading is the
    reading as written. A row with no instrument touches no lot.
    """
    order = {t.txn_id: _order(t) for t in ledger}
    mine = order[row.txn_id]
    for later in ledger:
        if later.txn_id <= row.txn_id or later.instrument_id is None:
            continue
        if order[later.txn_id] < mine:
            return True
        reverses = later.reverses_txn_id
        if (
            later.txn_type is TransactionType.REVERSAL
            and reverses in order
            and order[reverses] < mine
        ):
            return True
    return False


def _interleaved_import(row: Transaction, ledger: list[Transaction]) -> bool:
    """Whether *row* came from a batch whose rows sort before earlier rows."""
    if row.source is not TransactionSource.IMPORT:
        return False
    mine = _order(row)
    return any(
        t.txn_id < row.txn_id and t.instrument_id is not None and _order(t) > mine
        for t in ledger
    )


def _as_written(
    repos: Repositories, row: Transaction, ledger: list[Transaction], read: _Reading | None
) -> _Reading:
    if _interleaved_import(row, ledger):
        return _Reading(None, _INTERLEAVED)
    if read is not None and read.keyed is not None and not _disturbed(row, ledger):
        return read
    # Replay exactly the rows that had been appended, and read the ids against
    # the lots that replay numbered: the state the writer was looking at.
    appended = [t for t in ledger if t.txn_id < row.txn_id]
    assert row.instrument_id is not None
    with scratch_savepoint(repos.con):
        ReplayEngine(repos, legacy_reader=_ByNumber(set())).replay_rows(appended)
        return _read(row, repos.lots.open_lots(row.account_id, row.instrument_id))


def recover(repos: Repositories) -> Recovery:
    """Record what every unrecorded old designation means.

    Writes ``lot_designation`` and nothing else. The caller rebuilds, inside
    the same transaction, so derived state reads the recorded meanings.
    """
    targets = unrecorded(repos)
    if not targets:
        return Recovery(())
    ledger = repos.transactions.in_ledger_order()

    # How every report read these rows until now: one replay of the whole
    # ledger, numbering lots as replay always has.
    by_number = _ByNumber({t.txn_id for t in targets})
    with scratch_savepoint(repos.con):
        ReplayEngine(repos, legacy_reader=by_number).rebuild()
    as_read = by_number.readings

    rows: list[Recovered] = []
    for row in targets:
        read = as_read.get(row.txn_id)
        written = _as_written(repos, row, ledger, read)
        read_keyed = read.keyed if read else None
        if written.keyed is not None and written.keyed == read_keyed:
            status, selection, note = DesignationStatus.AGREED, written.keyed, None
        else:
            status, selection = DesignationStatus.PENDING, None
            if written.keyed is None:
                note = f"as written: {written.problem}"
            elif read_keyed is None:
                problem = read.problem if read else "the row is not reached by replay"
                note = f"as read: {problem}"
            else:
                note = (
                    "a row appended later renumbered the lots: the ids named "
                    f"{written.keyed} when written and {read_keyed} as read"
                )
        repos.designations.record(
            LotDesignation(
                txn_id=row.txn_id,
                status=status,
                as_written=written.keyed,
                as_read=read_keyed,
                lot_selection=selection,
                note=note,
            )
        )
        assert row.lot_selection is not None
        rows.append(
            Recovered(
                txn_id=row.txn_id,
                trade_date=row.trade_date,
                spec=row.lot_selection,
                status=status,
                as_written=written.keyed,
                as_read=read_keyed,
                note=note,
            )
        )
    return Recovery(tuple(rows))


def resolve(repos: Repositories, txn_id: int, *, reading: str, reason: str) -> LotDesignation:
    """Decide a pending designation as ``"as_written"`` or ``"as_read"``.

    The owner's decision, once, with the reason recorded beside it. The
    caller rebuilds in the same transaction.

    Raises:
        ValidationError: for a row that is not pending, a reading that could
            not be established, or a missing reason.
    """
    recorded = repos.designations.get(txn_id)
    if recorded is None:
        raise ValidationError(
            f"transaction {txn_id} has no recorded designation",
            code=E_USAGE,
            remedy="`pt lot designation list` shows the rows that have one.",
            txn_id=txn_id,
        )
    if not recorded.is_pending:
        raise ValidationError(
            f"transaction {txn_id}'s designation is already decided ({recorded.status})",
            code=E_LOT_DESIGNATION_UNRESOLVED,
            remedy="A recorded designation is decided once and never changed (ADR 0036).",
            txn_id=txn_id,
        )
    if not reason.strip():
        raise ValidationError(
            "a decision needs a reason",
            code=E_USAGE,
            remedy="Pass --reason, naming the evidence: the custodian's lot report, a 1099-B.",
        )
    if reading not in {DesignationStatus.AS_WRITTEN.value, DesignationStatus.AS_READ.value}:
        raise ValidationError(
            f"unknown reading {reading!r}: choose as_written or as_read", code=E_USAGE
        )
    status = DesignationStatus(reading)
    chosen = recorded.as_written if status is DesignationStatus.AS_WRITTEN else recorded.as_read
    if chosen is None:
        raise ValidationError(
            f"transaction {txn_id} has no {status.value.replace('_', ' ')} reading to choose",
            code=E_LOT_DESIGNATION_UNRESOLVED,
            remedy=(
                "Choose the other reading, or reverse the row with `pt trade reverse` "
                "and record the sale again naming the lots it relieved."
            ),
            txn_id=txn_id,
            note=recorded.note,
        )
    repos.designations.decide(txn_id, str(status), chosen, reason.strip())
    decided = repos.designations.get(txn_id)
    assert decided is not None
    return decided


def gains_by_reading(repos: Repositories) -> dict[int, dict[str, Decimal | None]]:
    """The realized gain each pending row gives under each of its readings.

    Two replays in a scratch savepoint, one per reading, so the owner decides
    with the consequence in front of them. A reading that does not apply
    gives ``None``.
    """
    pending = {d.txn_id: d for d in repos.designations.all(pending_only=True)}
    gains: dict[int, dict[str, Decimal | None]] = {txn_id: {} for txn_id in pending}
    for reading in ("as_written", "as_read"):

        def read(
            txn: Transaction, open_lots: list[Lot], reading: str = reading
        ) -> dict[int, Decimal]:
            recorded = pending.get(txn.txn_id) or repos.designations.get(txn.txn_id)
            chosen = getattr(recorded, reading) if recorded and recorded.is_pending else None
            selection = chosen or (recorded.lot_selection if recorded else None)
            if selection is None:
                raise ValidationError(
                    f"transaction {txn.txn_id} has no {reading} reading",
                    code=E_LOT_DESIGNATION_UNRESOLVED,
                )
            return resolve_keyed_selection(selection, open_lots)

        with scratch_savepoint(repos.con):
            ReplayEngine(repos, legacy_reader=read).rebuild()
            by_txn: dict[int, Decimal] = {}
            for disposition in repos.lots.dispositions():
                if disposition.txn_id in pending:
                    by_txn[disposition.txn_id] = (
                        by_txn.get(disposition.txn_id, ZERO) + disposition.realized_gain
                    )
        for txn_id in pending:
            gains[txn_id][reading] = by_txn.get(txn_id)
    return gains
