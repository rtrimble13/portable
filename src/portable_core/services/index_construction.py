"""Building an index's levels from its definition and its inputs. ADR 0050 §3 to §5.

:func:`construct` is pure: definition and market data in, levels out, no I/O,
so each method can be checked by hand. :func:`build` reads a `.bmk` file,
constructs, and replaces the file's built rows.

**The index holds units.** On each trading day ``t``:

1. **At the open**, a split multiplies a holding's units by its ratio, or,
   under ``price``, changes the divisor so the level does not move. A spinoff
   adds the received shares.
2. **At the close**, the price level is ``sum(units x close) / divisor``. A
   member leaving that day by a declared event is valued at what it paid.
3. **Dividends going ex** on ``t`` add ``sum(units x dividend) / divisor``
   index points. The total-return level chains on the price level:
   ``tr_t = tr_{t-1} x (price_t + points_t) / price_{t-1}``. That is the
   dividend reinvested across the whole index at the ex-date close (answer
   4), which scales every unit by one factor and moves no weight.
4. **After the close**, in this order: a member's delisting or merger is
   settled and its value reinvested across the index; a reconstitution or a
   methodology change takes effect; and the units are reset to the targets
   if any of those happened or the rebalancing rule falls due. Each change is
   value-neutral: the next day starts from the same level.

The price level is ``price_only`` and is never a benchmark (``PORT-GIPS-G01``);
the total-return level is the one a portfolio is measured against.

Every refusal names the security and the day. Nothing is carried, assumed or
skipped: a missing close, an undeclared event, a missing cap, a dividend gap
longer than the declared schedule allows, and two classes of one issuer each
stop the build (``CLAUDE.md`` invariant 9).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final

from portable_core.decimals import money_context
from portable_core.domain.index import (
    DistributionSchedule,
    Dividend,
    EventType,
    HoldingSpan,
    IndexBuild,
    IndexMeta,
    LevelRow,
    Methodology,
    RebalanceRule,
    RebalanceWeight,
    Reconstitution,
    Reinvestment,
    Schedule,
    SecurityEvent,
    Split,
    SyntheticSecurity,
    Weighting,
)
from portable_core.errors import DataUnavailableError, PortableError, ValidationError
from portable_core.errors.kinds import (
    E_INDEX_CAP_MISSING,
    E_INDEX_CLOSE_MISSING,
    E_INDEX_DEFINITION,
    E_INDEX_DIVIDEND_GAP,
    E_INDEX_EVENT_UNDECLARED,
    E_INDEX_NO_SCHEDULE,
    E_INDEX_NOT_BUILT,
    E_INDEX_ONE_ISSUER,
)
from portable_core.persistence.connection import scratch_transaction
from portable_core.persistence.index_repositories import IndexRepositories

__all__ = [
    "GAP_DAYS",
    "BuiltIndex",
    "Holding",
    "IndexInputs",
    "build",
    "composition",
    "construct",
    "current_build",
    "inputs_of",
    "verify",
]

ZERO: Final = Decimal(0)
ONE: Final = Decimal(1)

#: The longest gap between two ex-dates each schedule allows, in days, before
#: the build stops (ADR 0050, answer 3): the period with room for a payment
#: that slips a few weeks. `irregular` states its own.
GAP_DAYS: Final[dict[Schedule, int]] = {
    Schedule.MONTHLY: 45,
    Schedule.QUARTERLY: 120,
    Schedule.SEMIANNUAL: 210,
    Schedule.ANNUAL: 400,
}

#: The months whose last trading day a calendar rule rebalances on.
_PERIOD_MONTHS: Final[dict[RebalanceRule, frozenset[int]]] = {
    RebalanceRule.MONTHLY: frozenset(range(1, 13)),
    RebalanceRule.QUARTERLY: frozenset({3, 6, 9, 12}),
    RebalanceRule.SEMIANNUAL: frozenset({6, 12}),
    RebalanceRule.ANNUAL: frozenset({12}),
}

_TERMINAL: Final = frozenset(
    {EventType.DELISTING, EventType.CASH_MERGER, EventType.STOCK_MERGER}
)


@dataclass(frozen=True, slots=True)
class IndexInputs:
    """Everything an index is built from: the definition and the market data."""

    meta: IndexMeta
    securities: Mapping[int, SyntheticSecurity]
    methodologies: Sequence[Methodology]
    reconstitutions: Sequence[Reconstitution]
    events: Sequence[SecurityEvent]
    schedules: Sequence[DistributionSchedule]
    trading_days: Sequence[date]
    closes: Mapping[tuple[int, date], Decimal]
    splits: Sequence[Split]
    dividends: Sequence[Dividend]
    caps: Mapping[tuple[int, date], Decimal]


@dataclass(frozen=True, slots=True)
class BuiltIndex:
    """What a build produces."""

    levels: tuple[LevelRow, ...]
    holdings: tuple[HoldingSpan, ...]
    weights: tuple[RebalanceWeight, ...]
    reinvestments: tuple[Reinvestment, ...]

    @property
    def first_date(self) -> date:
        return self.levels[0].level_date

    @property
    def last_date(self) -> date:
        return self.levels[-1].level_date


def construct(inputs: IndexInputs, *, through: date | None = None) -> BuiltIndex:
    """Build every level from ``base_date`` to *through*, or the last trading day."""
    with money_context():
        return _Builder(inputs, through).run()


# ── the builder ──────────────────────────────────────────────────────────────


class _Builder:
    def __init__(self, inputs: IndexInputs, through: date | None) -> None:
        self.inputs = inputs
        meta = inputs.meta
        self.base = meta.base_date
        self.days = [
            d
            for d in inputs.trading_days
            if d >= self.base and (through is None or d <= through)
        ]
        if not self.days or self.days[0] != self.base:
            raise ValidationError(
                f"the base date {self.base.isoformat()} is not a trading day of the "
                f"{meta.calendar} calendar held in the file",
                code=E_INDEX_DEFINITION,
                remedy=(
                    "Refresh the calendar (`bmk refresh`), or choose a base date that is "
                    "a session."
                ),
                base_date=self.base.isoformat(),
            )
        self.day_set = set(self.days)
        self.all_days = sorted(inputs.trading_days)
        self.next_day = dict(zip(self.all_days, self.all_days[1:], strict=False))

        self.methods = sorted(inputs.methodologies, key=lambda m: m.effective_from)
        self.recons = sorted(inputs.reconstitutions, key=lambda r: r.effective_date)
        self._check_definition()
        self.method_change = {m.effective_from: m for m in self.methods}
        self.recon_on = {r.effective_date: r for r in self.recons}

        self.splits: dict[date, dict[int, Split]] = defaultdict(dict)
        for s in inputs.splits:
            self.splits[s.ex_date][s.security_id] = s
        self.dividends: dict[date, list[Dividend]] = defaultdict(list)
        for d in inputs.dividends:
            self.dividends[d.ex_date].append(d)
        self.terminal: dict[date, list[SecurityEvent]] = defaultdict(list)
        self.spinoffs: dict[date, list[SecurityEvent]] = defaultdict(list)
        seen_terminal: set[int] = set()
        for e in inputs.events:
            if e.event_type in _TERMINAL:
                if e.security_id in seen_terminal:
                    raise self._definition(
                        f"{self._sym(e.security_id)} has two delistings or mergers declared",
                        security=self._sym(e.security_id),
                    )
                seen_terminal.add(e.security_id)
                self.terminal[e.event_date].append(e)
            else:
                self.spinoffs[e.event_date].append(e)
        self.last_close: dict[int, date] = defaultdict(lambda: date.min)
        for sid, on in inputs.closes:
            if on > self.last_close[sid]:
                self.last_close[sid] = on

        # State.
        self.units: dict[int, Decimal] = {}
        self.members: set[int] = set()
        self.divisor = ONE
        self.method = self.methods[0]
        self.recon = self.recons[0]
        self.price_level = meta.base_level
        self.tr_level = meta.base_level

        # Output.
        self.levels: list[LevelRow] = []
        self.weights: list[RebalanceWeight] = []
        self.reinvestments: list[Reinvestment] = []
        self.held: dict[int, list[tuple[date, Decimal]]] = defaultdict(list)

    # ── definition checks ────────────────────────────────────────────────────

    def _definition(
        self, message: str, remedy: str | None = None, **context: Any
    ) -> ValidationError:
        return ValidationError(message, code=E_INDEX_DEFINITION, remedy=remedy, **context)

    def _sym(self, sid: int) -> str:
        found = self.inputs.securities.get(sid)
        return found.symbol if found else f"security {sid}"

    def _check_definition(self) -> None:
        base = self.base.isoformat()
        if not self.methods or self.methods[0].effective_from != self.base:
            raise self._definition(
                f"no methodology takes effect on the base date {base}",
                remedy=f"`bmk methodology set --effective-from {base} ...`",
            )
        if not self.recons or self.recons[0].effective_date != self.base:
            raise self._definition(
                f"no membership takes effect on the base date {base}",
                remedy=f"`bmk reconstitute --effective-date {base} SYMBOL ...`",
            )
        for row_date, what in [(m.effective_from, "methodology") for m in self.methods] + [
            (r.effective_date, "reconstitution") for r in self.recons
        ]:
            if row_date < self.base:
                raise self._definition(
                    f"a {what} on {row_date.isoformat()} precedes the base date"
                )
            if row_date <= self.days[-1] and row_date not in self.day_set:
                raise self._definition(
                    f"the {what} of {row_date.isoformat()} is not on a trading day: it applies "
                    "at a close",
                    remedy="Date it on a session of the index's calendar.",
                )
        for recon in self.recons:
            if not recon.members:
                raise self._definition(
                    f"the reconstitution of {recon.effective_date.isoformat()} has no members"
                )
            issuers: dict[str, int] = {}
            for member in recon.members:
                security = self.inputs.securities.get(member.security_id)
                if security is None:
                    raise self._definition(
                        f"a member names unknown security {member.security_id}"
                    )
                key = security.issuer_key
                if key is None:
                    continue
                if key in issuers:
                    raise ValidationError(
                        f"{self._sym(issuers[key])} and {security.symbol} are both issuer "
                        f"{key} in the membership of {recon.effective_date.isoformat()}: one "
                        "issuer is held once",
                        code=E_INDEX_ONE_ISSUER,
                        remedy=(
                            "Keep one class. fafnir's market cap counts every class of an "
                            "issuer under its primary class, so two would hold it twice."
                        ),
                        issuer=key,
                    )
                issuers[key] = member.security_id

    # ── prices ───────────────────────────────────────────────────────────────

    def _close(self, sid: int, on: date) -> Decimal:
        found = self.inputs.closes.get((sid, on))
        if found is not None:
            return found
        if self.last_close[sid] < on:
            raise DataUnavailableError(
                f"{self._sym(sid)} has no close on {on.isoformat()} or after it, and is held: "
                "declare what it became",
                code=E_INDEX_EVENT_UNDECLARED,
                remedy=(
                    "`bmk event add` a delisting, a cash merger or a stock merger dated its "
                    "last day, with what it paid. Nothing is assumed."
                ),
                security=self._sym(sid),
                last_close=(
                    None
                    if self.last_close[sid] == date.min
                    else self.last_close[sid].isoformat()
                ),
            )
        raise DataUnavailableError(
            f"{self._sym(sid)} has no close on {on.isoformat()}, a trading day it is held",
            code=E_INDEX_CLOSE_MISSING,
            remedy=(
                "Refresh it from the source, or supply it from a file. A price is never "
                "carried forward into an index."
            ),
            security=self._sym(sid),
            date=on.isoformat(),
        )

    def _consideration(self, event: SecurityEvent, on: date) -> Decimal:
        if event.event_type is EventType.STOCK_MERGER:
            assert event.received_security_id is not None and event.received_ratio is not None
            return event.received_ratio * self._close(event.received_security_id, on)
        assert event.cash_per_share is not None
        return event.cash_per_share

    def _price(self, sid: int, on: date, leaving: Mapping[int, SecurityEvent]) -> Decimal:
        event = leaving.get(sid)
        return self._consideration(event, on) if event else self._close(sid, on)

    # ── the walk ─────────────────────────────────────────────────────────────

    def run(self) -> BuiltIndex:
        for index, today in enumerate(self.days):
            if index == 0:
                self.members = {m.security_id for m in self.recon.members}
                self._reset(today, is_recon=True, first=True)
                self._record(today, ZERO, rebalanced=True, reconstituted=True, used=self.units)
                continue
            self._day(self.days[index - 1], today)
        self._check_distributions()
        return BuiltIndex(
            levels=tuple(self.levels),
            holdings=tuple(self._spans()),
            weights=tuple(self.weights),
            reinvestments=tuple(self.reinvestments),
        )

    def _day(self, prev: date, today: date) -> None:
        # 1. At the open: splits and spinoffs.
        splits = {sid: s for sid, s in self.splits.get(today, {}).items() if sid in self.units}
        if splits:
            if self.method.weighting is Weighting.PRICE:
                before = sum((u * self._close(s, prev) for s, u in self.units.items()), ZERO)
                adjusted = sum(
                    (
                        u
                        * (
                            self._close(s, prev) / splits[s].ratio
                            if s in splits
                            else self._close(s, prev)
                        )
                        for s, u in self.units.items()
                    ),
                    ZERO,
                )
                self.divisor = self.divisor * adjusted / before
            else:
                for sid, split in splits.items():
                    self.units[sid] = self.units[sid] * split.ratio
        for event in self.spinoffs.get(today, []):
            if event.security_id not in self.units:
                continue
            assert event.received_security_id is not None and event.received_ratio is not None
            received = event.received_security_id
            self.units[received] = (
                self.units.get(received, ZERO)
                + self.units[event.security_id] * event.received_ratio
            )

        # 2. The close.
        leaving = {
            e.security_id: e
            for e in self.terminal.get(today, [])
            if e.security_id in self.units
        }
        used = dict(self.units)
        value = sum((u * self._price(s, today, leaving) for s, u in used.items()), ZERO)
        price_level = value / self.divisor

        # 3. Dividends going ex today, on the units held at the open.
        points = ZERO
        for dividend in self.dividends.get(today, []):
            held = used.get(dividend.security_id)
            if held is None:
                continue
            added = held * dividend.amount / self.divisor
            points += added
            self.reinvestments.append(
                Reinvestment(
                    dividend_id=dividend.dividend_id,
                    level_date=today,
                    security_id=dividend.security_id,
                    units=held,
                    amount=dividend.amount,
                    index_points=added,
                )
            )
        self.tr_level = self.tr_level * (price_level + points) / self.price_level
        self.price_level = price_level

        # 4. After the close: events, then reconstitution and methodology, then
        # the rule.
        for sid, event in leaving.items():
            self._settle(sid, event, today)
        recon = self.recon_on.get(today)
        if recon is not None:
            self.recon = recon
            self.members = {m.security_id for m in recon.members}
        changed = self.method_change.get(today)
        if changed is not None:
            self.method = changed
        due = recon is not None or changed is not None or self._rule_due(today)
        if due:
            self._reset(today, is_recon=recon is not None)
        self._record(today, points, rebalanced=due, reconstituted=recon is not None, used=used)

    def _settle(self, sid: int, event: SecurityEvent, today: date) -> None:
        """A member leaves at its consideration, which is reinvested across the index."""
        units = self.units.pop(sid)
        self.members.discard(sid)
        if event.event_type is EventType.STOCK_MERGER:
            assert event.received_security_id is not None and event.received_ratio is not None
            received = event.received_security_id
            self.units[received] = self.units.get(received, ZERO) + units * event.received_ratio
            return
        if not self.units:
            raise self._definition(
                f"{self._sym(sid)} leaves on {today.isoformat()} and nothing is left to hold",
                remedy="Declare a reconstitution on that date.",
            )
        value_after = sum((u * self._close(s, today) for s, u in self.units.items()), ZERO)
        if self.method.weighting is Weighting.PRICE:
            # The member is dropped and the divisor keeps the level.
            self.divisor = value_after / self.price_level
        else:
            # Its proceeds are reinvested pro rata, which keeps every weight.
            scale = self.price_level * self.divisor / value_after
            self.units = {s: u * scale for s, u in self.units.items()}

    # ── the rule ─────────────────────────────────────────────────────────────

    def _rule_due(self, today: date) -> bool:
        rule = self.method.rebalance_rule
        if rule is RebalanceRule.DAILY:
            return True
        if rule is RebalanceRule.NEVER:
            return False
        if rule is RebalanceRule.DRIFT_BAND:
            assert self.method.drift_band is not None
            targets = self._targets(today)
            value = sum((u * self._close(s, today) for s, u in self.units.items()), ZERO)
            held = set(self.units) | set(targets)
            return any(
                abs(
                    self.units.get(s, ZERO) * self._close(s, today) / value
                    - targets.get(s, ZERO)
                )
                > self.method.drift_band
                for s in held
            )
        following = self.next_day.get(today)
        if following is None:
            # The period may not have ended: the next session is not in the file.
            # A rebalance at this close would only move later levels, which a
            # later build, knowing the next session, will decide.
            return False
        ends_month = following.month != today.month or following.year != today.year
        return ends_month and today.month in _PERIOD_MONTHS[rule]

    def _targets(self, today: date) -> dict[int, Decimal]:
        members = sorted(self.members)
        weighting = self.method.weighting
        if weighting is Weighting.EQUAL:
            share = ONE / Decimal(len(members))
            return dict.fromkeys(members, share)
        if weighting is Weighting.CUSTOM:
            declared = {m.security_id: m.weight for m in self.recon.members}
            missing = [self._sym(s) for s in members if declared.get(s) is None]
            if missing:
                raise self._definition(
                    f"under `custom`, {', '.join(missing)} have no weight in the membership "
                    f"of {self.recon.effective_date.isoformat()}",
                    remedy="Reconstitute with a weight for every member: SYMBOL=WEIGHT.",
                )
            weights = {s: declared[s] for s in members}
            total = sum((w for w in weights.values() if w is not None), ZERO)
            if total != ONE:
                left = sorted(
                    self._sym(m.security_id)
                    for m in self.recon.members
                    if m.security_id not in self.members
                )
                raise self._definition(
                    f"under `custom` the weights held on {today.isoformat()} sum to {total}, "
                    "not exactly 1" + (f": {', '.join(left)} left the index" if left else ""),
                    remedy=(
                        "Declare a reconstitution from that date with the weights the index "
                        "is to hold. A departed member's weight is not spread by guess."
                    ),
                    total=str(total),
                )
            return {s: w for s, w in weights.items() if w is not None}
        if weighting is Weighting.CAP:
            floats = {m.security_id: m.free_float or ONE for m in self.recon.members}
            caps: dict[int, Decimal] = {}
            for s in members:
                cap = self.inputs.caps.get((s, today))
                if cap is None:
                    raise DataUnavailableError(
                        f"{self._sym(s)} has no market cap on {today.isoformat()}, a "
                        "rebalance of a cap-weighted index",
                        code=E_INDEX_CAP_MISSING,
                        remedy=(
                            "Refresh it (`bmk refresh` reads `duk mc`). fafnir has none for a "
                            "fund, an ETF, a secondary share class, or a security Sharadar "
                            "does not feed; supply one from a file, which is marked as such."
                        ),
                        security=self._sym(s),
                        date=today.isoformat(),
                    )
                caps[s] = cap * floats.get(s, ONE)
            total = sum(caps.values(), ZERO)
            return {s: c / total for s, c in caps.items()}
        raise AssertionError(weighting)  # pragma: no cover -- price has no targets

    def _reset(self, today: date, *, is_recon: bool, first: bool = False) -> None:
        """Set the units to the targets at today's close, keeping the level."""
        level = self.inputs.meta.base_level if first else self.price_level
        if self.method.weighting is Weighting.PRICE:
            # One unit of each member; the divisor carries the level.
            self.units = dict.fromkeys(sorted(self.members), ONE)
            total = sum((self._close(s, today) for s in self.units), ZERO)
            self.divisor = total / level
            for s in self.units:
                close = self._close(s, today)
                self.weights.append(RebalanceWeight(today, s, close / total, close, ONE))
            return
        targets = self._targets(today)
        caps = (
            {s: self.inputs.caps[(s, today)] for s in targets}
            if self.method.weighting is Weighting.CAP
            else {}
        )
        self.divisor = ONE
        self.units = {}
        for s in sorted(targets):
            close = self._close(s, today)
            units = targets[s] * level / close
            self.units[s] = units
            self.weights.append(
                RebalanceWeight(today, s, targets[s], close, units, caps.get(s))
            )
        assert is_recon or targets

    def _record(
        self,
        today: date,
        points: Decimal,
        *,
        rebalanced: bool,
        reconstituted: bool,
        used: Mapping[int, Decimal],
    ) -> None:
        self.levels.append(
            LevelRow(
                level_date=today,
                price_level=self.price_level,
                total_return_level=self.tr_level,
                dividend_points=points,
                divisor=self.divisor,
                is_rebalance=rebalanced,
                is_reconstitution=reconstituted,
                member_count=len(used),
            )
        )
        # The units each security was valued at today; the first day's are
        # those set at its close.
        for s, u in used.items():
            self.held[s].append((today, u))

    def _spans(self) -> list[HoldingSpan]:
        position = {d: i for i, d in enumerate(self.days)}
        spans: list[HoldingSpan] = []
        for sid in sorted(self.held):
            run_start, run_end, run_units = None, None, None
            for on, units in self.held[sid]:
                contiguous = run_end is not None and position[on] == position[run_end] + 1
                if run_units is not None and contiguous and units == run_units:
                    run_end = on
                    continue
                if run_start is not None:
                    assert run_end is not None and run_units is not None
                    spans.append(HoldingSpan(sid, run_start, run_end, run_units))
                run_start, run_end, run_units = on, on, units
            if run_start is not None:
                assert run_end is not None and run_units is not None
                spans.append(HoldingSpan(sid, run_start, run_end, run_units))
        return spans

    # ── the distribution safeguard ───────────────────────────────────────────

    def _check_distributions(self) -> None:
        """Every security held declares how it pays, and the source agrees (answer 3)."""
        by_security: dict[int, list[DistributionSchedule]] = defaultdict(list)
        for schedule in self.inputs.schedules:
            by_security[schedule.security_id].append(schedule)
        ex_dates: dict[int, list[date]] = defaultdict(list)
        for dividend in self.inputs.dividends:
            ex_dates[dividend.security_id].append(dividend.ex_date)
        position = {d: i for i, d in enumerate(self.days)}

        for sid in sorted(self.held):
            schedules = sorted(by_security[sid], key=lambda s: s.effective_from)
            days = [on for on, _ in self.held[sid]]

            def in_force(
                on: date, schedules: list[DistributionSchedule] = schedules
            ) -> DistributionSchedule | None:
                found = None
                for schedule in schedules:
                    if schedule.effective_from <= on:
                        found = schedule
                return found

            runs: list[tuple[date, date, DistributionSchedule]] = []
            for on in days:
                found = in_force(on)
                if found is None:
                    raise ValidationError(
                        f"{self._sym(sid)} is held on {on.isoformat()} with no distribution "
                        "schedule declared",
                        code=E_INDEX_NO_SCHEDULE,
                        remedy=(
                            "`bmk schedule propose` reads its dividend history and "
                            "`bmk schedule accept` declares what it proposes; or "
                            "`bmk schedule set`. A missing dividend would understate the "
                            "total return, so how a security pays is declared."
                        ),
                        security=self._sym(sid),
                    )
                last = runs[-1] if runs else None
                if (
                    last is not None
                    and last[2] is found
                    and position[on] == position[last[1]] + 1
                ):
                    runs[-1] = (last[0], on, found)
                else:
                    runs.append((on, on, found))
            for start, end, schedule in runs:
                self._check_run(sid, start, end, schedule, sorted(ex_dates[sid]))

    def _check_run(
        self,
        sid: int,
        start: date,
        end: date,
        schedule: DistributionSchedule,
        exes: list[date],
    ) -> None:
        within = [x for x in exes if start <= x <= end]
        if schedule.schedule is Schedule.NONE:
            if within:
                raise ValidationError(
                    f"{self._sym(sid)} is declared a non-payer from "
                    f"{schedule.effective_from.isoformat()}, and went ex a dividend on "
                    f"{within[0].isoformat()}",
                    code=E_INDEX_DIVIDEND_GAP,
                    remedy="Declare the schedule it pays on, from when it began to pay.",
                    security=self._sym(sid),
                )
            return
        allowed = schedule.max_gap_days or GAP_DAYS[schedule.schedule]
        before = [x for x in exes if x < start]
        points = [before[-1] if before else start, *within]
        for a, b in zip(points, [*points[1:], end], strict=False):
            if (b - a).days > allowed:
                raise ValidationError(
                    f"{self._sym(sid)} is declared {schedule.schedule}, and the source "
                    f"has no dividend from {a.isoformat()} to {b.isoformat()}, "
                    f"{(b - a).days} days, more than the {allowed} it allows",
                    code=E_INDEX_DIVIDEND_GAP,
                    remedy=(
                        "Refresh the dividends; supply a missing one from a file; or, if it "
                        "stopped paying, declare that from the date it did. A missing "
                        "dividend understates the total return silently."
                    ),
                    security=self._sym(sid),
                    gap_from=a.isoformat(),
                    gap_to=b.isoformat(),
                    allowed_days=allowed,
                )


# ── reading and writing a file ───────────────────────────────────────────────


def inputs_of(repos: IndexRepositories) -> IndexInputs:
    """Everything a `.bmk` file holds that a build reads."""
    return IndexInputs(
        meta=repos.index_meta(),
        securities={s.security_id: s for s in repos.securities()},
        methodologies=repos.methodologies(),
        reconstitutions=repos.reconstitutions(),
        events=repos.events(),
        schedules=repos.schedules(),
        trading_days=repos.trading_days(),
        closes={key: c.close for key, c in repos.closes().items()},
        splits=repos.splits(),
        dividends=repos.dividends(),
        caps={key: c.market_cap for key, c in repos.market_caps().items()},
    )


def build(repos: IndexRepositories, *, through: date | None, built_by: str) -> IndexBuild:
    """Construct the index and replace the file's built rows with the result."""
    built = construct(inputs_of(repos), through=through)
    record = IndexBuild(
        inputs_hash=repos.inputs_hash(),
        first_date=built.first_date,
        last_date=built.last_date,
        built_at=repos.clock.stamp(),
        built_by=built_by,
    )
    repos.clear_built()
    repos.write_built(record, built.levels, built.holdings, built.weights, built.reinvestments)
    return record


def current_build(repos: IndexRepositories) -> IndexBuild:
    """The build, refused if there is none or the inputs have changed since."""
    record = repos.build()
    if record is None:
        raise DataUnavailableError(
            "the index has not been built",
            code=E_INDEX_NOT_BUILT,
            remedy="`bmk refresh`, then `bmk build`.",
        )
    if record.inputs_hash != repos.inputs_hash():
        raise DataUnavailableError(
            "the index was built from inputs that have changed since",
            code=E_INDEX_NOT_BUILT,
            remedy=(
                "`bmk build` again. Its levels would no longer be the ones its definition "
                "and inputs give."
            ),
            built_from=record.inputs_hash,
        )
    return record


@dataclass(frozen=True, slots=True)
class Holding:
    """One security on a built day: its units, close, value and weight."""

    security: SyntheticSecurity
    units: Decimal
    close: Decimal
    #: Its value in index points: units x close / divisor.
    points: Decimal
    weight: Decimal


def composition(repos: IndexRepositories, on: date) -> tuple[LevelRow, list[Holding]]:
    """What the index held on a built day, valued at that day's closes."""
    current_build(repos)
    levels = repos.levels(on, on)
    if not levels:
        raise DataUnavailableError(
            f"{on.isoformat()} is not a day the index was built for",
            code=E_INDEX_NOT_BUILT,
            remedy="Ask for a trading day between the base date and the build's last day.",
        )
    (level,) = levels
    closes = repos.closes()
    held: list[tuple[SyntheticSecurity, Decimal, Decimal]] = []
    for span in repos.holdings_on(on):
        close = closes.get((span.security_id, on))
        if close is None:
            # Valued that day at what it paid: a member leaving by its event.
            event = next(
                e
                for e in repos.events()
                if e.security_id == span.security_id and e.event_date == on
            )
            price = (
                event.cash_per_share
                if event.cash_per_share is not None
                else (event.received_ratio or ZERO)
                * closes[(event.received_security_id or 0, on)].close
            )
        else:
            price = close.close
        held.append((repos.security(span.security_id), span.units, price))
    with money_context():
        total = sum((u * p for _, u, p in held), ZERO)
        holdings = [
            Holding(
                security=security,
                units=units,
                close=price,
                points=units * price / level.divisor,
                weight=units * price / total,
            )
            for security, units, price in held
        ]
    return level, holdings


def verify(repos: IndexRepositories) -> list[tuple[str, str]]:
    """What is wrong with a file: its build, and whether a rebuild reproduces it.

    Read-only: the rebuild runs in a transaction that is always rolled back, so
    the file is byte-identical afterwards (ADR 0016's rule for `pt validate`).
    """
    problems: list[tuple[str, str]] = [
        ("meta", f"missing required metadata key {key!r}") for key in repos.missing_meta()
    ]
    if problems:
        return problems
    record = repos.build()
    if record is None:
        problems.append(("build", "the index has not been built"))
    elif record.inputs_hash != repos.inputs_hash():
        problems.append(("build", "built from inputs that have changed since; `bmk build`"))
    stored = repos.built_digest()
    try:
        with scratch_transaction(repos.con):
            built = construct(inputs_of(repos), through=record.last_date if record else None)
            repos.clear_built()
            if record is not None:
                repos.write_built(
                    record, built.levels, built.holdings, built.weights, built.reinvestments
                )
            rebuilt = repos.built_digest()
    except PortableError as cannot:
        problems.append(("definition", f"[{cannot.code}] {cannot.message}"))
        return problems
    if record is not None and rebuilt != stored:
        problems.append(
            ("rebuild", "the stored levels are not the ones a rebuild gives; `bmk build`")
        )
    return problems
