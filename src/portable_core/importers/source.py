"""`source.toml` -- which file is which, and how to read what is in it.

ADR 0018 §4. Together with `activity_map.toml` this is the whole of a custodian
adapter for the common case: *a custodian whose exports are plain tabular files
needs no Python at all.* That is the point of the design, and the reason both
files are validated exhaustively at load rather than lazily at first use --
a mapping file is reviewed once and used for every row thereafter, so the review
has to be able to see everything that could be wrong with it.

The spec carries four things:

- **Documents.** Which file holds the holdings snapshot, which holds the
  transaction history, and the column name for each canonical field. Both
  documents are required and an import refuses without them (ADR 0018 §1).
- **Formats.** Date patterns, thousands and decimal separators, whether
  negatives are parenthesised, which strings mean "blank". Not guessed: a
  ``01/02/2026`` read under the wrong pattern is a silently wrong number with a
  date attached.
- **Cash equivalents, per account.** ADR 0013 generalised: sweep vehicles are
  cash, and *which identifiers are sweep vehicles* is a declared set per
  account, not two hard-coded tickers.
- **Capability checks.** Which optional inputs the adapter claims, and the named
  check on the data that justifies each (ADR 0018 §3).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_IMPORT_SOURCE_INVALID
from portable_core.importers.capabilities import ImportCapability

__all__ = [
    "HOLDINGS",
    "OPTIONAL_COLUMNS",
    "REQUIRED_COLUMNS",
    "TRANSACTIONS",
    "CapabilityCheck",
    "DocumentSpec",
    "NumberFormat",
    "SourceSpec",
    "load_source",
]

HOLDINGS: Final = "holdings"
TRANSACTIONS: Final = "transactions"

#: The minimum dataset, as columns. ADR 0018 §1 states it as two documents;
#: this is the same statement at the field level, and it is what refuses.
REQUIRED_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    HOLDINGS: ("account", "identifier", "quantity"),
    TRANSACTIONS: ("trade_date", "account", "activity", "amount"),
}

#: Everything else a document may carry. Most map one-to-one onto a capability.
OPTIONAL_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    HOLDINGS: ("as_of", "market_value", "cost_basis", "acquired", "lot_id"),
    TRANSACTIONS: ("identifier", "quantity", "settlement_date", "external_id", "note"),
}


@dataclass(frozen=True, slots=True)
class NumberFormat:
    """How this custodian writes numbers and dates.

    Declared rather than sniffed. Sniffing gets ``1.234`` wrong exactly once, on
    a European export, and the result is a position off by a factor of a
    thousand that still reconciles to itself.
    """

    #: `strptime` patterns, tried in order. A value matching none is refused.
    dates: tuple[str, ...] = ("%Y-%m-%d",)
    thousands: str = ","
    decimal_point: str = "."
    #: `(1,234.00)` for a negative, as accounting exports write it.
    parentheses_negative: bool = True
    #: Stripped before parsing, so `$1,234.00` and `1,234.00 USD` both work.
    strip: tuple[str, ...] = ("$", "USD")
    #: Strings that mean "the custodian said nothing here". Never zero.
    blanks: tuple[str, ...] = ("", "-", "--", "N/A", "n/a", "NULL", "None")
    encoding: str = "utf-8-sig"

    def is_blank(self, text: str) -> bool:
        return text.strip() in self.blanks

    def number(self, text: str, *, what: str, row: int) -> Decimal:
        """Parse one numeric cell, or refuse naming the row and the cell."""
        cleaned = text.strip()
        for token in self.strip:
            cleaned = cleaned.replace(token, "")
        cleaned = cleaned.strip()
        negative = False
        if self.parentheses_negative and cleaned.startswith("(") and cleaned.endswith(")"):
            negative, cleaned = True, cleaned[1:-1].strip()
        if self.thousands:
            cleaned = cleaned.replace(self.thousands, "")
        if self.decimal_point != ".":
            cleaned = cleaned.replace(self.decimal_point, ".")
        cleaned = cleaned.replace(" ", "")
        try:
            value = Decimal(cleaned)
        except InvalidOperation:
            raise ValidationError(
                f"row {row}: {what} is {text!r}, which is not a number in this "
                f"custodian's declared format",
                code=E_IMPORT_SOURCE_INVALID,
                row=row,
                column=what,
                value=text,
            ) from None
        if not value.is_finite():
            raise ValidationError(
                f"row {row}: {what} is {text!r}, which is not a finite number",
                code=E_IMPORT_SOURCE_INVALID,
                row=row,
                column=what,
                value=text,
            )
        return -value if negative else value

    def date(self, text: str, *, what: str, row: int) -> date:
        """Parse one date cell against the declared patterns, in order."""
        cleaned = text.strip()
        for pattern in self.dates:
            try:
                # A trade date, not a timestamp: a custodian's export states
                # the day, and attaching a timezone to it would invent one.
                return datetime.strptime(cleaned, pattern).date()  # noqa: DTZ007
            except ValueError:
                continue
        raise ValidationError(
            f"row {row}: {what} is {text!r}, which matches none of this "
            f"custodian's declared date formats ({', '.join(self.dates)})",
            code=E_IMPORT_SOURCE_INVALID,
            row=row,
            column=what,
            value=text,
            formats=list(self.dates),
        )


@dataclass(frozen=True, slots=True)
class DocumentSpec:
    """One of the two required documents, and how to read its columns."""

    kind: str
    file: str
    #: canonical field name -> the custodian's column header.
    columns: dict[str, str]
    #: For a snapshot whose as-of date is in a page header rather than a column.
    as_of: date | None = None
    #: The crosswalk file, where this document's identifiers are security
    #: *names* rather than symbols (``INSTRUMENT_SYMBOL`` absent). Every
    #: identifier in the document is then resolved through it, and a name it
    #: does not carry is a refusal. Relative to the adapter directory.
    crosswalk: str | None = None
    #: Accounts the custodian's snapshot genuinely omits a cash line for. A
    #: declared exception, recorded in the file, rather than a silent pass:
    #: cash on the snapshot is required precisely because cash reconciliation
    #: is the only check that catches a sign error or a dropped row.
    allow_missing_cash: tuple[str, ...] = ()

    def column(self, field_name: str) -> str | None:
        return self.columns.get(field_name)


@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    """A capability the adapter claims, and the check on data that earns it."""

    capability: ImportCapability
    check: str
    document: str
    #: The canonical field the check reads, where the check reads one.
    field: str | None = None
    #: For ratio checks: the share of examined rows that must satisfy it.
    min_ratio: Decimal = Decimal("1")
    #: For `history_since`: the date the history must reach back to.
    since: date | None = None
    #: For `activity_covers`: the transaction types the map must name.
    types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """A parsed, validated `source.toml`."""

    broker: str
    name: str
    documents: dict[str, DocumentSpec]
    numbers: NumberFormat
    #: account name -> identifiers that are cash for that account. The empty
    #: string keys the default set (ADR 0013, generalised by ADR 0018).
    cash_equivalents: dict[str, frozenset[str]] = field(default_factory=dict)
    checks: tuple[CapabilityCheck, ...] = ()
    root: Path | None = None
    note: str | None = None

    def document(self, kind: str) -> DocumentSpec:
        return self.documents[kind]

    def is_cash_equivalent(self, account: str, identifier: str) -> bool:
        folded = identifier.strip().casefold()
        for key in (account, ""):
            declared = self.cash_equivalents.get(key)
            if declared and folded in declared:
                return True
        return False


def load_source(path: Path) -> SourceSpec:
    """Parse and fully validate a `source.toml`.

    Accepts either the file itself or the directory holding it, because an
    adapter is a directory and asking a user to type its one fixed filename is
    a papercut with no upside.
    """
    root = path if path.is_dir() else path.parent
    spec_path = path / "source.toml" if path.is_dir() else path
    try:
        raw = tomllib.loads(spec_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(
            f"no source.toml at {spec_path}",
            code=E_IMPORT_SOURCE_INVALID,
            path=str(spec_path),
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(
            f"{spec_path} is not valid TOML: {exc}",
            code=E_IMPORT_SOURCE_INVALID,
            path=str(spec_path),
        ) from exc

    broker = raw.get("broker")
    if not isinstance(broker, str) or not broker.strip():
        raise _invalid(spec_path, "needs a non-empty `broker`")
    stated_name = raw.get("name")
    name = stated_name if isinstance(stated_name, str) else broker

    documents = _documents(spec_path, raw.get("documents"))
    numbers = _numbers(spec_path, raw.get("format", {}))
    cash = _cash_equivalents(spec_path, raw.get("cash_equivalents", {}))
    checks = _checks(spec_path, raw.get("capability", []), documents)

    return SourceSpec(
        broker=broker,
        name=name,
        documents=documents,
        numbers=numbers,
        cash_equivalents=cash,
        checks=checks,
        root=root,
        note=raw.get("note") if isinstance(raw.get("note"), str) else None,
    )


def _documents(path: Path, raw: Any) -> dict[str, DocumentSpec]:
    if not isinstance(raw, dict):
        raise _invalid(path, "needs a [documents] table")
    missing = [kind for kind in (HOLDINGS, TRANSACTIONS) if kind not in raw]
    if missing:
        raise _invalid(
            path,
            f"declares no {' and no '.join(missing)} document. Both are required: "
            f"the snapshot is the reconciliation anchor and the history is the "
            f"ledger, and neither substitutes for the other (ADR 0018 §1)",
        )
    unknown = sorted(set(raw) - {HOLDINGS, TRANSACTIONS})
    if unknown:
        raise _invalid(
            path,
            f"declares unknown document(s) {', '.join(unknown)}. The adapter reads exactly two",
        )
    return {kind: _document(path, kind, raw[kind]) for kind in (HOLDINGS, TRANSACTIONS)}


def _document(path: Path, kind: str, raw: Any) -> DocumentSpec:
    if not isinstance(raw, dict):
        raise _invalid(path, f"[documents.{kind}] is not a table")
    file_name = raw.get("file")
    if not isinstance(file_name, str) or not file_name.strip():
        raise _invalid(path, f"[documents.{kind}] needs a `file`")

    columns = raw.get("columns")
    if not isinstance(columns, dict):
        raise _invalid(path, f"[documents.{kind}.columns] is missing")
    known = set(REQUIRED_COLUMNS[kind]) | set(OPTIONAL_COLUMNS[kind])
    unknown = sorted(set(columns) - known)
    if unknown:
        raise _invalid(
            path,
            f"[documents.{kind}.columns] maps unknown field(s) "
            f"{', '.join(unknown)}. Known fields: {', '.join(sorted(known))}",
        )
    for field_name, header in columns.items():
        if not isinstance(header, str) or not header.strip():
            raise _invalid(path, f"[documents.{kind}.columns] {field_name} has no column name")

    as_of = None
    if kind == HOLDINGS:
        stated = raw.get("as_of")
        if stated is not None:
            as_of = _as_of(path, stated)
        if "as_of" not in columns and as_of is None:
            raise _invalid(
                path,
                "[documents.holdings] states no as-of date: map an `as_of` column "
                "or set `as_of` here. A snapshot with no date cannot anchor a "
                "reconstruction and cannot be reconciled against anything",
            )

    missing = [f for f in REQUIRED_COLUMNS[kind] if f not in columns]
    if missing:
        raise _invalid(
            path,
            f"[documents.{kind}.columns] is missing required field(s) {', '.join(missing)}",
        )
    crosswalk = raw.get("crosswalk")
    if crosswalk is not None and (not isinstance(crosswalk, str) or not crosswalk.strip()):
        raise _invalid(path, f"[documents.{kind}] `crosswalk` must name a file")
    if crosswalk is not None and "identifier" not in columns:
        raise _invalid(
            path,
            f"[documents.{kind}] declares a crosswalk but maps no `identifier` "
            f"column, so there is nothing to resolve through it",
        )
    return DocumentSpec(
        kind=kind,
        file=file_name,
        columns=dict(columns),
        as_of=as_of,
        crosswalk=crosswalk,
        allow_missing_cash=_strings(path, raw.get("allow_missing_cash", [])),
    )


def _as_of(path: Path, stated: Any) -> date:
    if isinstance(stated, date):
        return stated
    if isinstance(stated, str):
        try:
            return date.fromisoformat(stated)
        except ValueError:
            pass
    raise _invalid(path, f"[documents.holdings] `as_of` is {stated!r}, not an ISO date")


def _numbers(path: Path, raw: Any) -> NumberFormat:
    if not isinstance(raw, dict):
        raise _invalid(path, "[format] is not a table")
    defaults = NumberFormat()
    dates = _strings(path, raw.get("dates", list(defaults.dates)))
    if not dates:
        raise _invalid(path, "[format] `dates` is empty; declare at least one pattern")
    for pattern in dates:
        _check_pattern(path, pattern)
    return NumberFormat(
        dates=dates,
        thousands=_text(path, raw, "thousands", defaults.thousands),
        decimal_point=_text(path, raw, "decimal_point", defaults.decimal_point),
        parentheses_negative=bool(
            raw.get("parentheses_negative", defaults.parentheses_negative)
        ),
        strip=_strings(path, raw.get("strip", list(defaults.strip))),
        blanks=_strings(path, raw.get("blanks", list(defaults.blanks))),
        encoding=_text(path, raw, "encoding", defaults.encoding),
    )


#: A date every pattern must round-trip exactly. Chosen so that the day, the
#: month and a two-digit year are all distinguishable from one another.
_REFERENCE_DATE: Final = date(2026, 3, 17)


def _check_pattern(path: Path, pattern: str) -> None:
    """A date pattern must carry a whole date, and must be checked at load.

    Two failures, both otherwise silent. A bogus directive (``%Q``) would not
    surface until the first row that used it -- six thousand rows into an
    import rather than during the review. And a pattern carrying only *part* of
    a date (``%Y-%m``) parses without complaint and quietly returns the first
    of the month, which is a wrong trade date rather than an error.

    Round-tripping a known date catches both: format it, parse it back, and
    require the same date out.

    One caveat worth knowing: ``%b`` and ``%B`` read month *names*, and
    `strptime` reads those in the process locale. A custodian writing
    "17-Mar-2026" will fail on a machine whose locale spells March differently
    -- loudly, so not a silently wrong number, but prefer a numeric pattern
    wherever the custodian offers one.
    """
    try:
        formatted = _REFERENCE_DATE.strftime(pattern)
        recovered = datetime.strptime(formatted, pattern).date()  # noqa: DTZ007
    except ValueError as exc:
        raise _invalid(
            path, f"[format] `dates` pattern {pattern!r} is not a valid pattern: {exc}"
        ) from None
    if recovered != _REFERENCE_DATE:
        raise _invalid(
            path,
            f"[format] `dates` pattern {pattern!r} does not carry a whole date: "
            f"{_REFERENCE_DATE.isoformat()} writes as {formatted!r} and reads back "
            f"as {recovered.isoformat()}. A partial pattern parses without "
            f"complaint and silently invents the missing part",
        )


def _cash_equivalents(path: Path, raw: Any) -> dict[str, frozenset[str]]:
    if not isinstance(raw, dict):
        raise _invalid(path, "[cash_equivalents] is not a table")
    declared: dict[str, frozenset[str]] = {}
    for account, identifiers in raw.items():
        key = "" if account == "default" else account
        declared[key] = frozenset(i.strip().casefold() for i in _strings(path, identifiers))
    return declared


def _checks(
    path: Path, raw: Any, documents: dict[str, DocumentSpec]
) -> tuple[CapabilityCheck, ...]:
    if not isinstance(raw, list):
        raise _invalid(path, "[[capability]] entries must be a list of tables")
    checks: list[CapabilityCheck] = []
    seen: set[ImportCapability] = set()
    for position, entry in enumerate(raw):
        where = f"[[capability]] #{position + 1}"
        if not isinstance(entry, dict):
            raise _invalid(path, f"{where} is not a table")
        raw_name = entry.get("name")
        try:
            capability = ImportCapability(str(raw_name))
        except ValueError:
            raise _invalid(
                path,
                f"{where} names unknown capability {raw_name!r}. Known: "
                + ", ".join(sorted(c.value for c in ImportCapability)),
            ) from None
        if capability in seen:
            raise _invalid(path, f"{where} declares {capability.value} twice")
        seen.add(capability)

        check = entry.get("check")
        if not isinstance(check, str) or not check:
            raise _invalid(path, f"{where} needs a named `check`")
        document = entry.get("document", TRANSACTIONS)
        if document not in documents:
            raise _invalid(path, f"{where} reads document {document!r}, which is not declared")
        field_name = entry.get("field")
        if field_name is not None and not isinstance(field_name, str):
            raise _invalid(path, f"{where} has a non-string `field`")

        min_ratio = Decimal("1")
        if "min_ratio" in entry:
            try:
                min_ratio = Decimal(str(entry["min_ratio"]))
            except InvalidOperation:
                raise _invalid(path, f"{where} has a non-numeric `min_ratio`") from None
            if not (Decimal(0) < min_ratio <= Decimal(1)):
                raise _invalid(path, f"{where} `min_ratio` must be in (0, 1]")

        since = _as_of(path, entry["since"]) if "since" in entry else None
        checks.append(
            CapabilityCheck(
                capability=capability,
                check=check,
                document=document,
                field=field_name,
                min_ratio=min_ratio,
                since=since,
                types=_strings(path, entry.get("types", [])),
            )
        )
    return tuple(checks)


def _strings(path: Path, raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list) or any(not isinstance(v, str) for v in raw):
        raise _invalid(path, f"expected a list of strings, got {raw!r}")
    return tuple(raw)


def _text(path: Path, raw: dict[str, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise _invalid(path, f"[format] `{key}` must be a string")
    return value


def _invalid(path: Path, message: str) -> ValidationError:
    return ValidationError(
        f"{path.name}: {message}", code=E_IMPORT_SOURCE_INVALID, path=str(path)
    )
