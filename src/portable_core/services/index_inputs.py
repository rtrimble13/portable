"""Refreshing what an index is built from: calendar, closes, actions, caps. ADR 0050 §6.

A refresh adds what is new. It does not change what is already stored: a
vendor's correction, a dividend the source now reports differently, a session
that is no longer one. Each of those would move levels already built, so it is
refused, naming every row, unless the owner restates with a reason, and then
each change is logged in ``input_restatement``.

Dividends are read from a year before the base date, so a security's schedule
can be checked from the first day it is held (answer 3). Closes are read from
the base date. Market caps are read for a cap-weighted methodology only, on
the dates it rebalances: its base, each reconstitution and methodology change,
and each period end its rule names.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Final

from portable_core.domain.index import (
    Close,
    Dividend,
    MarketCap,
    RebalanceRule,
    Split,
    SyntheticSecurity,
    Weighting,
)
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_INDEX_RESTATEMENT
from portable_core.persistence.index_repositories import IndexRepositories
from portable_core.providers import (
    Capability,
    IndexSecurityKey,
    MarketDataProvider,
    as_index_data_provider,
    as_trading_calendar_provider,
    require_capability,
)

__all__ = ["RefreshReport", "cap_dates", "refresh"]

#: How far before the base date dividends are read: far enough to hold an
#: annual payer's last payment before the index starts.
DIVIDEND_LOOKBACK: Final = timedelta(days=400)

_SPLITS: Final = frozenset({"split", "reverse_split"})
_DIVIDENDS: Final = frozenset({"cash_dividend", "dividend"})


@dataclass(slots=True)
class RefreshReport:
    """What a refresh added, restated, and could not take."""

    through: date
    trading_days: int = 0
    closes: int = 0
    splits: int = 0
    dividends: int = 0
    market_caps: int = 0
    restated: int = 0
    #: Actions the source lists that an index does not read by itself: a
    #: spinoff or a merger is declared with `bmk event add`.
    not_read: list[str] = field(default_factory=list)


def _key(security: SyntheticSecurity) -> IndexSecurityKey:
    return IndexSecurityKey(security.symbol, security.fafnir_sid)


class _Changes:
    """New rows, and rows the source now states differently."""

    def __init__(self) -> None:
        self.conflicts: list[tuple[str, str, str | None, str | None]] = []

    def differ(self, table: str, row_key: str, old: str | None, new: str | None) -> None:
        self.conflicts.append((table, row_key, old, new))


def refresh(
    repos: IndexRepositories,
    provider: MarketDataProvider,
    *,
    through: date,
    restate_reason: str | None = None,
) -> RefreshReport:
    """Bring the file's inputs up to *through* from *provider*."""
    meta = repos.index_meta()
    if through < meta.base_date:
        raise ValidationError(
            f"{through.isoformat()} precedes the base date {meta.base_date.isoformat()}",
            remedy="Refresh through a date on or after the base date.",
        )
    source = as_index_data_provider(provider)
    report = RefreshReport(through=through)
    changes = _Changes()
    stamp = repos.clock.stamp()

    # The calendar.
    calendar = as_trading_calendar_provider(provider).trading_calendar(
        meta.calendar, meta.base_date, through
    )
    last_known = min(through, calendar.last)
    sessions = sorted(d for d in calendar.open_days if meta.base_date <= d <= last_known)
    held_days = repos.trading_days()
    stored = set(held_days)
    gone = sorted(
        d for d in stored if meta.base_date <= d <= last_known and d not in calendar.open_days
    )
    for day in gone:
        changes.differ("trading_day", f"trade_date={day.isoformat()}", "open", "closed")
    new_days = [d for d in sessions if d not in stored]
    report.trading_days = len(new_days)

    securities = _securities(repos)
    closes = repos.closes()
    splits = {(s.security_id, s.ex_date): s for s in repos.splits()}
    dividends: dict[int, dict[tuple[date, Decimal], Dividend]] = defaultdict(dict)
    for d in repos.dividends():
        dividends[d.security_id][(d.ex_date, d.amount)] = d

    new_closes: list[tuple[Close, bool]] = []
    new_splits: list[tuple[Split, bool]] = []
    new_dividends: list[Dividend] = []
    dropped_dividends: list[int] = []
    for security in securities:
        key = _key(security)
        for price in source.index_closes(key, meta.base_date, through):
            row = Close(
                security_id=security.security_id,
                trade_date=price.price_date,
                close=price.price,
                source=price.source,
                as_of=price.as_of.isoformat(),
            )
            held = closes.get((security.security_id, price.price_date))
            if held is None:
                new_closes.append((row, False))
            elif held.close != price.price:
                changes.differ(
                    "close",
                    f"security_id={security.security_id};trade_date={price.price_date}",
                    str(held.close),
                    str(price.price),
                )
                new_closes.append((row, True))
        seen: set[tuple[date, Decimal]] = set()
        start = meta.base_date - DIVIDEND_LOOKBACK
        for action in source.index_actions(key, start, through):
            if action.action_type in _SPLITS:
                assert (
                    action.split_numerator is not None and action.split_denominator is not None
                )
                split = Split(
                    security_id=security.security_id,
                    ex_date=action.ex_date,
                    numerator=action.split_numerator,
                    denominator=action.split_denominator,
                    source=action.provider_ref or provider.name,
                    as_of=stamp,
                )
                held_split = splits.get((security.security_id, action.ex_date))
                if held_split is None:
                    new_splits.append((split, False))
                elif held_split.ratio != split.ratio:
                    changes.differ(
                        "split",
                        f"security_id={security.security_id};ex_date={action.ex_date}",
                        f"{held_split.numerator}:{held_split.denominator}",
                        f"{split.numerator}:{split.denominator}",
                    )
                    new_splits.append((split, True))
            elif action.action_type in _DIVIDENDS:
                assert action.cash_amount is not None
                pair = (action.ex_date, action.cash_amount)
                seen.add(pair)
                if pair not in dividends[security.security_id]:
                    new_dividends.append(
                        Dividend(
                            dividend_id=0,
                            security_id=security.security_id,
                            ex_date=action.ex_date,
                            amount=action.cash_amount,
                            source=action.provider_ref or provider.name,
                            as_of=stamp,
                            pay_date=action.pay_date,
                        )
                    )
            else:
                report.not_read.append(
                    f"{security.symbol} {action.action_type} ex {action.ex_date.isoformat()}"
                )
        # A dividend stored, in the range read, that the source no longer lists.
        for (ex, amount), held_dividend in sorted(dividends[security.security_id].items()):
            if start <= ex <= through and (ex, amount) not in seen:
                changes.differ(
                    "dividend",
                    f"security_id={security.security_id};ex_date={ex.isoformat()}",
                    str(amount),
                    None,
                )
                dropped_dividends.append(held_dividend.dividend_id)
        # A changed amount reads as one dropped and one added; only the drop is
        # a restatement, so a new amount waits for it.
        if dropped_dividends:
            changed_days = {
                d.ex_date for d in repos.dividends() if d.dividend_id in dropped_dividends
            }
            for d in new_dividends:
                if d.security_id == security.security_id and d.ex_date in changed_days:
                    changes.differ(
                        "dividend",
                        f"security_id={d.security_id};ex_date={d.ex_date.isoformat()}",
                        None,
                        str(d.amount),
                    )

    new_caps, cap_conflicts = _caps(repos, provider, securities, sessions, through)
    changes.conflicts.extend(cap_conflicts)

    if changes.conflicts and restate_reason is None:
        raise ValidationError(
            f"the source now states {len(changes.conflicts)} input(s) differently from what "
            "the index was built from",
            code=E_INDEX_RESTATEMENT,
            remedy=(
                "Each would move levels already built. If the source is right, refresh "
                "again with --restate REASON; each change is logged. Otherwise refresh "
                "from the source the index was built from."
            ),
            changes=[
                f"{table} {row}: {old} -> {new}"
                for table, row, old, new in changes.conflicts[:20]
            ],
            more=max(0, len(changes.conflicts) - 20),
        )

    # Write.
    repos.add_trading_days(new_days, calendar.source, stamp)
    if gone:
        assert restate_reason is not None
        repos.remove_trading_days(gone)
    for close, replace in new_closes:
        repos.put_close(close, replace=replace)
    for split, replace in new_splits:
        repos.put_split(split, replace=replace)
    for dividend_id in dropped_dividends:
        repos.remove_dividend(dividend_id)
    for dividend in new_dividends:
        repos.add_dividend(dividend)
    for cap, replace in new_caps:
        repos.put_market_cap(cap, replace=replace)
    for table, row_key, old, new in changes.conflicts:
        assert restate_reason is not None
        repos.log_restatement(table, row_key, old, new, restate_reason)

    report.closes = sum(1 for _, r in new_closes if not r)
    report.splits = sum(1 for _, r in new_splits if not r)
    report.dividends = len(new_dividends)
    report.market_caps = sum(1 for _, r in new_caps if not r)
    report.restated = len(changes.conflicts)
    return report


def _securities(repos: IndexRepositories) -> list[SyntheticSecurity]:
    """Every security a membership or an event names."""
    named = {m.security_id for r in repos.reconstitutions() for m in r.members}
    named |= {e.received_security_id for e in repos.events() if e.received_security_id}
    return [repos.security(sid) for sid in sorted(named)]


def cap_dates(repos: IndexRepositories, sessions: list[date]) -> dict[date, set[int]]:
    """The dates a cap-weighted methodology rebalances, and who it weighs on each."""
    methods = repos.methodologies()
    recons = repos.reconstitutions()
    days = sorted(set(sessions) | set(repos.trading_days()))
    following = dict(pairwise(days))
    wanted: dict[date, set[int]] = {}
    for index, method in enumerate(methods):
        if method.weighting is not Weighting.CAP:
            continue
        end = methods[index + 1].effective_from if index + 1 < len(methods) else date.max
        span = [d for d in days if method.effective_from <= d < end]
        for day in span:
            members = [r for r in recons if r.effective_date <= day]
            if not members:
                continue
            due = day == method.effective_from or any(r.effective_date == day for r in recons)
            rule = method.rebalance_rule
            nxt = following.get(day)
            if rule is RebalanceRule.DAILY:
                due = True
            elif rule is not RebalanceRule.NEVER and nxt is not None and nxt.month != day.month:
                months = {
                    RebalanceRule.MONTHLY: range(1, 13),
                    RebalanceRule.QUARTERLY: (3, 6, 9, 12),
                    RebalanceRule.SEMIANNUAL: (6, 12),
                    RebalanceRule.ANNUAL: (12,),
                }[rule]
                due = due or day.month in months
            if due:
                wanted[day] = {m.security_id for m in members[-1].members}
    return wanted


def _caps(
    repos: IndexRepositories,
    provider: MarketDataProvider,
    securities: list[SyntheticSecurity],
    sessions: list[date],
    through: date,
) -> tuple[list[tuple[MarketCap, bool]], list[tuple[str, str, str | None, str | None]]]:
    wanted = cap_dates(repos, sessions)
    wanted = {d: ids for d, ids in wanted.items() if d <= through}
    if not wanted:
        return [], []
    require_capability(provider, Capability.MARKET_CAP)
    source = as_index_data_provider(provider)
    by_id = {s.security_id: s for s in securities}
    ids = sorted({sid for members in wanted.values() for sid in members})
    keys = {sid: _key(by_id[sid]) for sid in ids}
    read = source.index_market_caps(list(keys.values()), min(wanted), max(wanted))
    held = repos.market_caps()
    stamp = repos.clock.stamp()
    new: list[tuple[MarketCap, bool]] = []
    conflicts: list[tuple[str, str, str | None, str | None]] = []
    for sid, key in keys.items():
        for record in read.get(key, []):
            if sid not in wanted.get(record.cap_date, set()):
                continue
            cap = MarketCap(
                security_id=sid,
                cap_date=record.cap_date,
                market_cap=record.market_cap,
                source=record.source,
                as_of=stamp,
                close_raw=record.close_raw,
                shares_outstanding=record.shares_outstanding,
                share_factor=record.share_factor,
                fiscal_date=record.fiscal_date,
                known_from=record.known_from,
            )
            stored = held.get((sid, record.cap_date))
            if stored is None:
                new.append((cap, False))
            elif stored.market_cap != record.market_cap:
                conflicts.append(
                    (
                        "market_cap",
                        f"security_id={sid};cap_date={record.cap_date.isoformat()}",
                        str(stored.market_cap),
                        str(record.market_cap),
                    )
                )
                new.append((cap, True))
    return new, conflicts
