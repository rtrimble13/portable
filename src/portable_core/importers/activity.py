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

One activity word frequently names several events -- the reference custodian
writes *Credit* for a symbol change and for a share-class conversion, and
*Expense* for a fee, for the transfer that funds another account's fee, and for
a withdrawal -- and the only thing that tells them apart is the row's note. A
rule may therefore carry a ``note`` pattern as a second key. The no-default-arm
principle holds one level down: once any rule for an activity is keyed on a
note, every rule for that activity must be, so that a row matching none of the
patterns is a refusal rather than a fall-through.

Two further refusals happen at *load* time rather than row time, because a map
is reviewed once and used a hundred thousand times:

- A rule mapping to ``fee`` with no ``fee_class`` is refused. ``fee_class`` is a
  stored fact decided when the fee is recorded, never inferred at report time
  (``PORT-GIPS-D01``), and the three return bases are derived from it.
- Two rules matching one string are refused rather than resolved by order. A
  map where the answer depends on line order is a map somebody will reorder.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from portable_core.domain.enums import FeeClass, TransactionType
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_ACTIVITY_UNMAPPED, E_IMPORT_SOURCE_INVALID

__all__ = ["ActivityMap", "ActivityRule", "PairSpec", "Sign", "load_activity_map"]


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
    #: The column's sign is authoritative and backwards: the custodian signs
    #: from its own side of the ledger, so a positive is money *out*. ADR 0014's
    #: transfer-to-cover rows are the reference case -- positive in the paying
    #: account, negative in the receiving one, and unsigned everywhere else.
    INVERTED = "inverted"
    #: A magnitude that always means an increase for this activity.
    POSITIVE = "positive"
    #: A magnitude that always means a decrease for this activity.
    NEGATIVE = "negative"
    #: This activity carries no value in this column at all. Not zero.
    NONE = "none"


_SIGNS = frozenset(s.value for s in Sign)

#: The identifier classes a rule may be keyed on. ADR 0013 generalised: a
#: sweep vehicle is cash, and whether a row names one changes what the row
#: means -- a reinvested distribution into the sweep is income, into a fund
#: it is income and a lot.
_IDENTIFIER_CLASSES = frozenset({"cash_equivalents", "securities"})

#: What a row may attach to another row as, instead of becoming a row.
_ATTACHMENTS = frozenset({"taxes_withheld"})

#: What an unpaired transfer leg may become. Direction is checked against the
#: leg: an outbound leg can only fall back to a withdrawal, an inbound one only
#: to a deposit, because the alternative is a flow pointing the wrong way.
_UNPAIRED_OUT = frozenset({TransactionType.WITHDRAWAL})
_UNPAIRED_IN = frozenset({TransactionType.DEPOSIT})


@dataclass(frozen=True, slots=True)
class PairSpec:
    """How the two legs of one internal transfer find each other. ADR 0014.

    A custodian that reports both sides of a journal -- once in the paying
    account, once in the receiving one -- has reported one event twice. Recorded
    twice it is two external flows at portfolio level, which rewrites the track
    record with money that never left (ADR 0007). The pairing rule collapses the
    two rows into one ``transfer`` with a counter account.

    Legs pair on the same trade date, the same magnitude, opposite directions,
    and different accounts. ``counterpart`` narrows that, where the note names
    the other account: two IRAs funded with the same amount on the same day are
    otherwise indistinguishable, and a guess is exactly what is refused.
    """

    #: A regex over the note with a named group ``account``; where it matches,
    #: the leg pairs only with that account. Optional. The captured text is
    #: resolved through the source's account aliases, where declared, so a
    #: custodian that names accounts by number in the note is still expressible
    #: without the number.
    counterpart: re.Pattern[str] | None = None
    #: How many days apart the two legs may be dated. Zero means the same day.
    #: The reference custodian dates the receiving leg three days before the
    #: paying one in most quarters, and a window that is too wide is refused
    #: only by ambiguity -- so declare the narrowest one the data needs.
    window_days: int = 0
    #: What an outbound leg becomes when its counterpart is outside the
    #: portfolio. ``None`` means an unpaired outbound leg is a refusal.
    unpaired_out: TransactionType | None = None
    #: What an inbound leg becomes when its counterpart is outside the
    #: portfolio. ``None`` means an unpaired inbound leg is a refusal.
    unpaired_in: TransactionType | None = None

    def named_counterpart(self, note: str | None) -> str | None:
        """The account the note names as the other side, if the pattern says."""
        if self.counterpart is None or note is None:
            return None
        found = self.counterpart.search(note)
        if found is None:
            return None
        try:
            named = found.group("account")
        except IndexError:
            return None
        return named.strip() if isinstance(named, str) and named.strip() else None


@dataclass(frozen=True, slots=True)
class ActivityRule:
    """One custodian activity string and everything that follows from it."""

    #: The custodian's string, as written in the file.
    match: str
    txn_type: TransactionType | None
    quantity: Sign
    cash: Sign
    #: How to read the amount column as the event's *value* where the event
    #: moves no cash: a distribution reinvested into units, securities received
    #: in kind. ``cash`` says what the account's balance did; ``value`` says
    #: what the event was worth. Both read the same column, and a rule that
    #: sets both to something other than ``none`` is refused, because one
    #: column cannot be two different facts.
    value: Sign = Sign.NONE
    fee_class: FeeClass | None = None
    #: A row this custodian emits that must not become a ledger row -- a
    #: position-only memo line, a duplicated summary row. Skipping is a
    #: decision with a reason attached, not a silence.
    skip: bool = False
    reason: str | None = None
    #: The second key. Where set, the rule applies only to rows whose note the
    #: pattern matches, and every other rule for this activity must carry one
    #: too. Searched case-insensitively.
    note_pattern: re.Pattern[str] | None = None
    #: The third key, ADR 0013 generalised: ``"cash_equivalents"`` or
    #: ``"securities"``. Where set, the rule applies only to rows whose
    #: identifier is, or is not, in the account's declared cash-equivalent set
    #: -- and every other rule for this activity and note must carry one too. A
    #: class no rule names is a refusal. This is what makes a sweep drop a
    #: whitelist rather than a blanket: the one thing that must never happen
    #: is a real movement discarded by a rule written for bookkeeping noise.
    identifiers: str | None = None
    #: ADR 0014. Present on, and only on, a ``transfer``.
    pair: PairSpec | None = None
    #: A row that is not an event but a fact about another row: a withholding
    #: line that belongs on the same-day income row as ``taxes_withheld``
    #: (PORT-GIPS-A06 -- withholding is tax, not a fee). The row's amount is
    #: attached and the row itself is carried as a skip naming its target.
    attach: str | None = None

    @property
    def label(self) -> str:
        """The rule as a batch names it: the activity and its keys, if any."""
        keys = [
            k
            for k in (
                self.note_pattern.pattern if self.note_pattern else None,
                self.identifiers,
            )
            if k
        ]
        if not keys:
            return f"activity:{self.match}"
        return f"activity:{self.match} [{', '.join(keys)}]"

    def apply_quantity(self, value: Decimal | None) -> Decimal | None:
        return _signed(value, self.quantity)

    def apply_cash(self, value: Decimal | None) -> Decimal | None:
        return _signed(value, self.cash)

    def apply_value(self, value: Decimal | None) -> Decimal | None:
        return _signed(value, self.value)


def _signed(value: Decimal | None, sign: Sign) -> Decimal | None:
    if sign is Sign.NONE or value is None:
        return None
    if sign is Sign.AS_STATED:
        return value
    if sign is Sign.INVERTED:
        return -value
    magnitude = abs(value)
    return magnitude if sign is Sign.POSITIVE else -magnitude


@dataclass(frozen=True, slots=True)
class ActivityMap:
    """Every activity string the custodian emits, and no others."""

    rules: tuple[ActivityRule, ...]
    path: Path | None = None

    def rule_for(
        self,
        activity: str,
        *,
        note: str | None = None,
        cash_equivalent: bool | None = None,
        row: int | None = None,
    ) -> ActivityRule:
        """The rule for one activity string, or a refusal naming the row.

        Matching folds case and collapses internal whitespace, because a
        custodian that writes "Dividend Received" in one export and "DIVIDEND
        RECEIVED" in the next has not changed its vocabulary. It does not do
        anything cleverer than that: a prefix or fuzzy match would let a new
        activity string silently inherit an old string's meaning, which is the
        failure this file exists to prevent.

        Where the activity's rules are keyed on a note pattern, exactly one
        must match the row's note. None is a refusal naming the patterns that
        were tried; more than one is a refusal naming the map, because two
        patterns that both match one row is an ambiguity in the file, not in
        the data. Where they are keyed on the identifier class, the row's
        identifier decides, and ``cash_equivalent`` says which class it is in
        (``None``: the row names no identifier, which no class-keyed rule
        accepts).
        """
        key = _key(activity)
        candidates = [rule for rule in self.rules if _key(rule.match) == key]
        where = f" (row {row})" if row is not None else ""
        if not candidates:
            raise ValidationError(
                f"unmapped activity {activity!r}{where}. Add it to the activity map, "
                f"or the import would have to guess what it means",
                code=E_ACTIVITY_UNMAPPED,
                activity=activity,
                row=row,
                mapped=list(dict.fromkeys(rule.match for rule in self.rules)),
                path=str(self.path) if self.path else None,
            )
        if candidates[0].note_pattern is not None:
            candidates = self._by_note(candidates, activity, note, where, row)
        if candidates[0].identifiers is not None:
            candidates = self._by_class(candidates, activity, cash_equivalent, where, row)
        # One rule, by construction: the loader refuses two rules with the
        # same activity, note pattern and identifier class.
        return candidates[0]

    def _by_note(
        self,
        candidates: list[ActivityRule],
        activity: str,
        note: str | None,
        where: str,
        row: int | None,
    ) -> list[ActivityRule]:
        """Narrow note-keyed candidates to the one whose pattern matches."""
        matched = [
            rule
            for rule in candidates
            if rule.note_pattern is not None and rule.note_pattern.search(note or "")
        ]
        patterns = list(
            dict.fromkeys(rule.note_pattern.pattern for rule in candidates if rule.note_pattern)
        )
        distinct = {rule.note_pattern.pattern for rule in matched if rule.note_pattern}
        if len(distinct) == 1:
            return matched
        if not matched:
            raise ValidationError(
                f"activity {activity!r}{where} with note {note!r} matches none of the "
                f"note patterns the map declares for it ({', '.join(map(repr, patterns))}). "
                f"Add a rule for it, or the import would have to guess which of the "
                f"existing ones applies",
                code=E_ACTIVITY_UNMAPPED,
                activity=activity,
                note=note,
                row=row,
                patterns=patterns,
                path=str(self.path) if self.path else None,
            )
        both = sorted(distinct)
        raise ValidationError(
            f"activity {activity!r}{where} with note {note!r} matches "
            f"{len(both)} note patterns ({', '.join(map(repr, both))}). Two rules "
            f"for one row would make the result depend on file order; narrow the "
            f"patterns until exactly one applies",
            code=E_IMPORT_SOURCE_INVALID,
            activity=activity,
            note=note,
            row=row,
            patterns=both,
            path=str(self.path) if self.path else None,
        )

    def _by_class(
        self,
        candidates: list[ActivityRule],
        activity: str,
        cash_equivalent: bool | None,
        where: str,
        row: int | None,
    ) -> list[ActivityRule]:
        """Narrow class-keyed candidates to the one for the row's identifier."""
        declared = sorted({rule.identifiers for rule in candidates if rule.identifiers})
        if cash_equivalent is None:
            raise ValidationError(
                f"activity {activity!r}{where} is keyed on the identifier class "
                f"({', '.join(declared)}) but the row names no identifier. A sweep "
                f"movement names its vehicle; a row that does not is something else",
                code=E_ACTIVITY_UNMAPPED,
                activity=activity,
                row=row,
                classes=declared,
                path=str(self.path) if self.path else None,
            )
        wanted = "cash_equivalents" if cash_equivalent else "securities"
        matched = [rule for rule in candidates if rule.identifiers == wanted]
        if matched:
            return matched
        raise ValidationError(
            f"activity {activity!r}{where} names "
            f"{'a cash-equivalent' if cash_equivalent else 'a security'} identifier, "
            f"and the map has no rule for that class of it (declared: "
            f"{', '.join(declared)}). The rule applies only to the class it names "
            f"(ADR 0013): a real movement must not be handled by a rule written for "
            f"bookkeeping noise. Map this row under a rule of its own, or declare "
            f"the identifier in [cash_equivalents] if it is genuinely cash",
            code=E_ACTIVITY_UNMAPPED,
            activity=activity,
            row=row,
            wanted=wanted,
            classes=declared,
            path=str(self.path) if self.path else None,
        )

    def types(self) -> frozenset[TransactionType]:
        """Every transaction type the map can produce.

        Read by the capability checks: whether a custodian's history contains
        corporate actions or external flows is a fact about its *vocabulary*,
        not about any column. A pairing rule's fallbacks count -- an unpaired
        leg that becomes a withdrawal is a withdrawal the map produces.
        """
        produced: set[TransactionType] = set()
        for rule in self.rules:
            if rule.txn_type is not None:
                produced.add(rule.txn_type)
            if rule.pair is not None:
                produced.update(
                    t for t in (rule.pair.unpaired_out, rule.pair.unpaired_in) if t is not None
                )
        return frozenset(produced)

    @property
    def uses_notes(self) -> bool:
        """Whether any rule reads the note: a second key, or a counterpart."""
        return any(
            rule.note_pattern is not None
            or (rule.pair is not None and rule.pair.counterpart is not None)
            for rule in self.rules
        )

    @property
    def uses_identifiers(self) -> bool:
        """Whether any rule keys on the identifier class, or attaches by it."""
        return any(
            rule.identifiers is not None or rule.attach is not None for rule in self.rules
        )


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
    seen: dict[tuple[str, str | None, str | None], str] = {}
    for position, entry in enumerate(entries):
        rule = _rule(path, position, entry)
        pattern = rule.note_pattern.pattern if rule.note_pattern is not None else None
        key = (
            _key(rule.match),
            _key(pattern) if pattern is not None else None,
            rule.identifiers,
        )
        if key in seen:
            raise _invalid(
                path,
                f"activity {rule.match!r} is mapped twice (also as {seen[key]!r}"
                + (f", both keyed on note {pattern!r}" if pattern else "")
                + (f", both for {rule.identifiers}" if rule.identifiers else "")
                + "). Two rules for one string would make the result depend on file "
                "order",
            )
        seen[key] = rule.match
        rules.append(rule)

    _check_keys_are_complete(path, rules)
    return ActivityMap(rules=tuple(rules), path=path)


def _check_keys_are_complete(path: Path, rules: list[ActivityRule]) -> None:
    """Once one rule for an activity carries a key, all of them must.

    A keyed rule beside an un-keyed rule for the same activity is a default
    arm one level down: the un-keyed rule would catch every row the keys
    miss, which is precisely the silent inheritance the map exists to refuse.
    The same holds for the identifier class within one note pattern.
    """
    by_activity: dict[str, list[ActivityRule]] = {}
    for rule in rules:
        by_activity.setdefault(_key(rule.match), []).append(rule)
    for group in by_activity.values():
        keyed = [r for r in group if r.note_pattern is not None]
        if keyed and len(keyed) != len(group):
            bare = next(r for r in group if r.note_pattern is None)
            raise _invalid(
                path,
                f"activity {bare.match!r} has a rule with no `note` pattern beside "
                f"{len(keyed)} that have one. Once an activity is keyed on the note, "
                f"every rule for it must be: an un-keyed rule would be a default "
                f"arm for every row the patterns miss",
            )
        by_note: dict[str | None, list[ActivityRule]] = {}
        for rule in group:
            pattern = rule.note_pattern.pattern if rule.note_pattern else None
            by_note.setdefault(_key(pattern) if pattern else None, []).append(rule)
        for siblings in by_note.values():
            classed = [r for r in siblings if r.identifiers is not None]
            if classed and len(classed) != len(siblings):
                bare = next(r for r in siblings if r.identifiers is None)
                raise _invalid(
                    path,
                    f"activity {bare.match!r} has a rule with no `identifiers` class "
                    f"beside one that has. Once a rule is keyed on the identifier "
                    f"class, its siblings must be: an un-keyed rule would be a "
                    f"default arm for the other class",
                )


def _rule(path: Path, position: int, entry: Any) -> ActivityRule:
    where = f"[[activity]] #{position + 1}"
    if not isinstance(entry, dict):
        raise _invalid(path, f"{where} is not a table")

    match = entry.get("match")
    if not isinstance(match, str) or not match.strip():
        raise _invalid(path, f"{where} needs a non-empty `match`")
    where = f"activity {match!r}"

    note_pattern = _pattern(path, where, entry.get("note"), "note")
    if note_pattern is not None:
        where = f"activity {match!r} [{note_pattern.pattern}]"

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
    if not skip and entry.get("attach") is None:
        raw_type = entry.get("txn_type")
        if not isinstance(raw_type, str):
            raise _invalid(
                path, f"{where} needs a `txn_type` (or `skip = true`, or an `attach`)"
            )
        txn_type = _txn_type(path, where, raw_type)

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

    identifiers = entry.get("identifiers")
    if identifiers is not None and identifiers not in _IDENTIFIER_CLASSES:
        raise _invalid(
            path,
            f"{where} has an invalid `identifiers` class {identifiers!r}. One of: "
            + ", ".join(sorted(_IDENTIFIER_CLASSES)),
        )

    attach = entry.get("attach")
    if attach is not None:
        if attach not in _ATTACHMENTS:
            raise _invalid(
                path,
                f"{where} has an invalid `attach` {attach!r}. One of: "
                + ", ".join(sorted(_ATTACHMENTS)),
            )
        if skip or "txn_type" in entry:
            raise _invalid(
                path,
                f"{where} attaches to another row and so is neither a ledger row "
                f"nor a skip: drop `txn_type` and `skip`",
            )

    pair = _pair(path, where, entry.get("pair"))
    if txn_type is TransactionType.TRANSFER and pair is None:
        raise _invalid(
            path,
            f"{where} maps to a transfer with no [activity.pair] table. A transfer "
            f"is one ledger row with a counter account (ADR 0007), and the map has "
            f"to say how the other leg is found (ADR 0014)",
        )
    if pair is not None and txn_type is not TransactionType.TRANSFER:
        raise _invalid(
            path,
            f"{where} declares [activity.pair] but is not a transfer. Pairing "
            f"describes the two legs of one internal movement and means nothing "
            f"on any other event",
        )

    cash = _sign(path, where, entry.get("cash", "as_stated"), "cash")
    value = _sign(path, where, entry.get("value", "none"), "value")
    if value is not Sign.NONE and cash is not Sign.NONE:
        raise _invalid(
            path,
            f"{where} reads the amount column as both `cash` and `value`. One "
            f"column is one fact: the cash the account's balance moved, or the "
            f"value of an event that moved none",
        )
    if value in (Sign.AS_STATED, Sign.INVERTED, Sign.NEGATIVE):
        raise _invalid(
            path,
            f"{where} has `value` = {value.value!r}; a value is a magnitude, so "
            f"only `positive` (or `none`) makes sense",
        )

    return ActivityRule(
        match=match,
        txn_type=txn_type,
        quantity=_sign(path, where, entry.get("quantity", "none"), "quantity"),
        cash=cash,
        value=value,
        fee_class=fee_class,
        skip=skip,
        reason=reason if isinstance(reason, str) else None,
        note_pattern=note_pattern,
        identifiers=identifiers,
        pair=pair,
        attach=attach,
    )


def _pair(path: Path, where: str, raw: Any) -> PairSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _invalid(path, f"{where} has a `pair` that is not a table")
    unknown = sorted(set(raw) - {"counterpart", "unpaired_out", "unpaired_in", "window_days"})
    if unknown:
        raise _invalid(
            path,
            f"{where} [activity.pair] has unknown key(s) {', '.join(unknown)}. "
            f"Known: counterpart, unpaired_in, unpaired_out, window_days",
        )
    window = raw.get("window_days", 0)
    if isinstance(window, bool) or not isinstance(window, int) or window < 0:
        raise _invalid(
            path, f"{where} [activity.pair] `window_days` must be a non-negative integer"
        )
    counterpart = _pattern(path, where, raw.get("counterpart"), "pair.counterpart")
    if counterpart is not None and "account" not in counterpart.groupindex:
        raise _invalid(
            path,
            f"{where} [activity.pair] `counterpart` pattern {counterpart.pattern!r} "
            f"has no named group `account`. The pattern exists to say which "
            f"account the note names, so it has to capture one: (?P<account>...)",
        )
    out = _fallback(path, where, raw.get("unpaired_out"), "unpaired_out", _UNPAIRED_OUT)
    into = _fallback(path, where, raw.get("unpaired_in"), "unpaired_in", _UNPAIRED_IN)
    return PairSpec(
        counterpart=counterpart, window_days=window, unpaired_out=out, unpaired_in=into
    )


def _fallback(
    path: Path, where: str, raw: Any, key: str, allowed: frozenset[TransactionType]
) -> TransactionType | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _invalid(path, f"{where} [activity.pair] `{key}` is not a string")
    txn_type = _txn_type(path, f"{where} [activity.pair] `{key}`", raw)
    if txn_type not in allowed:
        raise _invalid(
            path,
            f"{where} [activity.pair] `{key}` is {raw!r}; it must be one of "
            + ", ".join(sorted(t.value for t in allowed))
            + ". An unpaired leg is a flow across the portfolio boundary, and its "
            "direction is the leg's direction",
        )
    return txn_type


def _pattern(path: Path, where: str, raw: Any, key: str) -> re.Pattern[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise _invalid(path, f"{where} has a `{key}` that is not a non-empty string")
    try:
        return re.compile(raw, re.IGNORECASE)
    except re.error as exc:
        raise _invalid(
            path, f"{where} `{key}` pattern {raw!r} is not a valid regex: {exc}"
        ) from None


def _txn_type(path: Path, where: str, raw: str) -> TransactionType:
    try:
        return TransactionType(raw)
    except ValueError:
        raise _invalid(
            path,
            f"{where} maps to unknown txn_type {raw!r}. Known types: "
            + ", ".join(sorted(t.value for t in TransactionType)),
        ) from None


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
