"""The generic tabular adapter -- two TOML files and no Python per custodian.

ADR 0018 §4. This module is the thing that makes that claim true: it reads
`source.toml` and `activity_map.toml`, opens whatever delimited files they name,
and emits the canonical records of `records.py`. A custodian whose exports are
plain tabular files is then a fixture and two mapping files, reviewed as data.

Three properties are worth stating plainly, because each is a decision that
could reasonably have gone the other way:

**It emits records, not ledger rows.** The adapter's job ends at
``HoldingRecord`` and ``TransactionRecord``. Turning those into a batch needs
the cutover reconstruction (ADR 0017), which is a separate piece of work with
its own refusals. Stopping here means the adapter can be reviewed against a
custodian's file on its own terms -- "does this read the export correctly?" --
which is a question a person can actually answer.

**A withheld capability strips its data.** If ``SETTLEMENT_DATE`` fails its
check, the records carry no settlement dates -- not the dates that failed. A
capability that is merely a label on data that flows through anyway is not a
safeguard; it is a comment. The reference custodian's settlement column, most
of whose populated cells precede their own trade dates, is exactly the case:
the right outcome is that the column is *not read*, and the reason is reported.

**Nothing is guessed.** No delimiter sniffing, no date-format inference, no
default arm on the activity map, no fallback for a header the file does not
have. Every one of those would work for the first custodian and produce a
plausible wrong number for the second.
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from portable_core.domain.enums import TransactionType
from portable_core.domain.import_records import (
    ClosedLotRecord,
    HoldingRecord,
    MappedTransaction,
    TransactionRecord,
)
from portable_core.errors import ValidationError
from portable_core.errors.kinds import (
    E_IMPORT_COLUMN_MISSING,
    E_IMPORT_SOURCE_INVALID,
)
from portable_core.importers.activity import (
    ActivityMap,
    ActivityRule,
    Sign,
    load_activity_map,
)
from portable_core.importers.capabilities import (
    CapabilityFinding,
    CapabilitySet,
    ImportCapability,
)
from portable_core.importers.checks import CHECKS, CheckInput, run_check
from portable_core.importers.crosswalk import Crosswalk, load_crosswalk
from portable_core.importers.pairing import Leg, pair_legs
from portable_core.importers.source import (
    HOLDINGS,
    REALIZED,
    TRANSACTIONS,
    DocumentSpec,
    SourceSpec,
    load_source,
)

__all__ = ["AdapterReport", "SkippedRow", "TabularAdapter"]

#: Which record field each capability guards. A capability that fails its check
#: does not merely go unreported -- the field it guards is cleared, so nothing
#: downstream can read data that was never validated.
_GUARDED: Final[dict[ImportCapability, tuple[str, str]]] = {
    ImportCapability.COST_BASIS: (HOLDINGS, "cost_basis"),
    ImportCapability.ACQUISITION_DATE: (HOLDINGS, "acquired"),
    ImportCapability.LOT_DETAIL: (HOLDINGS, "lot_id"),
    ImportCapability.SETTLEMENT_DATE: (TRANSACTIONS, "settlement_date"),
    ImportCapability.TRANSACTION_ID: (TRANSACTIONS, "external_id"),
}

#: Fields parsed as dates, per document, so one parse routine serves both.
_DATE_FIELDS: Final[dict[str, frozenset[str]]] = {
    HOLDINGS: frozenset({"as_of", "acquired"}),
    TRANSACTIONS: frozenset({"trade_date", "settlement_date"}),
    REALIZED: frozenset({"acquired", "disposed"}),
}
_NUMBER_FIELDS: Final[dict[str, frozenset[str]]] = {
    HOLDINGS: frozenset({"quantity", "market_value", "cost_basis"}),
    TRANSACTIONS: frozenset({"quantity", "amount"}),
    REALIZED: frozenset({"quantity", "cost_basis", "proceeds"}),
}

_SPREADSHEETS: Final = frozenset({".xlsx", ".xls", ".xlsm", ".ods"})


@dataclass(frozen=True, slots=True)
class SkippedRow:
    """A row the activity map deliberately keeps out of the ledger."""

    index: int
    activity: str
    reason: str


@dataclass(frozen=True, slots=True)
class AdapterReport:
    """Everything one read of a custodian's exports produced.

    Reported before anything is written, because ADR 0018's consequence is that
    the user sees what their custodian supports -- and therefore what the
    resulting portfolio will be unable to claim -- as the first thing, not as a
    footnote under a number.
    """

    broker: str
    name: str
    capabilities: CapabilitySet
    holdings: tuple[HoldingRecord, ...]
    transactions: tuple[TransactionRecord, ...]
    skipped: tuple[SkippedRow, ...]
    #: Every transaction row with its activity resolved, skips included. The
    #: seam ADR 0018 §5 describes: downstream sees this and never the map.
    mapped: tuple[MappedTransaction, ...]
    #: (file name, sha256) per document read, for the batch's provenance.
    files: tuple[tuple[str, str], ...]
    as_of: date | None = None
    period: tuple[date, date] | None = None
    #: From the optional realized document. Empty where none is declared.
    closed_lots: tuple[ClosedLotRecord, ...] = ()

    @property
    def accounts(self) -> tuple[str, ...]:
        seen = {h.account for h in self.holdings} | {t.account for t in self.transactions}
        return tuple(sorted(seen))


class TabularAdapter:
    """Reads one custodian's delimited exports into canonical records."""

    def __init__(
        self,
        spec: SourceSpec,
        activity: ActivityMap,
        root: Path,
        crosswalks: dict[str, Crosswalk] | None = None,
    ) -> None:
        self.spec = spec
        self.activity = activity
        self.root = root
        #: document kind -> the crosswalk its identifiers resolve through.
        self.crosswalks = crosswalks or {}
        for check in spec.checks:
            if check.check not in CHECKS:
                raise ValidationError(
                    f"capability {check.capability.value} names unknown check "
                    f"{check.check!r}. Known checks: " + ", ".join(sorted(CHECKS)),
                    code=E_IMPORT_SOURCE_INVALID,
                    capability=check.capability.value,
                    check=check.check,
                )
        self._check_map_against_columns()

    def _check_map_against_columns(self) -> None:
        """The map may only read columns the source actually maps.

        Caught here, where both files are in hand, rather than on the first
        row: a note-keyed rule over a document with no note column would match
        nothing and refuse every row -- correctly, but six thousand rows into
        an import instead of at the review.
        """
        transactions = self.spec.document(TRANSACTIONS)
        if self.activity.uses_notes and transactions.column("note") is None:
            raise ValidationError(
                "the activity map keys rules on the note (a `note` pattern or a "
                "pair `counterpart`), but source.toml maps no `note` column for the "
                "transactions document. Map the column the patterns are meant to "
                "read",
                code=E_IMPORT_SOURCE_INVALID,
            )
        if self.activity.uses_identifiers and transactions.column("identifier") is None:
            raise ValidationError(
                "the activity map restricts a rule to cash-equivalent identifiers "
                "(`identifiers`, or an `attach`), but source.toml maps no `identifier` "
                "column for the transactions document, so there is nothing to check "
                "against the declared set",
                code=E_IMPORT_SOURCE_INVALID,
            )

    @classmethod
    def load(cls, path: Path) -> TabularAdapter:
        """Load an adapter from its directory. Both mapping files are required."""
        root = path if path.is_dir() else path.parent
        spec = load_source(path)
        crosswalks = {
            kind: load_crosswalk(root / document.crosswalk)
            for kind, document in spec.documents.items()
            if document.crosswalk is not None
        }
        return cls(spec, load_activity_map(root / "activity_map.toml"), root, crosswalks)

    # ── reading ──────────────────────────────────────────────────────────────

    def read(self) -> AdapterReport:
        """Read both documents, decide the capabilities, and emit records."""
        holdings_rows = self._rows(self.spec.document(HOLDINGS))
        transaction_rows = self._rows(self.spec.document(TRANSACTIONS))
        realized_rows = (
            self._rows(self.spec.document(REALIZED)) if REALIZED in self.spec.documents else []
        )

        capabilities = self._capabilities(holdings_rows, transaction_rows)
        holdings = self._holdings(holdings_rows, capabilities)
        # Every account the export mentions: what a pairing rule's named
        # counterpart is checked against, to tell a hole in the history from
        # a movement across the portfolio boundary.
        accounts = frozenset(
            {h.account for h in holdings}
            | {str(r["account"]) for r in transaction_rows if r.get("account")}
        )
        transactions, skipped, mapped = self._transactions(
            transaction_rows, capabilities, accounts
        )

        self._check_cash_is_stated(holdings)
        as_of = self._one_as_of(holdings)
        dates = sorted(t.trade_date for t in transactions)

        return AdapterReport(
            broker=self.spec.broker,
            name=self.spec.name,
            capabilities=capabilities,
            holdings=holdings,
            transactions=transactions,
            skipped=skipped,
            mapped=mapped,
            files=self._digests(),
            as_of=as_of,
            period=(dates[0], dates[-1]) if dates else None,
            closed_lots=self._closed_lots(realized_rows),
        )

    def _path(self, document: DocumentSpec) -> Path:
        path = self.root / document.file
        if not path.exists():
            raise ValidationError(
                f"the {document.kind} document {document.file!r} is not in {self.root}",
                code=E_IMPORT_SOURCE_INVALID,
                document=document.kind,
                path=str(path),
            )
        if path.suffix.lower() in _SPREADSHEETS:
            # Refused by name rather than half-read. `portable`'s runtime
            # dependencies are typer and rich; adding a spreadsheet reader to
            # parse a file the custodian will also emit as CSV is a large
            # dependency for no capability (`CLAUDE.md` invariant 10).
            raise ValidationError(
                f"{document.file} is a spreadsheet. Export it as CSV from the "
                f"custodian, or save it as CSV, and point `file` at that: "
                f"`portable` reads delimited text and does not parse workbooks",
                code=E_IMPORT_SOURCE_INVALID,
                document=document.kind,
                path=str(path),
            )
        return path

    def _rows(self, document: DocumentSpec) -> list[dict[str, Any]]:
        """One dict per data row, canonical field -> parsed value or None."""
        path = self._path(document)
        with path.open(encoding=self.spec.numbers.encoding, newline="") as handle:
            reader = csv.DictReader(handle)
            headers = reader.fieldnames or []
            self._check_headers(document, headers)
            return [
                self._row(document, raw, index) for index, raw in enumerate(reader, start=1)
            ]

    def _check_headers(self, document: DocumentSpec, headers: Sequence[str]) -> None:
        present = {h.strip() for h in headers if h}
        missing = {
            field: header for field, header in document.columns.items() if header not in present
        }
        if missing:
            raise ValidationError(
                f"the {document.kind} document {document.file!r} has no column "
                + ", ".join(f"{h!r} (mapped as {f})" for f, h in sorted(missing.items()))
                + ". Columns present: "
                + ", ".join(repr(h) for h in sorted(present)),
                code=E_IMPORT_COLUMN_MISSING,
                document=document.kind,
                missing=sorted(missing.values()),
                present=sorted(present),
            )

    def _row(self, document: DocumentSpec, raw: dict[str, Any], index: int) -> dict[str, Any]:
        numbers = self.spec.numbers
        parsed: dict[str, Any] = {"_index": index, "_raw": _verbatim(raw)}
        for field_name, header in document.columns.items():
            cell = raw.get(header)
            text = "" if cell is None else str(cell)
            if numbers.is_blank(text):
                parsed[field_name] = None
            elif field_name in _DATE_FIELDS[document.kind]:
                parsed[field_name] = numbers.date(text, what=header, row=index)
            elif field_name in _NUMBER_FIELDS[document.kind]:
                parsed[field_name] = numbers.number(text, what=header, row=index)
            else:
                parsed[field_name] = text.strip()
        if document.kind == HOLDINGS and document.as_of is not None:
            parsed.setdefault("as_of", None)
            if parsed["as_of"] is None:
                parsed["as_of"] = document.as_of
        return parsed

    # ── capabilities ─────────────────────────────────────────────────────────

    def _capabilities(
        self, holdings: list[dict[str, Any]], transactions: list[dict[str, Any]]
    ) -> CapabilitySet:
        by_document = {HOLDINGS: holdings, TRANSACTIONS: transactions}
        findings: list[CapabilityFinding] = []
        declared = {check.capability for check in self.spec.checks}
        for check in self.spec.checks:
            document = self.spec.document(check.document)
            findings.append(
                run_check(
                    check,
                    CheckInput(
                        rows=by_document[check.document],
                        activity=self.activity,
                        mapped=check.field is None or check.field in document.columns,
                    ),
                )
            )
        # Every capability appears, declared or not. A set of what is present
        # cannot answer "why not?", and that is the question a refused tax
        # report actually raises.
        findings.extend(
            CapabilityFinding(
                capability=capability,
                declared=False,
                reason="the source declares no check for it",
            )
            for capability in ImportCapability
            if capability not in declared
        )
        return CapabilitySet(tuple(sorted(findings, key=lambda f: f.capability.value)))

    # ── records ──────────────────────────────────────────────────────────────

    def _holdings(
        self, rows: list[dict[str, Any]], capabilities: CapabilitySet
    ) -> tuple[HoldingRecord, ...]:
        guarded = self._cleared(HOLDINGS, capabilities)
        records: list[HoldingRecord] = []
        for row in rows:
            index = row["_index"]
            account = _required(row, "account", index)
            identifier = self._resolve(HOLDINGS, _required(row, "identifier", index), index)
            is_cash = self.spec.is_cash_equivalent(account, identifier)
            quantity = row.get("quantity")
            market_value = row.get("market_value")
            if quantity is None and is_cash and isinstance(market_value, Decimal):
                # ADR 0013: a sweep vehicle is cash, and custodians state cash
                # as a balance rather than a share count. The balance is the
                # quantity of a par-priced vehicle, and the reconstruction and
                # the reconciler already read a cash line's market value as its
                # balance; this makes the record say the same thing.
                quantity = market_value
            if not isinstance(quantity, Decimal):
                raise _missing(index, "quantity", "a holdings line")
            as_of = row.get("as_of")
            if not isinstance(as_of, date):
                raise _missing(index, "as_of", "a holdings line")
            records.append(
                HoldingRecord(
                    as_of=as_of,
                    account=account,
                    identifier=identifier,
                    quantity=quantity,
                    is_cash_equivalent=is_cash,
                    market_value=market_value,
                    cost_basis=None if "cost_basis" in guarded else row.get("cost_basis"),
                    acquired=None if "acquired" in guarded else row.get("acquired"),
                    lot_id=None if "lot_id" in guarded else row.get("lot_id"),
                )
            )
        return tuple(records)

    def _closed_lots(self, rows: list[dict[str, Any]]) -> tuple[ClosedLotRecord, ...]:
        """The realized document, as records. Nothing is derived from it here."""
        records: list[ClosedLotRecord] = []
        for row in rows:
            index = row["_index"]
            account = _required(row, "account", index)
            identifier = self._resolve(REALIZED, _required(row, "identifier", index), index)
            acquired, disposed = row.get("acquired"), row.get("disposed")
            if not isinstance(acquired, date):
                raise _missing(index, "acquired", "a closed lot")
            if not isinstance(disposed, date):
                raise _missing(index, "disposed", "a closed lot")
            quantity, basis = row.get("quantity"), row.get("cost_basis")
            if not isinstance(quantity, Decimal):
                raise _missing(index, "quantity", "a closed lot")
            if not isinstance(basis, Decimal):
                raise _missing(index, "cost_basis", "a closed lot")
            proceeds = row.get("proceeds")
            records.append(
                ClosedLotRecord(
                    account=account,
                    identifier=identifier,
                    acquired=acquired,
                    disposed=disposed,
                    quantity=abs(quantity),
                    cost_basis=basis,
                    source_row=row["_raw"],
                    proceeds=proceeds if isinstance(proceeds, Decimal) else None,
                )
            )
        return tuple(records)

    def _transactions(
        self,
        rows: list[dict[str, Any]],
        capabilities: CapabilitySet,
        accounts: frozenset[str],
    ) -> tuple[
        tuple[TransactionRecord, ...],
        tuple[SkippedRow, ...],
        tuple[MappedTransaction, ...],
    ]:
        """Canonical records, the skips, and every row with its rule resolved.

        The third return is the seam ADR 0018 §5 describes: the adapter owns the
        activity map, so the adapter resolves the activity, and everything
        downstream works from the resolution rather than from the vocabulary.
        Skipped rows appear there too — a row deliberately kept out of the
        ledger is a decision the batch has to be able to show.
        """
        guarded = self._cleared(TRANSACTIONS, capabilities)
        records: list[TransactionRecord] = []
        skipped: list[SkippedRow] = []
        mapped: list[MappedTransaction] = []
        indices: list[int] = []
        legs: list[Leg] = []

        attachments: list[Leg] = []

        for row in rows:
            index = row["_index"]
            indices.append(index)
            activity = _required(row, "activity", index)
            account = _required(row, "account", index)
            raw_identifier = row.get("identifier")
            identifier = (
                self._resolve(TRANSACTIONS, raw_identifier, index)
                if isinstance(raw_identifier, str)
                else None
            )
            rule = self.activity.rule_for(
                activity,
                note=row.get("note"),
                cash_equivalent=(
                    self.spec.is_cash_equivalent(account, identifier)
                    if identifier is not None
                    else None
                ),
                row=index,
            )
            # Built leniently for a skipped row: it will never become a
            # ledger row, so holding it to a ledger row's completeness
            # would refuse the import over a blank in a memo line -- which
            # is frequently the very reason the map skips it.
            record = self._record(
                row, rule, activity, identifier, guarded, index, strict=not rule.skip
            )

            if rule.skip:
                skipped.append(
                    SkippedRow(index=index, activity=activity, reason=rule.reason or "")
                )
                mapped.append(
                    MappedTransaction(record=record, rule=rule.label, reason=rule.reason)
                )
                continue

            records.append(record)
            if rule.pair is not None:
                # Decided once every row is read: a leg's meaning depends on
                # whether its other half is in the file.
                legs.append(Leg(index=index, record=record, rule=rule))
                mapped.append(MappedTransaction(record=record, rule=rule.label))
                continue
            if rule.attach is not None:
                attachments.append(Leg(index=index, record=record, rule=rule))
                mapped.append(MappedTransaction(record=record, rule=rule.label))
                continue
            mapped.append(
                MappedTransaction(
                    record=record,
                    rule=rule.label,
                    txn_type=rule.txn_type,
                    fee_class=rule.fee_class,
                )
            )

        if legs:
            paired = pair_legs(legs, accounts, self.spec.account_aliases)
            mapped = [paired.get(i, m) for i, m in zip(indices, mapped, strict=True)]
            skipped.extend(
                SkippedRow(index=leg.index, activity=leg.record.activity, reason=reason)
                for leg in legs
                if (reason := paired[leg.index].reason) is not None
            )
        if attachments:
            mapped = _attach(attachments, indices, mapped)
            skipped.extend(
                SkippedRow(
                    index=leg.index,
                    activity=leg.record.activity,
                    reason=mapped[indices.index(leg.index)].reason or "",
                )
                for leg in attachments
            )
        skipped.sort(key=lambda entry: entry.index)
        return tuple(records), tuple(skipped), tuple(mapped)

    def _record(
        self,
        row: dict[str, Any],
        rule: ActivityRule,
        activity: str,
        identifier: str | None,
        guarded: frozenset[str],
        index: int,
        *,
        strict: bool = True,
    ) -> TransactionRecord:
        """One canonical record: signs normalised, withheld capabilities cleared.

        ``strict`` is False only for a row the map skips. Such a row is carried
        into the batch so the decision is visible, and its numbers are never
        read -- what a reviewer needs from it is the verbatim source row and
        the rule that dropped it.
        """
        traded = row.get("trade_date")
        if not isinstance(traded, date):
            raise _missing(index, "trade_date", "a transaction")
        value: Decimal | None = None
        if strict:
            quantity = _apply(rule.quantity, row.get("quantity"), index, "quantity")
            amount = _apply(rule.cash, row.get("amount"), index, "amount")
            value = _apply(rule.value, row.get("amount"), index, "amount")
        else:
            raw_quantity = row.get("quantity")
            raw_amount = row.get("amount")
            quantity = raw_quantity if isinstance(raw_quantity, Decimal) else None
            amount = raw_amount if isinstance(raw_amount, Decimal) else None
        return TransactionRecord(
            trade_date=traded,
            account=_required(row, "account", index),
            activity=activity,
            identifier=identifier,
            quantity=quantity,
            # `Sign.NONE` is the map asserting this activity moves no cash --
            # a stated zero, not an unstated one.
            amount=amount if amount is not None else Decimal("0"),
            source_row=row["_raw"],
            value=value,
            external_id=(None if "external_id" in guarded else row.get("external_id")),
            settlement_date=(
                None if "settlement_date" in guarded else row.get("settlement_date")
            ),
            note=row.get("note"),
        )

    def _resolve(self, document: str, identifier: str, index: int) -> str:
        """The identifier as the ledger will know it.

        Through the document's crosswalk where one is declared -- the custodian
        wrote a name, and a name it does not carry is a refusal -- and verbatim
        otherwise. The cash-equivalent set is checked against the *resolved*
        identifier, so an adapter declares its sweep vehicles by symbol once
        rather than by every spelling the custodian uses for them.
        """
        crosswalk = self.crosswalks.get(document)
        if crosswalk is None:
            return identifier
        return crosswalk.resolve(identifier, row=index)

    def _cleared(self, document: str, capabilities: CapabilitySet) -> frozenset[str]:
        """Fields whose capability was withheld, and which are therefore unread."""
        return frozenset(
            field
            for capability, (kind, field) in _GUARDED.items()
            if kind == document and capability not in capabilities
        )

    # ── whole-document validation ────────────────────────────────────────────

    def _check_cash_is_stated(self, holdings: tuple[HoldingRecord, ...]) -> None:
        """Every account's snapshot states a cash balance. ADR 0018 §1.

        Cash on the snapshot is required rather than optional because cash
        reconciliation is the only check that catches a sign error, a dropped
        row, or a double-counted transfer -- the errors that leave every share
        count right and the money wrong.
        """
        document = self.spec.document(HOLDINGS)
        exempt = {a.casefold() for a in document.allow_missing_cash}
        with_cash = {h.account for h in holdings if h.is_cash_equivalent}
        missing = sorted(
            {h.account for h in holdings}
            - with_cash
            - {a for a in {h.account for h in holdings} if a.casefold() in exempt}
        )
        if missing:
            raise ValidationError(
                "the holdings snapshot states no cash for "
                + ", ".join(missing)
                + ". Either the account's cash identifier is missing from "
                "[cash_equivalents] in source.toml, or the export omits its cash "
                "line; if the custodian genuinely reports none, list the account "
                "in `allow_missing_cash` so the exception is on the record",
                code=E_IMPORT_SOURCE_INVALID,
                accounts=missing,
                declared=sorted(
                    i for values in self.spec.cash_equivalents.values() for i in values
                ),
            )

    def _one_as_of(self, holdings: tuple[HoldingRecord, ...]) -> date | None:
        """A snapshot has one date.

        Rows dated differently are not one snapshot, and rolling back from them
        as if they were would anchor the reconstruction at a date that never
        existed (ADR 0017).
        """
        dates = sorted({h.as_of for h in holdings})
        if len(dates) > 1:
            raise ValidationError(
                "the holdings snapshot carries more than one as-of date ("
                + ", ".join(d.isoformat() for d in dates)
                + "). A reconstruction rolls back from one dated position set; "
                "split the file, or state a single `as_of` in source.toml",
                code=E_IMPORT_SOURCE_INVALID,
                dates=[d.isoformat() for d in dates],
            )
        return dates[0] if dates else None

    def _digests(self) -> tuple[tuple[str, str], ...]:
        """sha256 per document, so a batch can name what it was built from."""
        return tuple(
            (document.file, _sha256(self.root / document.file))
            for kind, document in self.spec.documents.items()
        )


#: Income rows a withholding line may attach to.
_ATTACHABLE: Final = frozenset(
    {
        TransactionType.DIVIDEND,
        TransactionType.DIVIDEND_REINVEST,
        TransactionType.COUPON,
        TransactionType.RETURN_OF_CAPITAL,
        TransactionType.INTEREST,
    }
)


def _attach(
    attachments: Sequence[Leg], indices: Sequence[int], mapped: list[MappedTransaction]
) -> list[MappedTransaction]:
    """Fold each withholding line into the income row it belongs to.

    PORT-GIPS-A06: withholding is tax, not a fee, and the return is earned on
    the gross while the cash moved by the net. A custodian that reports the
    tax as its own line has reported one event in two rows; the ledger wants
    the income row with `taxes_withheld` on it, and the tax line carried as a
    skip naming the row it went to. The target is the one income row on the
    same day, in the same account, on the same instrument -- none or two is a
    refusal, never a guess.
    """
    position = {index: i for i, index in enumerate(indices)}
    result = list(mapped)
    for line in attachments:
        record = line.record
        targets = [
            (i, m)
            for i, m in enumerate(result)
            if m.txn_type in _ATTACHABLE
            and m.record.trade_date == record.trade_date
            and m.record.account == record.account
            and m.record.identifier == record.identifier
        ]
        where = (
            f"row {line.index}: {line.rule.match!r} on {record.identifier!r} in "
            f"{record.account} on {record.trade_date.isoformat()}"
        )
        if not targets:
            raise ValidationError(
                f"{where} has no income row to attach to. A withholding line "
                f"belongs to a same-day income row on the same instrument; add "
                f"the missing row to the history, or map this one as an event of "
                f"its own",
                code=E_IMPORT_SOURCE_INVALID,
                row=line.index,
            )
        if len(targets) > 1:
            raise ValidationError(
                f"{where} could attach to rows "
                + ", ".join(str(indices[i]) for i, _ in targets)
                + ". Two income rows on one day for one instrument leave nothing to "
                "decide by; nothing here guesses",
                code=E_IMPORT_SOURCE_INVALID,
                row=line.index,
                candidates=[indices[i] for i, _ in targets],
            )
        i, target = targets[0]
        withheld = abs(record.amount)
        if withheld == Decimal("0"):
            raise ValidationError(
                f"{where} withholds nothing. Check the `cash` convention on the rule",
                code=E_IMPORT_SOURCE_INVALID,
                row=line.index,
            )
        result[i] = MappedTransaction(
            record=target.record,
            rule=f"{target.rule} (+ withholding from row {line.index})",
            txn_type=target.txn_type,
            fee_class=target.fee_class,
            counter_account=target.counter_account,
            taxes_withheld=target.taxes_withheld + withheld,
        )
        result[position[line.index]] = MappedTransaction(
            record=record,
            rule=f"{line.rule.label} (attached to row {indices[i]})",
            reason=(
                f"withholding on the income recorded on row {indices[i]}; carried "
                f"there as taxes_withheld, not as a fee (PORT-GIPS-A06)"
            ),
        )
    return result


def _apply(sign: Sign, value: Any, index: int, field: str) -> Decimal | None:
    if sign is Sign.NONE:
        return None
    if value is None:
        raise _missing(index, field, f"an activity mapped with {field} = {sign.value!r}")
    if not isinstance(value, Decimal):  # pragma: no cover -- parsing guarantees it
        raise _missing(index, field, "a transaction")
    if sign is Sign.AS_STATED:
        return value
    if sign is Sign.INVERTED:
        return -value
    magnitude = abs(value)
    return magnitude if sign is Sign.POSITIVE else -magnitude


def _required(row: dict[str, Any], field: str, index: int) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise _missing(index, field, "a row")
    return value


def _missing(index: int, field: str, what: str) -> ValidationError:
    return ValidationError(
        f"row {index}: {what} has no {field}. A blank is not a zero and the "
        f"import will not choose one for you",
        code=E_IMPORT_COLUMN_MISSING,
        row=index,
        column=field,
    )


def _verbatim(raw: dict[str, Any]) -> dict[str, str]:
    """The source row, unparsed, for the batch file a person reviews."""
    return {
        str(key): "" if value is None else str(value)
        for key, value in raw.items()
        if key is not None
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()
