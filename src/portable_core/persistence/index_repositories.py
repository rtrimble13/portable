"""Repositories over a `.bmk` index file: the only place its SQL lives. ADR 0050.

The `.port`'s conventions hold (ADR 0002): explicit SQL, Decimal written as
canonical text and read back with :func:`~portable_core.persistence.mappers.to_decimal`,
every stamp from the injected clock (ADR 0035). A definition row that has
been corrected is kept and never read: the ``effective`` readers return the
rows nothing corrects (ADR 0025 decision 5).
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from typing import Any, ClassVar, Final

from portable_core.clock import SYSTEM_CLOCK, Clock
from portable_core.decimals import to_text
from portable_core.domain.index import (
    Close,
    DistributionSchedule,
    Dividend,
    EventType,
    HoldingSpan,
    IndexBuild,
    IndexMeta,
    LevelRow,
    MarketCap,
    Member,
    Methodology,
    RebalanceRule,
    RebalanceWeight,
    Reconstitution,
    Reinvestment,
    Schedule,
    SecurityEvent,
    SecurityType,
    Split,
    SyntheticSecurity,
    Weighting,
)
from portable_core.errors import PortfolioFileError, ValidationError
from portable_core.errors.kinds import E_INDEX_DEFINITION, E_PORTFOLIO_CORRUPT
from portable_core.persistence.mappers import to_date, to_decimal

__all__ = ["IndexRepositories"]

#: The keys `meta` must hold (`bmk validate`).
REQUIRED_META: Final = (
    "schema_version",
    "index_name",
    "description",
    "base_date",
    "base_level",
    "currency",
    "calendar",
    "created_at",
    "portable_version",
)


def _d(value: Any) -> Decimal | None:
    return None if value is None else to_decimal(value)


def _t(value: Decimal | None) -> str | None:
    return None if value is None else to_text(value)


def _day(value: Any) -> date | None:
    return None if value is None else to_date(value)


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


class IndexRepositories:
    """Every read and write of one `.bmk` file, over one connection."""

    #: The tables `bmk build` replaces, children first.
    DERIVED: ClassVar[tuple[str, ...]] = (
        "reinvestment",
        "rebalance_weight",
        "holding",
        "level",
        "build",
    )

    #: What the inputs hash covers, each in a fixed order (ADR 0050, answer 8):
    #: the definition and the reference rows, never the built ones, and never
    #: a stamp of when a row was written.
    HASHED: ClassVar[dict[str, tuple[str, str]]] = {
        "meta": ("key, value", "key NOT IN ('created_at', 'portable_version', 'updated_at')"),
        "security": (
            "security_id, fafnir_sid, symbol, name, security_type, currency, issuer_key",
            "1",
        ),
        "methodology": (
            "methodology_id, effective_from, weighting, rebalance_rule, drift_band, "
            "corrects_methodology_id",
            "1",
        ),
        "reconstitution": (
            "reconstitution_id, effective_date, corrects_reconstitution_id",
            "1",
        ),
        "reconstitution_member": (
            "reconstitution_id, security_id, weight, float_factor, float_source",
            "1",
        ),
        "security_event": (
            "event_id, security_id, event_date, event_type, cash_per_share, "
            "received_security_id, received_ratio, corrects_event_id",
            "1",
        ),
        "distribution_schedule": (
            "schedule_id, security_id, effective_from, schedule, max_gap_days, "
            "corrects_schedule_id",
            "1",
        ),
        "trading_day": ("trade_date", "1"),
        "close": ("security_id, trade_date, close", "1"),
        "split": ("security_id, ex_date, numerator, denominator", "1"),
        "dividend": ("dividend_id, security_id, ex_date, amount", "1"),
        "market_cap": ("security_id, cap_date, market_cap", "1"),
    }

    def __init__(self, con: sqlite3.Connection, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.con = con
        self.clock = clock

    # ── meta ─────────────────────────────────────────────────────────────────

    def meta_get(self, key: str) -> str | None:
        row = self.con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def meta_set(self, key: str, value: str) -> None:
        self.con.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def meta_all(self) -> dict[str, str]:
        return {str(r[0]): str(r[1]) for r in self.con.execute("SELECT key, value FROM meta")}

    def missing_meta(self) -> list[str]:
        held = self.meta_all()
        return [key for key in REQUIRED_META if key not in held]

    def index_meta(self) -> IndexMeta:
        missing = self.missing_meta()
        if missing:
            raise PortfolioFileError(
                f"index metadata is missing {', '.join(missing)}",
                code=E_PORTFOLIO_CORRUPT,
                remedy="Run `bmk validate` for the full list of what is wrong.",
                missing=missing,
            )
        held = self.meta_all()
        return IndexMeta(
            index_name=held["index_name"],
            description=held["description"],
            base_date=to_date(held["base_date"]),
            base_level=to_decimal(held["base_level"]),
            currency=held["currency"],
            calendar=held["calendar"],
        )

    # ── securities ───────────────────────────────────────────────────────────

    def add_security(self, security: SyntheticSecurity) -> int:
        cur = self.con.execute(
            "INSERT INTO security (fafnir_sid, symbol, name, security_type, currency, "
            "issuer_key, source, provider_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                security.fafnir_sid,
                security.symbol,
                security.name,
                str(security.security_type),
                security.currency,
                security.issuer_key,
                security.source,
                security.provider_ref,
                self.clock.stamp(),
            ),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def add_symbol(
        self,
        security_id: int,
        symbol: str,
        valid_from: date,
        valid_to: date | None,
        source: str,
    ) -> None:
        self.con.execute(
            "INSERT OR IGNORE INTO security_symbol_history (security_id, symbol, valid_from, "
            "valid_to, source) VALUES (?, ?, ?, ?, ?)",
            (security_id, symbol, valid_from.isoformat(), _iso(valid_to), source),
        )

    def securities(self) -> list[SyntheticSecurity]:
        return [
            self._security(r)
            for r in self.con.execute("SELECT * FROM security ORDER BY security_id")
        ]

    def security(self, security_id: int) -> SyntheticSecurity:
        row = self.con.execute(
            "SELECT * FROM security WHERE security_id = ?", (security_id,)
        ).fetchone()
        if row is None:
            raise ValidationError(
                f"the index holds no security {security_id}",
                code=E_INDEX_DEFINITION,
                security_id=security_id,
            )
        return self._security(row)

    def by_fafnir_sid(self, sid: int) -> SyntheticSecurity | None:
        row = self.con.execute("SELECT * FROM security WHERE fafnir_sid = ?", (sid,)).fetchone()
        return None if row is None else self._security(row)

    def resolve(self, symbol: str, on: date) -> list[SyntheticSecurity]:
        """The securities that traded as *symbol* on *on*: by history, then by name."""
        rows = self.con.execute(
            "SELECT DISTINCT s.* FROM security s JOIN security_symbol_history h "
            "USING (security_id) WHERE h.symbol = ? AND h.valid_from <= ? "
            "AND (h.valid_to IS NULL OR h.valid_to >= ?) ORDER BY s.security_id",
            (symbol, on.isoformat(), on.isoformat()),
        ).fetchall()
        if not rows:
            rows = self.con.execute(
                "SELECT * FROM security WHERE symbol = ? ORDER BY security_id", (symbol,)
            ).fetchall()
        return [self._security(r) for r in rows]

    @staticmethod
    def _security(r: sqlite3.Row) -> SyntheticSecurity:
        return SyntheticSecurity(
            security_id=int(r["security_id"]),
            fafnir_sid=None if r["fafnir_sid"] is None else int(r["fafnir_sid"]),
            symbol=str(r["symbol"]),
            name=r["name"],
            security_type=SecurityType(r["security_type"]),
            currency=str(r["currency"]),
            issuer_key=r["issuer_key"],
            source=str(r["source"]),
            provider_ref=r["provider_ref"],
        )

    # ── definition ───────────────────────────────────────────────────────────

    def add_methodology(self, m: Methodology) -> int:
        cur = self.con.execute(
            "INSERT INTO methodology (effective_from, weighting, rebalance_rule, drift_band, "
            "note, created_at, corrects_methodology_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                m.effective_from.isoformat(),
                str(m.weighting),
                str(m.rebalance_rule),
                _t(m.drift_band),
                m.note,
                self.clock.stamp(),
                m.corrects_methodology_id,
            ),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def methodologies(self, *, include_corrected: bool = False) -> list[Methodology]:
        where = (
            ""
            if include_corrected
            else (
                " WHERE methodology_id NOT IN (SELECT corrects_methodology_id FROM methodology "
                "WHERE corrects_methodology_id IS NOT NULL)"
            )
        )
        return [
            Methodology(
                methodology_id=int(r["methodology_id"]),
                effective_from=to_date(r["effective_from"]),
                weighting=Weighting(r["weighting"]),
                rebalance_rule=RebalanceRule(r["rebalance_rule"]),
                drift_band=_d(r["drift_band"]),
                note=r["note"],
                corrects_methodology_id=r["corrects_methodology_id"],
            )
            for r in self.con.execute(
                f"SELECT * FROM methodology{where} ORDER BY effective_from, methodology_id"  # noqa: S608
            )
        ]

    def add_reconstitution(self, recon: Reconstitution) -> int:
        cur = self.con.execute(
            "INSERT INTO reconstitution (effective_date, note, created_at, "
            "corrects_reconstitution_id) VALUES (?, ?, ?, ?)",
            (
                recon.effective_date.isoformat(),
                recon.note,
                self.clock.stamp(),
                recon.corrects_reconstitution_id,
            ),
        )
        assert cur.lastrowid is not None
        row_id = int(cur.lastrowid)
        self.con.executemany(
            "INSERT INTO reconstitution_member (reconstitution_id, security_id, weight, "
            "float_factor, float_source) VALUES (?, ?, ?, ?, ?)",
            [
                (row_id, m.security_id, _t(m.weight), _t(m.float_factor), m.float_source)
                for m in recon.members
            ],
        )
        return row_id

    def reconstitutions(self, *, include_corrected: bool = False) -> list[Reconstitution]:
        where = (
            ""
            if include_corrected
            else (
                " WHERE reconstitution_id NOT IN (SELECT corrects_reconstitution_id FROM "
                "reconstitution WHERE corrects_reconstitution_id IS NOT NULL)"
            )
        )
        found: list[Reconstitution] = []
        for r in self.con.execute(
            f"SELECT * FROM reconstitution{where} ORDER BY effective_date, reconstitution_id"  # noqa: S608
        ).fetchall():
            members = tuple(
                Member(
                    security_id=int(m["security_id"]),
                    weight=_d(m["weight"]),
                    float_factor=_d(m["float_factor"]),
                    float_source=m["float_source"],
                )
                for m in self.con.execute(
                    "SELECT * FROM reconstitution_member WHERE reconstitution_id = ? "
                    "ORDER BY security_id",
                    (r["reconstitution_id"],),
                )
            )
            found.append(
                Reconstitution(
                    reconstitution_id=int(r["reconstitution_id"]),
                    effective_date=to_date(r["effective_date"]),
                    members=members,
                    note=r["note"],
                    corrects_reconstitution_id=r["corrects_reconstitution_id"],
                )
            )
        return found

    def add_event(self, e: SecurityEvent) -> int:
        cur = self.con.execute(
            "INSERT INTO security_event (security_id, event_date, event_type, cash_per_share, "
            "received_security_id, received_ratio, source, note, created_at, "
            "corrects_event_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                e.security_id,
                e.event_date.isoformat(),
                str(e.event_type),
                _t(e.cash_per_share),
                e.received_security_id,
                _t(e.received_ratio),
                e.source,
                e.note,
                self.clock.stamp(),
                e.corrects_event_id,
            ),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def events(self, *, include_corrected: bool = False) -> list[SecurityEvent]:
        where = (
            ""
            if include_corrected
            else (
                " WHERE event_id NOT IN (SELECT corrects_event_id FROM security_event "
                "WHERE corrects_event_id IS NOT NULL)"
            )
        )
        return [
            SecurityEvent(
                event_id=int(r["event_id"]),
                security_id=int(r["security_id"]),
                event_date=to_date(r["event_date"]),
                event_type=EventType(r["event_type"]),
                source=str(r["source"]),
                cash_per_share=_d(r["cash_per_share"]),
                received_security_id=r["received_security_id"],
                received_ratio=_d(r["received_ratio"]),
                note=r["note"],
                corrects_event_id=r["corrects_event_id"],
            )
            for r in self.con.execute(
                f"SELECT * FROM security_event{where} ORDER BY event_date, event_id"  # noqa: S608
            )
        ]

    def add_schedule(self, s: DistributionSchedule) -> int:
        cur = self.con.execute(
            "INSERT INTO distribution_schedule (security_id, effective_from, schedule, "
            "max_gap_days, source, created_at, corrects_schedule_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                s.security_id,
                s.effective_from.isoformat(),
                str(s.schedule),
                s.max_gap_days,
                s.source,
                self.clock.stamp(),
                s.corrects_schedule_id,
            ),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def schedules(self, *, include_corrected: bool = False) -> list[DistributionSchedule]:
        where = (
            ""
            if include_corrected
            else (
                " WHERE schedule_id NOT IN (SELECT corrects_schedule_id FROM "
                "distribution_schedule WHERE corrects_schedule_id IS NOT NULL)"
            )
        )
        return [
            DistributionSchedule(
                schedule_id=int(r["schedule_id"]),
                security_id=int(r["security_id"]),
                effective_from=to_date(r["effective_from"]),
                schedule=Schedule(r["schedule"]),
                source=str(r["source"]),
                max_gap_days=r["max_gap_days"],
                corrects_schedule_id=r["corrects_schedule_id"],
            )
            for r in self.con.execute(
                f"SELECT * FROM distribution_schedule{where} "  # noqa: S608
                "ORDER BY security_id, effective_from, schedule_id"
            )
        ]

    # ── reference inputs ─────────────────────────────────────────────────────

    def trading_days(self) -> list[date]:
        return [
            to_date(r[0])
            for r in self.con.execute("SELECT trade_date FROM trading_day ORDER BY trade_date")
        ]

    def add_trading_days(self, days: Iterable[date], source: str, as_of: str) -> None:
        self.con.executemany(
            "INSERT OR IGNORE INTO trading_day (trade_date, source, as_of) VALUES (?, ?, ?)",
            [(d.isoformat(), source, as_of) for d in days],
        )

    def remove_trading_days(self, days: Iterable[date]) -> None:
        """A restated calendar: sessions the source no longer lists."""
        self.con.executemany(
            "DELETE FROM trading_day WHERE trade_date = ?", [(d.isoformat(),) for d in days]
        )

    def closes(self) -> dict[tuple[int, date], Close]:
        return {
            (int(r["security_id"]), to_date(r["trade_date"])): Close(
                security_id=int(r["security_id"]),
                trade_date=to_date(r["trade_date"]),
                close=to_decimal(r["close"]),
                source=str(r["source"]),
                as_of=str(r["as_of"]),
            )
            for r in self.con.execute("SELECT * FROM close ORDER BY security_id, trade_date")
        }

    def put_close(self, c: Close, *, replace: bool = False) -> None:
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        self.con.execute(
            f"{verb} INTO close (security_id, trade_date, close, source, as_of) "
            "VALUES (?, ?, ?, ?, ?)",
            (c.security_id, c.trade_date.isoformat(), to_text(c.close), c.source, c.as_of),
        )

    def splits(self) -> list[Split]:
        return [
            Split(
                security_id=int(r["security_id"]),
                ex_date=to_date(r["ex_date"]),
                numerator=to_decimal(r["numerator"]),
                denominator=to_decimal(r["denominator"]),
                source=str(r["source"]),
                as_of=str(r["as_of"]),
            )
            for r in self.con.execute("SELECT * FROM split ORDER BY ex_date, security_id")
        ]

    def put_split(self, s: Split, *, replace: bool = False) -> None:
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        self.con.execute(
            f"{verb} INTO split (security_id, ex_date, numerator, denominator, source, "
            "as_of) VALUES (?, ?, ?, ?, ?, ?)",
            (
                s.security_id,
                s.ex_date.isoformat(),
                to_text(s.numerator),
                to_text(s.denominator),
                s.source,
                s.as_of,
            ),
        )

    def dividends(self) -> list[Dividend]:
        return [
            Dividend(
                dividend_id=int(r["dividend_id"]),
                security_id=int(r["security_id"]),
                ex_date=to_date(r["ex_date"]),
                amount=to_decimal(r["amount"]),
                source=str(r["source"]),
                as_of=str(r["as_of"]),
                pay_date=_day(r["pay_date"]),
            )
            for r in self.con.execute(
                "SELECT * FROM dividend ORDER BY ex_date, security_id, dividend_id"
            )
        ]

    def add_dividend(self, d: Dividend) -> int:
        cur = self.con.execute(
            "INSERT INTO dividend (security_id, ex_date, pay_date, amount, source, as_of) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                d.security_id,
                d.ex_date.isoformat(),
                _iso(d.pay_date),
                to_text(d.amount),
                d.source,
                d.as_of,
            ),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def remove_dividend(self, dividend_id: int) -> None:
        self.con.execute("DELETE FROM dividend WHERE dividend_id = ?", (dividend_id,))

    def market_caps(self) -> dict[tuple[int, date], MarketCap]:
        return {
            (int(r["security_id"]), to_date(r["cap_date"])): MarketCap(
                security_id=int(r["security_id"]),
                cap_date=to_date(r["cap_date"]),
                market_cap=to_decimal(r["market_cap"]),
                source=str(r["source"]),
                as_of=str(r["as_of"]),
                close_raw=_d(r["close_raw"]),
                shares_outstanding=_d(r["shares_outstanding"]),
                share_factor=_d(r["share_factor"]),
                fiscal_date=_day(r["fiscal_date"]),
                known_from=_day(r["known_from"]),
            )
            for r in self.con.execute("SELECT * FROM market_cap ORDER BY security_id, cap_date")
        }

    def put_market_cap(self, c: MarketCap, *, replace: bool = False) -> None:
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        self.con.execute(
            f"{verb} INTO market_cap (security_id, cap_date, market_cap, close_raw, "
            "shares_outstanding, share_factor, fiscal_date, known_from, source, as_of) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                c.security_id,
                c.cap_date.isoformat(),
                to_text(c.market_cap),
                _t(c.close_raw),
                _t(c.shares_outstanding),
                _t(c.share_factor),
                _iso(c.fiscal_date),
                _iso(c.known_from),
                c.source,
                c.as_of,
            ),
        )

    def log_restatement(
        self, table: str, row_key: str, old: str | None, new: str | None, reason: str
    ) -> None:
        self.con.execute(
            "INSERT INTO input_restatement (table_name, row_key, old_value, new_value, "
            "reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (table, row_key, old, new, reason, self.clock.stamp()),
        )

    def restatements(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.con.execute("SELECT * FROM input_restatement ORDER BY restatement_id")
        ]

    # ── the inputs hash ──────────────────────────────────────────────────────

    def inputs_hash(self) -> str:
        """SHA-256 over the definition and the inputs, in a fixed order."""
        digest = hashlib.sha256()
        for table, (columns, where) in self.HASHED.items():
            digest.update(f"[{table}]\n".encode())
            for row in self.con.execute(
                f'SELECT {columns} FROM "{table}" WHERE {where} ORDER BY {columns}'  # noqa: S608
            ):
                digest.update(
                    ("\x1f".join("" if v is None else str(v) for v in row) + "\n").encode()
                )
        return digest.hexdigest()

    # ── built ────────────────────────────────────────────────────────────────

    def clear_built(self) -> None:
        for table in self.DERIVED:
            self.con.execute(f'DELETE FROM "{table}"')  # noqa: S608

    def write_built(
        self,
        build: IndexBuild,
        levels: Sequence[LevelRow],
        holdings: Sequence[HoldingSpan],
        weights: Sequence[RebalanceWeight],
        reinvestments: Sequence[Reinvestment],
    ) -> None:
        self.con.execute(
            "INSERT INTO build (build_id, inputs_hash, first_date, last_date, built_at, "
            "built_by) VALUES (1, ?, ?, ?, ?, ?)",
            (
                build.inputs_hash,
                build.first_date.isoformat(),
                build.last_date.isoformat(),
                build.built_at,
                build.built_by,
            ),
        )
        self.con.executemany(
            "INSERT INTO level (level_date, price_level, total_return_level, "
            "dividend_points, divisor, is_rebalance, is_reconstitution, member_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    lv.level_date.isoformat(),
                    to_text(lv.price_level),
                    to_text(lv.total_return_level),
                    to_text(lv.dividend_points),
                    to_text(lv.divisor),
                    int(lv.is_rebalance),
                    int(lv.is_reconstitution),
                    lv.member_count,
                )
                for lv in levels
            ],
        )
        self.con.executemany(
            "INSERT INTO holding (security_id, from_date, to_date, units) VALUES (?, ?, ?, ?)",
            [
                (
                    h.security_id,
                    h.from_date.isoformat(),
                    h.to_date.isoformat(),
                    to_text(h.units),
                )
                for h in holdings
            ],
        )
        self.con.executemany(
            "INSERT INTO rebalance_weight (rebalance_date, security_id, target_weight, close, "
            "units, weighting_cap) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    w.rebalance_date.isoformat(),
                    w.security_id,
                    to_text(w.target_weight),
                    to_text(w.close),
                    to_text(w.units),
                    _t(w.weighting_cap),
                )
                for w in weights
            ],
        )
        self.con.executemany(
            "INSERT INTO reinvestment (dividend_id, level_date, security_id, units, amount, "
            "index_points) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    r.dividend_id,
                    r.level_date.isoformat(),
                    r.security_id,
                    to_text(r.units),
                    to_text(r.amount),
                    to_text(r.index_points),
                )
                for r in reinvestments
            ],
        )

    def build(self) -> IndexBuild | None:
        r = self.con.execute("SELECT * FROM build").fetchone()
        if r is None:
            return None
        return IndexBuild(
            inputs_hash=str(r["inputs_hash"]),
            first_date=to_date(r["first_date"]),
            last_date=to_date(r["last_date"]),
            built_at=str(r["built_at"]),
            built_by=str(r["built_by"]),
        )

    def levels(self, start: date | None = None, end: date | None = None) -> list[LevelRow]:
        return [
            LevelRow(
                level_date=to_date(r["level_date"]),
                price_level=to_decimal(r["price_level"]),
                total_return_level=to_decimal(r["total_return_level"]),
                dividend_points=to_decimal(r["dividend_points"]),
                divisor=to_decimal(r["divisor"]),
                is_rebalance=bool(r["is_rebalance"]),
                is_reconstitution=bool(r["is_reconstitution"]),
                member_count=int(r["member_count"]),
            )
            for r in self.con.execute(
                "SELECT * FROM level WHERE level_date >= ? AND level_date <= ? "
                "ORDER BY level_date",
                ((start or date.min).isoformat(), (end or date.max).isoformat()),
            )
        ]

    def holdings_on(self, on: date) -> list[HoldingSpan]:
        return [
            HoldingSpan(
                security_id=int(r["security_id"]),
                from_date=to_date(r["from_date"]),
                to_date=to_date(r["to_date"]),
                units=to_decimal(r["units"]),
            )
            for r in self.con.execute(
                "SELECT * FROM holding WHERE from_date <= ? AND to_date >= ? "
                "ORDER BY security_id",
                (on.isoformat(), on.isoformat()),
            )
        ]

    def all_holdings(self) -> list[HoldingSpan]:
        return [
            HoldingSpan(
                security_id=int(r["security_id"]),
                from_date=to_date(r["from_date"]),
                to_date=to_date(r["to_date"]),
                units=to_decimal(r["units"]),
            )
            for r in self.con.execute("SELECT * FROM holding ORDER BY security_id, from_date")
        ]

    def rebalance_weights(self, on: date | None = None) -> list[RebalanceWeight]:
        where, args = ("WHERE rebalance_date = ?", (on.isoformat(),)) if on else ("", ())
        return [
            RebalanceWeight(
                rebalance_date=to_date(r["rebalance_date"]),
                security_id=int(r["security_id"]),
                target_weight=to_decimal(r["target_weight"]),
                close=to_decimal(r["close"]),
                units=to_decimal(r["units"]),
                weighting_cap=_d(r["weighting_cap"]),
            )
            for r in self.con.execute(
                f"SELECT * FROM rebalance_weight {where} "  # noqa: S608
                "ORDER BY rebalance_date, security_id",
                args,
            )
        ]

    def reinvestments(self) -> list[Reinvestment]:
        return [
            Reinvestment(
                dividend_id=int(r["dividend_id"]),
                level_date=to_date(r["level_date"]),
                security_id=int(r["security_id"]),
                units=to_decimal(r["units"]),
                amount=to_decimal(r["amount"]),
                index_points=to_decimal(r["index_points"]),
            )
            for r in self.con.execute(
                "SELECT * FROM reinvestment ORDER BY level_date, security_id, dividend_id"
            )
        ]

    def built_digest(self) -> str:
        """SHA-256 over every built row but the build's own stamp: what a rebuild reproduces."""
        digest = hashlib.sha256()
        for table in reversed(self.DERIVED):
            columns = "inputs_hash, first_date, last_date" if table == "build" else "*"
            for row in self.con.execute(f'SELECT {columns} FROM "{table}" ORDER BY 1, 2, 3'):  # noqa: S608
                digest.update(("\x1f".join(str(v) for v in row) + "\n").encode())
        return digest.hexdigest()
