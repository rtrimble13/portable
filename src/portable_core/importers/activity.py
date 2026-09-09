"""`activity_map.toml` -- the custodian's vocabulary, mapped once, as data.

ADR 0018 §4. The activity vocabulary is the highest-risk part of an import and
belongs in the most reviewable place. "DIV" means a dividend at one custodian
and a distribution that is partly return of capital at another; "Journal" is a
transfer at one and a fee settlement at a third. Getting one of those wrong is
not a crash -- it is a plausible number in a tax report.

So: **no default arm.** An activity string the map does not name stops the
import and quotes the row. A default that guessed `TransactionType.OTHER` would
turn the one failure mode this repository exists to prevent into a warning
nobody reads.

Two further refusals happen at *load* time rather than row time, because a map
is reviewed once and used a hundred thousand times:

- A rule mapping to ``fee`` with no ``fee_class`` is refused. ``fee_class`` is a
  stored fact decided when the fee is recorded, never inferred at report time
  (``PORT-GIPS-D01``), and the three return bases are derived from it.
- Two rules matching one string are refused rather than resolved by order. A
  map where the answer depends on line order is a map somebody will reorder.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from portable_core.domain.enums import FeeClass, TransactionType
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_ACTIVITY_UNMAPPED, E_IMPORT_SOURCE_INVALID

__all__ = ["ActivityMap", "ActivityRule", "Sign", "load_activity_map"]


class Sign(StrEnum):
    """How to read a numeric column for one activity.

    Custodians are not consistent even with themselves. Some sign the amount
    column; some state a magnitude and put the direction in the activity string;
    some use accounting parentheses on one report and a minus on another. The
    map declares which, per activity, and the adapter normalises to the
    canonical convention in ``records.py``.
    """

    #: The column's own sign is authoritative. Use only where it was checked.
    AS_STATED = "as_stated"
    #: A magnitude that always means an increase for this activity.
    POSITIVE = "positive"
    #: A magnitude that always means a decrease for this activity.
    NEGATIVE = "negative"
    #: This activity carries no value in this column at all. Not zero.
    NONE = "none"


_SIGNS = frozenset(s.value for s in Sign)


@dataclass(frozen=True, slots=True)
class ActivityRule:
    """One custodian activity string and everything that follows from it."""

    #: The custodian's string, as written in the file.
    match: str
    txn_type: TransactionType | None
    quantity: Sign
    cash: Sign
    fee_class: FeeClass | None = None
    #: A row this custodian emits that must not become a ledger row -- a
    #: position-only memo line, a duplicated summary row. Skipping is a
    #: decision with a reason attached, not a silence.
    skip: bool = False
    reason: str | None = None
    note: str | None = None

    def apply_quantity(self, value: Decimal | None) -> Decimal | None:
        return _signed(value, self.quantity)

    def apply_cash(self, value: Decimal | None) -> Decimal | None:
        return _signed(value, self.cash)


def _signed(value: Decimal | None, sign: Sign) -> Decimal | None:
    if sign is Sign.NONE or value is None:
        return None
    if sign is Sign.AS_STATED:
        return value
    magnitude = abs(value)
    return magnitude if sign is Sign.POSITIVE else -magnitude


@dataclass(frozen=True, slots=True)
class ActivityMap:
    """Every activity string the custodian emits, and no others."""

    rules: tuple[ActivityRule, ...]
    path: Path | None = None

    def rule_for(self, activity: str, *, row: int | None = None) -> ActivityRule:
        """The rule for one activity string, or a refusal naming the row.

        Matching folds case and collapses internal whitespace, because a
        custodian that writes "Dividend Received" in one export and "DIVIDEND
        RECEIVED" in the next has not changed its vocabulary. It does not do
        anything cleverer than that: a prefix or fuzzy match would let a new
        activity string silently inherit an old string's meaning, which is the
        failure this file exists to prevent.
        """
        key = _key(activity)
        for rule in self.rules:
            if _key(rule.match) == key:
                return rule
        where = f" (row {row})" if row is not None else ""
        raise ValidationError(
            f"unmapped activity {activity!r}{where}. Add it to the activity map, "
            f"or the import would have to guess what it means",
            code=E_ACTIVITY_UNMAPPED,
            activity=activity,
            row=row,
            mapped=[rule.match for rule in self.rules],
            path=str(self.path) if self.path else None,
        )

    def types(self) -> frozenset[TransactionType]:
        """Every transaction type the map can produce.

        Read by the capability checks: whether a custodian's history contains
        corporate actions or external flows is a fact about its *vocabulary*,
        not about any column.
        """
        return frozenset(r.txn_type for r in self.rules if r.txn_type is not None)


def _key(activity: str) -> str:
    return " ".join(activity.split()).casefold()


def load_activity_map(path: Path) -> ActivityMap:
    """Parse and fully validate an activity map. Every refusal is at load."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(
            f"no activity map at {path}", code=E_IMPORT_SOURCE_INVALID, path=str(path)
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(
            f"{path} is not valid TOML: {exc}",
            code=E_IMPORT_SOURCE_INVALID,
            path=str(path),
        ) from exc

    entries = raw.get("activity")
    if not isinstance(entries, list) or not entries:
        raise _invalid(path, "expected at least one [[activity]] table")

    rules: list[ActivityRule] = []
    seen: dict[str, str] = {}
    for position, entry in enumerate(entries):
        rule = _rule(path, position, entry)
        key = _key(rule.match)
        if key in seen:
            raise _invalid(
                path,
                f"activity {rule.match!r} is mapped twice (also as {seen[key]!r}). "
                f"Two rules for one string would make the result depend on file "
                f"order",
            )
        seen[key] = rule.match
        rules.append(rule)
    return ActivityMap(rules=tuple(rules), path=path)


def _rule(path: Path, position: int, entry: Any) -> ActivityRule:
    where = f"[[activity]] #{position + 1}"
    if not isinstance(entry, dict):
        raise _invalid(path, f"{where} is not a table")

    match = entry.get("match")
    if not isinstance(match, str) or not match.strip():
        raise _invalid(path, f"{where} needs a non-empty `match`")
    where = f"activity {match!r}"

    skip = bool(entry.get("skip", False))
    reason = entry.get("reason")
    if skip and not isinstance(reason, str):
        raise _invalid(
            path,
            f"{where} is skipped with no `reason`. A row deliberately kept out "
            f"of the ledger has to say why, or the next reader cannot tell a "
            f"decision from an oversight",
        )

    txn_type: TransactionType | None = None
    if not skip:
        raw_type = entry.get("txn_type")
        if not isinstance(raw_type, str):
            raise _invalid(path, f"{where} needs a `txn_type` (or `skip = true`)")
        try:
            txn_type = TransactionType(raw_type)
        except ValueError:
            raise _invalid(
                path,
                f"{where} maps to unknown txn_type {raw_type!r}. Known types: "
                + ", ".join(sorted(t.value for t in TransactionType)),
            ) from None

    fee_class: FeeClass | None = None
    raw_fee = entry.get("fee_class")
    if raw_fee is not None:
        if not isinstance(raw_fee, str):
            raise _invalid(path, f"{where} has a non-string `fee_class`")
        try:
            fee_class = FeeClass(raw_fee)
        except ValueError:
            raise _invalid(
                path,
                f"{where} has unknown fee_class {raw_fee!r}. Known classes: "
                + ", ".join(sorted(f.value for f in FeeClass)),
            ) from None
    if txn_type is TransactionType.FEE and fee_class is None:
        raise _invalid(
            path,
            f"{where} maps to a fee with no `fee_class`. The three return bases "
            f"are derived from it (PORT-GIPS-D01) and it is a stored fact, never "
            f"an inference at report time",
        )
    if fee_class is not None and txn_type is not TransactionType.FEE:
        raise _invalid(
            path,
            f"{where} sets a `fee_class` but is not a fee. A fee class on "
            f"anything else would be carried into the ledger and read later as "
            f"if it meant something",
        )

    return ActivityRule(
        match=match,
        txn_type=txn_type,
        quantity=_sign(path, where, entry.get("quantity", "none"), "quantity"),
        cash=_sign(path, where, entry.get("cash", "as_stated"), "cash"),
        fee_class=fee_class,
        skip=skip,
        reason=reason if isinstance(reason, str) else None,
        note=entry.get("note") if isinstance(entry.get("note"), str) else None,
    )


def _sign(path: Path, where: str, value: Any, field: str) -> Sign:
    if not isinstance(value, str) or value not in _SIGNS:
        raise _invalid(
            path,
            f"{where} has an invalid `{field}` convention {value!r}. One of: "
            + ", ".join(sorted(_SIGNS)),
        )
    return Sign(value)


def _invalid(path: Path, message: str) -> ValidationError:
    return ValidationError(
        f"{path.name}: {message}", code=E_IMPORT_SOURCE_INVALID, path=str(path)
    )
