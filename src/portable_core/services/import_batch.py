"""The reviewable artifact between a custodian's export and the ledger.

ADR 0012. Import is three stages -- extract, review, commit -- and this module
owns the middle one's format and the last one's mechanics.

The batch **states what happened, not what follows from it**. A row carries the
event as the custodian described it; `portable` derives the cash effect, the lot
relief and the tax through the same services a typed command uses. That is the
whole point: every refusal that guards hand entry then guards an import too,
rather than an importer growing a second, laxer path into the ledger.

`schemas/import-batch-1.0.json` is the published contract for adapter authors.
It is not used at runtime -- `jsonschema` is a development dependency, and a
hand-written check produces a better error anyway: it can name the row index,
the field, and what to do about it. `tests/unit/test_import_batch.py` asserts
the two agree, which is what stops them drifting.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from portable_core.decimals import from_text, to_text
from portable_core.domain.enums import (
    BasisSource,
    FeeClass,
    ReliefMethod,
    TransactionSource,
    TransactionType,
)
from portable_core.domain.models import Transaction
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_USAGE
from portable_core.persistence.repositories import Repositories
from portable_core.services.replay import ReplayEngine
from portable_core.services.trading import TradeIntent, TradingService

__all__ = [
    "SUPPORTED_TYPES",
    "BatchImporter",
    "BatchRow",
    "BatchSource",
    "ImportBatch",
    "ImportResult",
    "dump_batch",
    "load_batch",
]

FORMAT: Final = "portable-import-batch"
FORMAT_VERSION: Final = 1
ZERO = Decimal("0.00")

#: Types a batch can carry, and the reason the list is short.
#:
#: Each has a service behind it that already validates and derives. Corporate
#: actions and the options lifecycle do not: they need position context a typed
#: command gathers interactively -- which lots a split touches, which stock leg
#: an assignment closes -- and half-supporting them would mean an importer
#: deriving basis by a second, unreviewed route. Refusing is honest
#: (`CLAUDE.md` invariant 10); ADR 0018 puts them in a per-custodian post-pass.
_TRADES: Final = frozenset(
    {
        TransactionType.BUY,
        TransactionType.SELL,
        TransactionType.SELL_SHORT,
        TransactionType.BUY_TO_COVER,
    }
)
_CASH: Final = frozenset(
    {
        TransactionType.DEPOSIT,
        TransactionType.WITHDRAWAL,
        TransactionType.TRANSFER,
        TransactionType.INTEREST,
        TransactionType.FEE,
        TransactionType.MARGIN_INTEREST,
    }
)
#: ADR 0015. Securities crossing the portfolio boundary without being traded.
#: These are what a cutover reconstruction emits: the opening position set has
#: to enter the ledger somehow, and every other lot-creating type moves cash.
_IN_KIND: Final = frozenset({TransactionType.TRANSFER_IN, TransactionType.TRANSFER_OUT})
_INCOME: Final = frozenset(
    {
        TransactionType.DIVIDEND,
        TransactionType.COUPON,
        TransactionType.RETURN_OF_CAPITAL,
    }
)
SUPPORTED_TYPES: Final[frozenset[TransactionType]] = _TRADES | _CASH | _INCOME | _IN_KIND

_ACTIONS: Final = frozenset({"append", "drop", "skip"})


@dataclass(frozen=True, slots=True)
class BatchSource:
    """Where a batch came from, and what its adapter could see."""

    broker: str
    files: tuple[tuple[str, str], ...]
    capabilities: tuple[str, ...] = ()
    period: tuple[date, date] | None = None


@dataclass(frozen=True, slots=True)
class BatchRow:
    """One prospective ledger row, or one deliberately not made.

    ``index`` is the row's position in the file, so every refusal can point at
    a line the reviewer can find.
    """

    index: int
    action: str
    rule: str
    source_row: Mapping[str, Any]
    external_ref: str | None = None
    account: str | None = None
    txn_type: TransactionType | None = None
    trade_date: date | None = None
    settlement_date: date | None = None
    symbol: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    amount: Decimal | None = None
    fees: Decimal = ZERO
    commissions: Decimal = ZERO
    fee_class: FeeClass | None = None
    taxes_withheld: Decimal = ZERO
    withholding_reclaimable: Decimal | None = None
    counter_account: str | None = None
    #: How a closing trade relieves lots. Stated in the batch rather than left
    #: to the account default, because ADR 0017 §2a solved the seeded basis
    #: under an assumed relief method and the ledger has to relieve the same
    #: way -- a block solved for FIFO and then relieved spec-ID gives a basis
    #: the solve never computed. Visible in the file, so a reviewer can change
    #: it, which is what the review is for.
    relief_method: ReliefMethod | None = None
    ex_date: date | None = None
    is_qualified: bool | None = None
    note: str | None = None
    #: ADR 0015, on an in-kind row only. `amount` is the market value on the
    #: transfer date -- the flow -- and these are what the owner paid at the
    #: delivering custodian. Two numbers, never interchangeable.
    original_basis: Decimal | None = None
    original_acquired_date: date | None = None
    #: ADR 0017. Which rung of the ladder the basis sits on, and the argument
    #: behind it where the rung is an approximate one.
    basis_source: BasisSource | None = None
    basis_assumption: str | None = None


@dataclass(frozen=True, slots=True)
class ImportBatch:
    source: BatchSource
    rows: tuple[BatchRow, ...]

    @property
    def to_append(self) -> tuple[BatchRow, ...]:
        return tuple(row for row in self.rows if row.action == "append")

    def counted(self) -> dict[str, int]:
        """Rows by action, with every action present even at zero.

        Absent and zero must not look the same: "no rows were dropped" and
        "dropping was not considered" are different claims about an import.
        """
        counts = dict.fromkeys(sorted(_ACTIONS), 0)
        for row in self.rows:
            counts[row.action] += 1
        return counts


@dataclass(frozen=True, slots=True)
class ImportResult:
    """What committing a batch did."""

    appended: int
    dropped: int
    skipped: int
    digest: str
    warnings: tuple[str, ...] = ()
    unverified_files: tuple[str, ...] = field(default=())


# ── loading ──────────────────────────────────────────────────────────────────


def _fail(message: str, **context: Any) -> ValidationError:
    return ValidationError(
        message,
        code=E_USAGE,
        remedy=(
            "See schemas/import-batch-1.0.json for the contract, or regenerate the "
            "batch with `pt import broker`."
        ),
        **context,
    )


def _decimal(value: Any, *, index: int, field_name: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        # A JSON number cannot round-trip a decimal, so one here is a defect in
        # whatever wrote the file rather than something to coerce and hope.
        raise _fail(
            f"row {index}: {field_name} is a JSON number; money and quantities are strings",
            row=index,
            field=field_name,
        )
    try:
        return from_text(value)
    except ValueError as exc:
        raise _fail(
            f"row {index}: {field_name} {value!r} is not a decimal", row=index, field=field_name
        ) from exc


def _date(value: Any, *, index: int, field_name: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise _fail(
            f"row {index}: {field_name} {value!r} is not YYYY-MM-DD",
            row=index,
            field=field_name,
        ) from exc


def dump_batch(batch: ImportBatch) -> str:
    """Serialise a batch to the published format. The inverse of `load_batch`.

    Written by hand rather than by a generic encoder, so that what goes out is
    exactly what `schemas/import-batch-1.0.json` describes and what comes back
    in reads identically -- a round trip is the only cheap check that the
    extract stage and the commit stage agree.

    Sorted keys and a trailing newline: a batch is a file a person reviews and
    very often a file `git diff` is run over, and a stable key order is what
    makes the second useful (invariant 6).
    """
    document: dict[str, Any] = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "source": {
            "broker": batch.source.broker,
            "capabilities": list(batch.source.capabilities),
            "files": [{"name": n, "sha256": h} for n, h in batch.source.files],
        },
        "rows": [_row_json(row) for row in batch.rows],
    }
    if batch.source.period is not None:
        start, end = batch.source.period
        document["source"]["period"] = {
            "from": start.isoformat(),
            "to": end.isoformat(),
        }
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _row_json(row: BatchRow) -> dict[str, Any]:
    """One row, with absent fields absent rather than null.

    A batch is read by a person. Forty nulls per row buries the six fields that
    say what the row is.
    """
    payload: dict[str, Any] = {
        "action": row.action,
        "rule": row.rule,
        "source_row": dict(row.source_row),
    }
    optional: dict[str, Any] = {
        "external_ref": row.external_ref,
        "account": row.account,
        "txn_type": str(row.txn_type) if row.txn_type else None,
        "trade_date": row.trade_date.isoformat() if row.trade_date else None,
        "settlement_date": (row.settlement_date.isoformat() if row.settlement_date else None),
        "symbol": row.symbol,
        "quantity": _text(row.quantity),
        "price": _text(row.price),
        "amount": _text(row.amount),
        "fees": _text(row.fees) if row.fees else None,
        "commissions": _text(row.commissions) if row.commissions else None,
        "fee_class": str(row.fee_class) if row.fee_class else None,
        "taxes_withheld": _text(row.taxes_withheld) if row.taxes_withheld else None,
        "withholding_reclaimable": _text(row.withholding_reclaimable),
        "counter_account": row.counter_account,
        "relief_method": str(row.relief_method) if row.relief_method else None,
        "ex_date": row.ex_date.isoformat() if row.ex_date else None,
        "is_qualified": row.is_qualified,
        "original_basis": _text(row.original_basis),
        "original_acquired_date": (
            row.original_acquired_date.isoformat() if row.original_acquired_date else None
        ),
        "basis_source": str(row.basis_source) if row.basis_source else None,
        "basis_assumption": row.basis_assumption,
        "note": row.note,
    }
    payload.update({k: v for k, v in optional.items() if v is not None})
    return payload


def _text(value: Decimal | None) -> str | None:
    """Canonical decimal text, never `str()`. ADR 0005."""
    return None if value is None else to_text(value)


def load_batch(path: Path) -> ImportBatch:
    """Read and validate a batch file.

    Raises:
        ValidationError: naming the row and the field. A batch is reviewed by a
            person before it is committed, so an error that says only "invalid"
            wastes the review.
    """
    if not path.is_file():
        raise _fail(f"batch file not found: {path}", path=str(path))
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _fail(f"{path} is not JSON: {exc}", path=str(path)) from exc

    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise _fail(f"{path} is not a portable import batch", path=str(path))
    if document.get("format_version") != FORMAT_VERSION:
        raise _fail(
            f"{path} is format version {document.get('format_version')!r}; this build "
            f"reads version {FORMAT_VERSION}",
            path=str(path),
        )

    raw_source = document.get("source")
    if not isinstance(raw_source, dict) or "broker" not in raw_source:
        raise _fail(f"{path}: `source` must name the broker that produced the batch")
    files = tuple(
        (str(entry["name"]), str(entry["sha256"]))
        for entry in raw_source.get("files", [])
        if isinstance(entry, dict) and "name" in entry and "sha256" in entry
    )
    period: tuple[date, date] | None = None
    raw_period = raw_source.get("period")
    if isinstance(raw_period, dict):
        first = _date(raw_period.get("from"), index=0, field_name="source.period.from")
        last = _date(raw_period.get("to"), index=0, field_name="source.period.to")
        if first is not None and last is not None:
            period = (first, last)

    source = BatchSource(
        broker=str(raw_source["broker"]),
        files=files,
        capabilities=tuple(str(c) for c in raw_source.get("capabilities", [])),
        period=period,
    )

    raw_rows = document.get("rows")
    if not isinstance(raw_rows, list):
        raise _fail(f"{path}: `rows` must be a list")

    return ImportBatch(
        source=source,
        rows=tuple(_row(raw, index) for index, raw in enumerate(raw_rows)),
    )


def _row(raw: Any, index: int) -> BatchRow:
    if not isinstance(raw, dict):
        raise _fail(f"row {index} is not an object", row=index)
    action = raw.get("action")
    if action not in _ACTIONS:
        raise _fail(
            f"row {index}: action {action!r} is not one of {sorted(_ACTIONS)}",
            row=index,
            action=action,
        )
    for required in ("rule", "source_row"):
        if required not in raw:
            raise _fail(
                f"row {index}: every row states its `{required}`",
                row=index,
                field=required,
            )
    source_row = raw["source_row"]
    if not isinstance(source_row, dict):
        raise _fail(f"row {index}: `source_row` must be the custodian's row as an object")

    common = {
        "index": index,
        "action": str(action),
        "rule": str(raw["rule"]),
        "source_row": source_row,
        "external_ref": raw.get("external_ref"),
    }
    if action != "append":
        return BatchRow(**common)  # type: ignore[arg-type]

    for required in ("account", "txn_type", "trade_date"):
        if not raw.get(required):
            raise _fail(
                f"row {index}: an appended row states its `{required}`",
                row=index,
                field=required,
            )
    raw_type = str(raw["txn_type"])
    try:
        txn_type = TransactionType(raw_type)
    except ValueError as exc:
        raise _fail(f"row {index}: unknown transaction type {raw_type!r}", row=index) from exc
    if txn_type not in SUPPORTED_TYPES:
        raise _fail(
            f"row {index}: a batch cannot carry {raw_type!r} in format version "
            f"{FORMAT_VERSION}",
            row=index,
            txn_type=raw_type,
            supported=sorted(str(t) for t in SUPPORTED_TYPES),
        )

    raw_class = raw.get("fee_class")
    return BatchRow(
        **common,  # type: ignore[arg-type]
        account=str(raw["account"]),
        txn_type=txn_type,
        trade_date=_date(raw["trade_date"], index=index, field_name="trade_date"),
        settlement_date=_date(
            raw.get("settlement_date"), index=index, field_name="settlement_date"
        ),
        symbol=raw.get("symbol") or None,
        quantity=_decimal(raw.get("quantity"), index=index, field_name="quantity"),
        price=_decimal(raw.get("price"), index=index, field_name="price"),
        amount=_decimal(raw.get("amount"), index=index, field_name="amount"),
        fees=_decimal(raw.get("fees"), index=index, field_name="fees") or ZERO,
        commissions=_decimal(raw.get("commissions"), index=index, field_name="commissions")
        or ZERO,
        fee_class=FeeClass(raw_class) if raw_class else None,
        relief_method=_relief(raw.get("relief_method"), index=index),
        original_basis=_decimal(
            raw.get("original_basis"), index=index, field_name="original_basis"
        ),
        original_acquired_date=_date(
            raw.get("original_acquired_date"),
            index=index,
            field_name="original_acquired_date",
        ),
        basis_source=_basis_source(raw.get("basis_source"), index=index),
        basis_assumption=raw.get("basis_assumption") or None,
        taxes_withheld=_decimal(
            raw.get("taxes_withheld"), index=index, field_name="taxes_withheld"
        )
        or ZERO,
        withholding_reclaimable=_decimal(
            raw.get("withholding_reclaimable"),
            index=index,
            field_name="withholding_reclaimable",
        ),
        counter_account=raw.get("counter_account") or None,
        ex_date=_date(raw.get("ex_date"), index=index, field_name="ex_date"),
        is_qualified=raw.get("is_qualified"),
        note=raw.get("note") or None,
    )


# ── committing ───────────────────────────────────────────────────────────────


class BatchImporter:
    """Turns a reviewed batch into ledger rows.

    Rows are appended in **trade-date order** and derived incrementally, then
    the whole ledger is replayed once at the end (ADR 0012, ADR 0016). Ordering
    matters because a sale's lot relief must see the purchase that precedes it
    in the same batch; the closing rebuild matters because a historical batch is
    back-dated relative to whatever the file already holds, and only a replay
    puts derived state in the ledger's own order.
    """

    def __init__(self, repos: Repositories) -> None:
        self.repos = repos
        self.trading = TradingService(repos)
        self.replay = ReplayEngine(repos)

    def commit(self, batch: ImportBatch, *, batch_path: Path | None = None) -> ImportResult:
        """Append the batch and replay. The caller wraps this in a transaction.

        There is deliberately no separate "validate without writing" path.
        Rows are validated *as they are applied*, because a sale's lot relief
        has to see the purchase that precedes it in the same batch -- checking
        each row against the state before the batch would refuse a batch that
        commits perfectly well. A dry run is therefore this method inside a
        transaction that is rolled back (`scratch_transaction`), which runs the
        same code and writes nothing.
        """
        unverified = self._verify_files(batch, batch_path)

        for row in self._ordered(batch):
            transaction = self._build(row)
            txn_id = self.repos.transactions.append(transaction)
            # Incremental within the batch so relief sees the rows before it;
            # the rebuild below is what makes the result canonical.
            self.replay.apply_transaction(replace(transaction, txn_id=txn_id))

        outcome = self.replay.rebuild()
        counts = batch.counted()
        return ImportResult(
            appended=counts["append"],
            dropped=counts["drop"],
            skipped=counts["skip"],
            digest=outcome.digest,
            warnings=outcome.warnings,
            unverified_files=unverified,
        )

    # ── internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _ordered(batch: ImportBatch) -> list[BatchRow]:
        """Appended rows in ledger order, stable within a date by file order."""
        return sorted(
            batch.to_append,
            key=lambda row: (row.trade_date or date.min, row.index),
        )

    def _verify_files(self, batch: ImportBatch, batch_path: Path | None) -> tuple[str, ...]:
        """Refuse a batch whose source document changed since it was reviewed.

        A review approves particular rows against a particular export. If the
        export has since been re-downloaded the review no longer covers what is
        about to be committed.

        A file that cannot be found is reported rather than refused -- a batch
        is often reviewed somewhere other than where it was extracted -- and
        the report says which, so "verified" and "not checked" stay distinct.
        """
        if batch_path is None:
            return tuple(name for name, _ in batch.source.files)

        unverified: list[str] = []
        for name, expected in batch.source.files:
            candidate = batch_path.parent / name
            if not candidate.is_file():
                unverified.append(name)
                continue
            actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if actual != expected:
                raise _fail(
                    f"{name} has changed since this batch was extracted",
                    file=name,
                    expected=expected,
                    actual=actual,
                )
        return tuple(unverified)

    def _build(self, row: BatchRow) -> Transaction:
        assert row.txn_type is not None and row.trade_date is not None
        account = self.repos.accounts.resolve(str(row.account))

        if row.txn_type in _TRADES:
            return self._trade(row, account)
        if row.txn_type in _IN_KIND:
            return self._in_kind(row, account)
        if row.txn_type in _CASH:
            return self._cash(row, account)
        return self._income(row, account)

    def _in_kind(self, row: BatchRow, account: Any) -> Transaction:
        """A transfer in or out, through the same service a typed command uses.

        Which means the same refusals: a basis that claims to be `derived` when
        it came from elsewhere, an approximate rung with no stated assumption,
        an acquisition date after the transfer, a fractional share an account
        cannot hold. An importer that reached past those would be a second,
        laxer path into the ledger, which is the thing ADR 0012 exists to
        prevent.
        """
        if row.symbol is None or row.quantity is None or row.amount is None:
            raise _fail(
                f"row {row.index}: an in-kind transfer states its symbol, quantity "
                f"and amount — the amount being the market value on the transfer "
                f"date, which is the flow and not the basis",
                row=row.index,
            )
        instrument = self.repos.instruments.resolve(row.symbol, on=row.trade_date)
        if row.txn_type is TransactionType.TRANSFER_OUT:
            return self.trading.record_transfer_out(
                account,
                instrument,
                row.quantity,
                row.trade_date,  # type: ignore[arg-type]
                value=row.amount,
                note=row.note,
                external_ref=row.external_ref,
                source=TransactionSource.IMPORT,
            )
        if row.basis_source is None:
            raise _fail(
                f"row {row.index}: a transfer_in states where its basis came from "
                f"(`basis_source`). It creates a lot, and a lot cannot exist "
                f"without answering that",
                row=row.index,
            )
        return self.trading.record_transfer_in(
            account,
            instrument,
            row.quantity,
            row.trade_date,  # type: ignore[arg-type]
            value=row.amount,
            original_basis=row.original_basis,
            original_acquired_date=row.original_acquired_date,
            basis_source=row.basis_source,
            basis_assumption=row.basis_assumption,
            note=row.note,
            external_ref=row.external_ref,
            source=TransactionSource.IMPORT,
        )

    def _trade(self, row: BatchRow, account: Any) -> Transaction:
        if row.symbol is None or row.quantity is None or row.price is None:
            raise _fail(
                f"row {row.index}: a trade states its symbol, quantity and price",
                row=row.index,
            )
        instrument = self.repos.instruments.resolve(row.symbol, on=row.trade_date)
        plan = self.trading.plan(
            TradeIntent(
                account=account,
                instrument=instrument,
                txn_type=row.txn_type,  # type: ignore[arg-type]
                quantity=row.quantity,
                price=row.price,
                trade_date=row.trade_date,  # type: ignore[arg-type]
                fees=row.fees,
                commissions=row.commissions,
                fee_class=row.fee_class,
                relief_method=row.relief_method,
                settlement_date=row.settlement_date,
                note=row.note,
                external_ref=row.external_ref,
                source=TransactionSource.IMPORT,
            )
        )
        return plan.transaction

    def _cash(self, row: BatchRow, account: Any) -> Transaction:
        if row.amount is None:
            raise _fail(
                f"row {row.index}: a cash row states its amount, always positive",
                row=row.index,
            )
        counter = (
            self.repos.accounts.resolve(row.counter_account) if row.counter_account else None
        )
        return self.trading.record_cash(
            account,
            row.txn_type,  # type: ignore[arg-type]
            row.amount,
            row.trade_date,  # type: ignore[arg-type]
            counter_account=counter,
            fee_class=row.fee_class,
            note=row.note,
            external_ref=row.external_ref,
            source=TransactionSource.IMPORT,
            # A statement is a record of what happened, so a withdrawal that
            # took the account negative is a fact to record, not a proposal to
            # refuse. The cash reconciliation is what catches a wrong one.
            allow_overdraft=True,
        )

    def _income(self, row: BatchRow, account: Any) -> Transaction:
        if row.symbol is None or row.amount is None:
            raise _fail(
                f"row {row.index}: an income row states its symbol and amount",
                row=row.index,
            )
        instrument = self.repos.instruments.resolve(row.symbol, on=row.trade_date)
        return self.trading.record_income(
            account,
            instrument,
            row.txn_type,  # type: ignore[arg-type]
            row.amount,
            row.trade_date,  # type: ignore[arg-type]
            ex_date=row.ex_date,
            taxes_withheld=row.taxes_withheld,
            withholding_reclaimable=row.withholding_reclaimable,
            is_qualified=row.is_qualified,
            note=row.note,
            external_ref=row.external_ref,
            source=TransactionSource.IMPORT,
        )


def _basis_source(value: Any, *, index: int) -> BasisSource | None:
    """Parse a declared basis source, or refuse naming the row."""
    if value is None or value == "":
        return None
    try:
        return BasisSource(str(value))
    except ValueError as exc:
        raise _fail(
            f"row {index}: unknown basis_source {value!r}",
            row=index,
            basis_source=str(value),
            known=sorted(s.value for s in BasisSource),
        ) from exc


def _relief(value: Any, *, index: int) -> ReliefMethod | None:
    """Parse a declared relief method, or refuse naming the row."""
    if value is None or value == "":
        return None
    try:
        return ReliefMethod(str(value))
    except ValueError as exc:
        raise _fail(
            f"row {index}: unknown relief_method {value!r}",
            row=index,
            relief_method=str(value),
            known=sorted(m.value for m in ReliefMethod),
        ) from exc
