"""The named checks that earn a capability. ADR 0018 §3.

*A capability is declared on validated data, not on a present column.* The
reference custodian's export has a settlement-date column in which most
populated cells hold a date earlier than their own trade date; a column is not
a capability, and the whole mechanism is worthless if declaring one only means
naming it.

So every capability in `source.toml` names one of these checks, the check runs
over the parsed rows, and the capability is declared only if it passes. Where it
fails, the capability is withheld **and the reason is reported**, which is
strictly better than the column being absent: the user learns their custodian's
export is broken rather than assuming the field is unavailable.

The registry is deliberately small and each entry is deliberately dumb. A check
clever enough to be interesting is a check nobody can review, and reviewing
these is the entire safeguard.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final

from portable_core.importers.activity import ActivityMap
from portable_core.importers.capabilities import CapabilityFinding
from portable_core.importers.source import CapabilityCheck

__all__ = ["CHECKS", "CheckInput", "run_check"]


@dataclass(frozen=True, slots=True)
class CheckInput:
    """Everything a check may read. Nothing here is custodian-specific."""

    #: One dict per data row, canonical field name -> parsed value or None.
    rows: Sequence[dict[str, Any]]
    activity: ActivityMap
    #: Whether the source mapped a column for the check's field at all.
    mapped: bool


def _populated(spec: CapabilityCheck, data: CheckInput) -> tuple[int, int, str | None]:
    """The field carries a value in at least `min_ratio` of rows."""
    field = spec.field or ""
    examined = len(data.rows)
    satisfied = sum(1 for row in data.rows if row.get(field) is not None)
    if examined == 0:
        return 0, 0, "the document has no rows to check"
    ratio = Decimal(satisfied) / Decimal(examined)
    if ratio < spec.min_ratio:
        return (
            examined,
            satisfied,
            f"only {satisfied} of {examined} rows carry a {field} "
            f"(the source requires {spec.min_ratio})",
        )
    return examined, satisfied, None


def _unique(spec: CapabilityCheck, data: CheckInput) -> tuple[int, int, str | None]:
    """The field is populated everywhere and never repeats.

    An identifier that repeats is not an identifier. Declaring
    ``TRANSACTION_ID`` on a column that recycles a confirm number across
    accounts would make ADR 0012's re-import safety a fiction.
    """
    field = spec.field or ""
    examined = len(data.rows)
    values = [row.get(field) for row in data.rows]
    present = [v for v in values if v is not None]
    if len(present) != examined:
        return (
            examined,
            len(present),
            f"{examined - len(present)} of {examined} rows carry no {field}",
        )
    seen: dict[Any, int] = {}
    for index, value in enumerate(present, start=1):
        if value in seen:
            return (
                examined,
                len(seen),
                f"{field} {value!r} appears on rows {seen[value]} and {index}; "
                f"a repeated identifier is not an identifier",
            )
        seen[value] = index
    return examined, examined, None


def _matches(spec: CapabilityCheck, data: CheckInput) -> tuple[int, int, str | None]:
    """Every populated value matches a declared pattern.

    The pattern lives in `source.toml` where it is reviewable. This is how
    ``INSTRUMENT_SYMBOL`` is earned: an identifier column populated with
    security *descriptions* passes ``populated`` and is useless, and the only
    honest way to tell the two apart is for the adapter's author to say what a
    symbol looks like at this custodian and be held to it.
    """
    field = spec.field or ""
    pattern = spec.types[0] if spec.types else None
    if pattern is None:
        return 0, 0, 'the check declares no pattern (set `types = ["<regex>"]`)'
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return 0, 0, f"the declared pattern is not a valid regex: {exc}"
    examined = len(data.rows)
    offenders: list[str] = []
    satisfied = 0
    for row in data.rows:
        value = row.get(field)
        if value is None:
            offenders.append("(blank)")
            continue
        if compiled.fullmatch(str(value)):
            satisfied += 1
        else:
            offenders.append(str(value))
    if offenders:
        sample = ", ".join(repr(o) for o in sorted(set(offenders))[:3])
        return (
            examined,
            satisfied,
            f"{len(offenders)} of {examined} values do not match {pattern!r} (e.g. {sample})",
        )
    return examined, satisfied, None


def _not_before_trade_date(
    spec: CapabilityCheck, data: CheckInput
) -> tuple[int, int, str | None]:
    """A date field never precedes its own row's trade date.

    This is the reference custodian's trap, written down. Settlement before
    trade is not a late file or a rounding difference -- it is a column that
    holds something other than what its header says, and importing it would put
    that something in the ledger.
    """
    field = spec.field or ""
    examined = 0
    satisfied = 0
    offenders = 0
    first: str | None = None
    for index, row in enumerate(data.rows, start=1):
        value = row.get(field)
        traded = row.get("trade_date")
        if value is None or not isinstance(traded, date) or not isinstance(value, date):
            continue
        examined += 1
        if value < traded:
            offenders += 1
            if first is None:
                first = (
                    f"row {index}: {field} {value.isoformat()} precedes "
                    f"trade date {traded.isoformat()}"
                )
        else:
            satisfied += 1
    if examined == 0:
        return 0, 0, f"no row carries both a {field} and a trade date"
    if offenders:
        return (
            examined,
            satisfied,
            f"{offenders} of {examined} rows have a {field} before their own "
            f"trade date ({first})",
        )
    return examined, satisfied, None


def _history_since(spec: CapabilityCheck, data: CheckInput) -> tuple[int, int, str | None]:
    """The history reaches back at least to a stated date.

    ``HISTORY_TO_INCEPTION`` is the capability whose absence forces the whole of
    ADR 0017, so it is worth being explicit: the adapter's author states the
    account's inception date and the check confirms the export actually reaches
    it. A two-year window that the custodian will not extend fails here, by
    design.
    """
    if spec.since is None:
        return 0, 0, "the check declares no `since` date"
    dates = [row.get("trade_date") for row in data.rows]
    known = [d for d in dates if isinstance(d, date)]
    if not known:
        return 0, 0, "the document has no dated rows"
    earliest = min(known)
    if earliest > spec.since:
        return (
            len(known),
            0,
            f"the earliest row is {earliest.isoformat()}, later than the stated "
            f"inception {spec.since.isoformat()}: the export does not reach "
            f"inception and a cutover is required (ADR 0017)",
        )
    return len(known), len(known), None


def _activity_covers(spec: CapabilityCheck, data: CheckInput) -> tuple[int, int, str | None]:
    """The activity map names every listed transaction type.

    Whether a custodian's history contains corporate actions, or contributions
    and withdrawals, is a fact about its **vocabulary**, not about any column --
    so this check reads the map rather than the rows.
    """
    if not spec.types:
        return 0, 0, "the check lists no `types`"
    named = {t.value for t in data.activity.types()}
    missing = sorted(set(spec.types) - named)
    if missing:
        return (
            len(spec.types),
            len(spec.types) - len(missing),
            f"the activity map names no rule producing {', '.join(missing)}",
        )
    return len(spec.types), len(spec.types), None


#: Every check an adapter may name. An unknown name is refused when the adapter
#: loads, not when a row reaches it.
CHECKS: Final[
    dict[str, Callable[[CapabilityCheck, CheckInput], tuple[int, int, str | None]]]
] = {
    "populated": _populated,
    "unique": _unique,
    "matches": _matches,
    "not_before_trade_date": _not_before_trade_date,
    "history_since": _history_since,
    "activity_covers": _activity_covers,
}


def run_check(spec: CapabilityCheck, data: CheckInput) -> CapabilityFinding:
    """Run one declared check and report what it found, pass or fail."""
    if spec.field is not None and not data.mapped:
        return CapabilityFinding(
            capability=spec.capability,
            declared=False,
            check=spec.check,
            reason=(
                f"the source maps no {spec.field} column in the {spec.document} "
                f"document, so there is nothing for {spec.check} to check"
            ),
        )
    examined, satisfied, reason = CHECKS[spec.check](spec, data)
    return CapabilityFinding(
        capability=spec.capability,
        declared=reason is None,
        check=spec.check,
        reason=reason,
        examined=examined,
        satisfied=satisfied,
    )
