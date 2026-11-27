"""Defining an index: its identity, securities, methodology, members and events. ADR 0050.

Every row here is the owner's, effective-dated and never edited. A second row
for the same key and date is refused; one entered wrongly is replaced on its
own date with ``corrects`` (ADR 0025 decision 5). Nothing is inferred: a
schedule proposed from a security's dividend history is written only when the
owner accepts it, and is marked as proposed and accepted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise
from statistics import median
from typing import Final

from portable_core import __version__
from portable_core.domain.enums import InstrumentType
from portable_core.domain.index import (
    DistributionSchedule,
    EventType,
    IndexMeta,
    Member,
    Methodology,
    RebalanceRule,
    Reconstitution,
    Schedule,
    SecurityEvent,
    SecurityType,
    SyntheticSecurity,
    Weighting,
)
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_INDEX_DEFINITION
from portable_core.persistence.index_repositories import IndexRepositories
from portable_core.providers import MarketDataProvider, as_security_master
from portable_core.schema.bmk import CURRENT_BMK_SCHEMA_VERSION
from portable_core.services.index_construction import GAP_DAYS

__all__ = [
    "ScheduleProposal",
    "accept_proposals",
    "add_event",
    "add_security",
    "add_security_from_provider",
    "create",
    "propose_schedules",
    "reconstitute",
    "resolve",
    "set_methodology",
    "set_schedule",
]

ZERO: Final = Decimal(0)
ONE: Final = Decimal(1)

_SECURITY_TYPES: Final[dict[InstrumentType, SecurityType]] = {
    InstrumentType.EQUITY: SecurityType.EQUITY,
    InstrumentType.ETF: SecurityType.ETF,
    InstrumentType.MUTUAL_FUND: SecurityType.MUTUAL_FUND,
    InstrumentType.ADR: SecurityType.ADR,
}


def _refuse(message: str, remedy: str | None = None, **context: object) -> ValidationError:
    return ValidationError(message, code=E_INDEX_DEFINITION, remedy=remedy, **context)


def create(repos: IndexRepositories, meta: IndexMeta, *, version: str = __version__) -> None:
    """Write a fresh file's identity. The schema is already applied.

    *version* is what the file says wrote it: this release, unless a
    generator pins it (ADR 0049 §5).
    """
    if not meta.index_name.strip():
        raise _refuse(
            "an index needs a name", remedy="A specific one: it is what reports show."
        )
    if meta.base_level <= 0:
        raise _refuse(f"a base level of {meta.base_level} is not a level")
    if meta.currency != "USD":
        raise _refuse(f"currency {meta.currency}: version 1 indexes are in USD")
    values = {
        "schema_version": str(CURRENT_BMK_SCHEMA_VERSION),
        "index_name": meta.index_name.strip(),
        "description": meta.description.strip(),
        "base_date": meta.base_date.isoformat(),
        "base_level": str(meta.base_level),
        "currency": meta.currency,
        "calendar": meta.calendar.strip().upper(),
        "created_at": repos.clock.stamp(),
        "portable_version": version,
    }
    for key, value in values.items():
        repos.meta_set(key, value)


# ── securities ───────────────────────────────────────────────────────────────


def add_security(repos: IndexRepositories, security: SyntheticSecurity) -> int:
    """Add a security the index may hold. A fafnir id is held once."""
    if security.fafnir_sid is not None and repos.by_fafnir_sid(security.fafnir_sid):
        raise _refuse(
            f"fafnir security {security.fafnir_sid} is already in the index",
            fafnir_sid=security.fafnir_sid,
        )
    if security.fafnir_sid is None and repos.resolve(security.symbol, date.max):
        raise _refuse(f"the index already holds a security named {security.symbol}")
    return repos.add_security(security)


def add_security_from_provider(
    repos: IndexRepositories,
    provider: MarketDataProvider,
    symbol: str,
    *,
    on: date,
    issuer_key: str | None = None,
) -> SyntheticSecurity:
    """The security *symbol* named on *on*, as the provider describes it, by its id."""
    instrument = as_security_master(provider).lookup_security(symbol, on=on)
    kind = _SECURITY_TYPES.get(instrument.instrument_type)
    if kind is None:
        raise _refuse(
            f"{symbol} is {instrument.instrument_type}, which an index does not hold",
            remedy="An index holds equities, ETFs, mutual funds and ADRs.",
        )
    sid: int | None = None
    ref = instrument.provider_ref or ""
    if ref.startswith("security_id="):
        sid = int(ref.removeprefix("security_id=").split(";")[0])
    security = SyntheticSecurity(
        security_id=0,
        fafnir_sid=sid,
        symbol=instrument.symbol,
        name=instrument.name,
        security_type=kind,
        currency=instrument.currency,
        issuer_key=issuer_key,
        source=instrument.source,
        provider_ref=instrument.provider_ref,
    )
    row_id = add_security(repos, security)
    repos.add_symbol(row_id, symbol.strip().upper(), on, None, instrument.source)
    return repos.security(row_id)


def resolve(repos: IndexRepositories, symbol: str, on: date) -> SyntheticSecurity:
    """The one security the index holds under *symbol* on *on*."""
    found = repos.resolve(symbol.strip().upper(), on)
    if not found:
        raise _refuse(
            f"the index holds no security {symbol} on {on.isoformat()}",
            remedy="Add it first with `bmk security add SYMBOL`.",
            symbol=symbol,
        )
    if len(found) > 1:
        raise _refuse(
            f"{symbol} names {len(found)} securities in the index on {on.isoformat()}",
            remedy="Name it by its fafnir id: sid:N.",
            symbol=symbol,
        )
    return found[0]


# ── the definition ───────────────────────────────────────────────────────────


def _same_date(rows: Sequence[object], *, corrects: bool, what: str, on: date) -> bool:
    """ADR 0025 decisions 4 and 5: one row per key and date; --corrects replaces it."""
    if corrects and not rows:
        raise _refuse(
            f"{what} has no row on {on.isoformat()} to correct",
            remedy="Without --corrects, it is recorded from that date.",
        )
    if rows and not corrects:
        raise _refuse(
            f"{what} already has a row on {on.isoformat()}",
            remedy=(
                "Rows are never edited. Pass --corrects to record this one in its place on "
                "the same date; the wrong one is kept as history (ADR 0025)."
            ),
        )
    return corrects


def _base(repos: IndexRepositories) -> date:
    return repos.index_meta().base_date


def set_methodology(
    repos: IndexRepositories,
    on: date,
    weighting: Weighting,
    rule: RebalanceRule,
    *,
    drift_band: Decimal | None = None,
    corrects: bool = False,
    note: str | None = None,
) -> int:
    """How the index weights and rebalances, from *on*'s close."""
    base = _base(repos)
    if on < base:
        raise _refuse(f"{on.isoformat()} precedes the base date {base.isoformat()}")
    if (rule is RebalanceRule.DRIFT_BAND) != (drift_band is not None):
        raise _refuse("a drift band is given with --rebalance drift_band, and only then")
    if drift_band is not None and not ZERO < drift_band < ONE:
        raise _refuse(f"a drift band of {drift_band} is not between 0 and 1")
    if rule is RebalanceRule.DRIFT_BAND and weighting not in (
        Weighting.CUSTOM,
        Weighting.EQUAL,
    ):
        raise _refuse(
            "a drift band needs targets that hold still between rebalances: custom or "
            "equal weights. A cap target moves every day; a price-weighted index has none."
        )
    if weighting is Weighting.PRICE and rule is not RebalanceRule.NEVER:
        raise _refuse(
            "a price-weighted index holds one unit of each member: only a reconstitution "
            "changes it, so its rule is `never`"
        )
    same = [m for m in repos.methodologies() if m.effective_from == on]
    corrected = _same_date(same, corrects=corrects, what="the methodology", on=on)
    return repos.add_methodology(
        Methodology(
            methodology_id=0,
            effective_from=on,
            weighting=weighting,
            rebalance_rule=rule,
            drift_band=drift_band,
            note=note,
            corrects_methodology_id=same[0].methodology_id if corrected else None,
        )
    )


def _method_on(repos: IndexRepositories, on: date) -> Methodology:
    found = [m for m in repos.methodologies() if m.effective_from <= on]
    if not found:
        raise _refuse(
            f"no methodology is in force on {on.isoformat()}",
            remedy="Set one first: `bmk methodology set --effective-from DATE ...`.",
        )
    return found[-1]


def reconstitute(
    repos: IndexRepositories,
    on: date,
    members: Sequence[Member],
    *,
    corrects: bool = False,
    note: str | None = None,
) -> int:
    """The full membership from *on*'s close."""
    base = _base(repos)
    if on < base:
        raise _refuse(f"{on.isoformat()} precedes the base date {base.isoformat()}")
    if not members:
        raise _refuse("a membership has at least one member")
    ids = [m.security_id for m in members]
    if len(set(ids)) != len(ids):
        raise _refuse("a security is named twice in the membership")
    method = _method_on(repos, on)
    weighted = [m for m in members if m.weight is not None]
    if method.weighting is Weighting.CUSTOM:
        if len(weighted) != len(members):
            raise _refuse(
                "under `custom` every member states its weight: SYMBOL=WEIGHT",
                remedy="bmk reconstitute --effective-date DATE AAA=0.6 BBB=0.4",
            )
        for m in members:
            assert m.weight is not None
            if not ZERO < m.weight <= ONE:
                raise _refuse(f"a weight of {m.weight} is not more than 0 and at most 1")
        total = sum((m.weight for m in members if m.weight is not None), ZERO)
        if total != ONE:
            raise _refuse(
                f"the weights sum to {total}, not exactly 1",
                remedy="State every weight so they add up to 1.",
                total=str(total),
            )
    elif weighted:
        raise _refuse(
            f"under `{method.weighting}` the method sets the weights; a weight given would "
            "not be read",
            remedy="Name the members alone, or set a `custom` methodology from this date.",
        )
    for m in members:
        if m.free_float is not None and not ZERO < m.free_float <= ONE:
            raise _refuse(f"a float factor of {m.free_float} is not more than 0 and at most 1")
        repos.security(m.security_id)
    same = [r for r in repos.reconstitutions() if r.effective_date == on]
    corrected = _same_date(same, corrects=corrects, what="the membership", on=on)
    return repos.add_reconstitution(
        Reconstitution(
            reconstitution_id=0,
            effective_date=on,
            members=tuple(sorted(members, key=lambda m: m.security_id)),
            note=note,
            corrects_reconstitution_id=same[0].reconstitution_id if corrected else None,
        )
    )


def add_event(repos: IndexRepositories, event: SecurityEvent, *, corrects: bool = False) -> int:
    """Declare a delisting, merger or spinoff (ADR 0050 §5)."""
    if event.event_type in (EventType.DELISTING, EventType.CASH_MERGER):
        if event.cash_per_share is None or event.cash_per_share < 0:
            raise _refuse(f"a {event.event_type} states what it paid a share, zero included")
        if event.received_security_id is not None:
            raise _refuse(f"a {event.event_type} pays cash, not shares")
    else:
        if event.received_security_id is None or event.received_ratio is None:
            raise _refuse(f"a {event.event_type} names the security received and the ratio")
        if event.received_ratio <= 0:
            raise _refuse(f"a ratio of {event.received_ratio} receives nothing")
        repos.security(event.received_security_id)
    repos.security(event.security_id)
    same = [
        e
        for e in repos.events()
        if e.security_id == event.security_id
        and e.event_date == event.event_date
        and e.event_type == event.event_type
    ]
    corrected = _same_date(
        same, corrects=corrects, what="that security's event", on=event.event_date
    )
    return repos.add_event(
        SecurityEvent(
            event_id=0,
            security_id=event.security_id,
            event_date=event.event_date,
            event_type=event.event_type,
            source=event.source,
            cash_per_share=event.cash_per_share,
            received_security_id=event.received_security_id,
            received_ratio=event.received_ratio,
            note=event.note,
            corrects_event_id=same[0].event_id if corrected else None,
        )
    )


def set_schedule(
    repos: IndexRepositories,
    security_id: int,
    on: date,
    schedule: Schedule,
    *,
    source: str,
    max_gap_days: int | None = None,
    corrects: bool = False,
) -> int:
    """Declare how a security pays, from *on* (ADR 0050, answer 3)."""
    repos.security(security_id)
    if (schedule is Schedule.IRREGULAR) != (max_gap_days is not None):
        raise _refuse("an irregular payer states --max-gap-days, and only it does")
    same = [
        s for s in repos.schedules() if s.security_id == security_id and s.effective_from == on
    ]
    corrected = _same_date(same, corrects=corrects, what="that security's schedule", on=on)
    return repos.add_schedule(
        DistributionSchedule(
            schedule_id=0,
            security_id=security_id,
            effective_from=on,
            schedule=schedule,
            source=source,
            max_gap_days=max_gap_days,
            corrects_schedule_id=same[0].schedule_id if corrected else None,
        )
    )


# ── schedules proposed from history ──────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ScheduleProposal:
    """A schedule read from a security's dividend history, not yet declared."""

    security_id: int
    symbol: str
    schedule: Schedule
    #: For `irregular`: the longest gap seen, with a margin of half again.
    max_gap_days: int | None
    dividends: int
    #: The median days between ex-dates, if there were two or more.
    median_gap_days: int | None
    first_ex: date | None
    last_ex: date | None


def propose_schedules(repos: IndexRepositories) -> list[ScheduleProposal]:
    """A schedule for every security that needs one and has none, from its history.

    A proposal only: written by :func:`accept_proposals`, when the owner says.
    Read from the dividends the file holds, so `bmk refresh` comes first. The
    median gap decides the period; a history too thin or too uneven to call
    is proposed as `irregular`, with its longest gap and half again.
    """
    declared = {s.security_id for s in repos.schedules()}
    held = {m.security_id for r in repos.reconstitutions() for m in r.members}
    held |= {e.received_security_id for e in repos.events() if e.received_security_id}
    exes: dict[int, list[date]] = {}
    for dividend in repos.dividends():
        exes.setdefault(dividend.security_id, []).append(dividend.ex_date)
    proposals: list[ScheduleProposal] = []
    for sid in sorted(held - declared):
        security = repos.security(sid)
        dates = sorted(set(exes.get(sid, [])))
        gaps = [(b - a).days for a, b in pairwise(dates)]
        middle = int(median(gaps)) if gaps else None
        schedule, max_gap = _schedule_of(dates, gaps)
        proposals.append(
            ScheduleProposal(
                security_id=sid,
                symbol=security.symbol,
                schedule=schedule,
                max_gap_days=max_gap,
                dividends=len(dates),
                median_gap_days=middle,
                first_ex=dates[0] if dates else None,
                last_ex=dates[-1] if dates else None,
            )
        )
    return proposals


#: The days between payments each schedule means.
_NOMINAL_DAYS: Final[dict[Schedule, int]] = {
    Schedule.MONTHLY: 30,
    Schedule.QUARTERLY: 91,
    Schedule.SEMIANNUAL: 182,
    Schedule.ANNUAL: 365,
}


def _schedule_of(dates: list[date], gaps: list[int]) -> tuple[Schedule, int | None]:
    """The period nearest the median gap, if every gap fits it; else irregular."""
    if not dates:
        return Schedule.NONE, None
    if len(gaps) >= 2:
        middle = median(gaps)
        nearest = min(_NOMINAL_DAYS, key=lambda s: abs(middle - _NOMINAL_DAYS[s]))
        close = abs(middle - _NOMINAL_DAYS[nearest]) <= _NOMINAL_DAYS[nearest] / 4
        if close and max(gaps) <= GAP_DAYS[nearest]:
            return nearest, None
    longest = max(gaps) if gaps else 365
    return Schedule.IRREGULAR, longest + longest // 2


def accept_proposals(
    repos: IndexRepositories, proposals: Sequence[ScheduleProposal], *, source: str
) -> list[int]:
    """Declare each proposal, from the base date, marked as proposed and accepted."""
    base = _base(repos)
    return [
        set_schedule(
            repos,
            p.security_id,
            base,
            p.schedule,
            source=f"proposed from {source} history ({p.dividends} dividends), accepted",
            max_gap_days=p.max_gap_days,
        )
        for p in proposals
    ]
