"""Repositories: the only place SQL lives (ADR 0002).

Each repository takes a connection, speaks SQL to one part of the schema, and
returns domain objects. There is no ORM, no lazy loading, and no query builder
-- what runs is what is written here.

Two disciplines apply throughout:

* **Decimal conversion is explicit** on the way in and out (ADR 0005). The
  write-side adapter turns a ``Decimal`` into canonical text; the read side
  goes through :mod:`portable_core.persistence.mappers`.
* **Money is never compared or ordered in SQL.** The canonical text form
  preserves trailing zeros, so ``'10.500' < '10.6'`` sorts as text and not as
  money. Ordering by money happens in the service layer, in ``Decimal``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, ClassVar, cast

from portable_core.decimals import to_text
from portable_core.domain.models import (
    Account,
    BasisAdjustment,
    Benchmark,
    Instrument,
    Lot,
    LotDisposition,
    Position,
    PositionLeg,
    Price,
    RealizedGain,
    ReturnPolicy,
    TaxRateSchedule,
    Transaction,
    ValuationSnapshot,
)
from portable_core.errors import ValidationError
from portable_core.errors.kinds import (
    E_ACCOUNT_NOT_FOUND,
    E_INSTRUMENT_AMBIGUOUS,
    E_INSTRUMENT_NOT_FOUND,
)
from portable_core.persistence import mappers

__all__ = [
    "AccountRepository",
    "BenchmarkRepository",
    "InstrumentRepository",
    "LotRepository",
    "MetaRepository",
    "PolicyRepository",
    "PositionRepository",
    "PriceRepository",
    "Repositories",
    "TransactionRepository",
    "ValuationRepository",
]


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value: Decimal | None) -> str | None:
    return None if value is None else to_text(value)


class _Repository:
    """Common base: holds the connection, nothing more."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con


# ── meta ─────────────────────────────────────────────────────────────────────


class MetaRepository(_Repository):
    """Portfolio identity, as key/value."""

    def get(self, key: str) -> str | None:
        row = self.con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def require(self, key: str) -> str:
        value = self.get(key)
        if value is None:
            raise ValidationError(
                f"portfolio metadata is missing required key {key!r}",
                code="PT-E-PORTFOLIO-CORRUPT",
                remedy="Run `pt validate` for the full list of what is missing.",
                key=key,
            )
        return value

    def set(self, key: str, value: str) -> None:
        self.con.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def all(self) -> dict[str, str]:
        return {
            str(r["key"]): str(r["value"])
            for r in self.con.execute("SELECT key, value FROM meta ORDER BY key")
        }

    def record_change(
        self, key: str, old: str | None, new: str, effective_from: date, reason: str
    ) -> None:
        """Log a redefinition or name change (PORT-GIPS-I17)."""
        self.con.execute(
            "INSERT INTO meta_change_log (key, old_value, new_value, effective_from, "
            "reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (key, old, new, effective_from.isoformat(), reason, _now()),
        )


# ── accounts ─────────────────────────────────────────────────────────────────


class AccountRepository(_Repository):
    _COLUMNS = (
        "account_id, name, account_type, custodian, account_alias, opened_date, "
        "closed_date, status, cash_treatment, default_relief_method, "
        "allows_fractional, sweep_instrument_id, currency, note"
    )

    def add(self, account: Account) -> int:
        cursor = self.con.execute(
            "INSERT INTO account (name, account_type, custodian, account_alias, "
            "opened_date, closed_date, status, cash_treatment, default_relief_method, "
            "allows_fractional, sweep_instrument_id, currency, note, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                account.name,
                str(account.account_type),
                account.custodian,
                account.account_alias,
                account.opened_date.isoformat(),
                account.closed_date.isoformat() if account.closed_date else None,
                str(account.status),
                str(account.cash_treatment),
                str(account.default_relief_method),
                int(account.allows_fractional),
                account.sweep_instrument_id,
                account.currency,
                account.note,
                _now(),
                _now(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def get(self, account_id: int) -> Account | None:
        row = self.con.execute(
            f"SELECT {self._COLUMNS} FROM account WHERE account_id = ?",  # noqa: S608 -- column list is a class constant
            (account_id,),
        ).fetchone()
        return None if row is None else mappers.to_account(row)

    def by_name(self, name: str) -> Account | None:
        row = self.con.execute(
            f"SELECT {self._COLUMNS} FROM account WHERE name = ?",  # noqa: S608 -- column list is a class constant
            (name,),
        ).fetchone()
        return None if row is None else mappers.to_account(row)

    def resolve(self, reference: str) -> Account:
        """Resolve an account by name, or by id when the reference is numeric.

        Raises:
            ValidationError: naming the accounts that do exist. An error that
                says "not found" without saying what *is* found makes the user
                run a second command to get anywhere.
        """
        account = self.by_name(reference)
        if account is None and reference.isdigit():
            account = self.get(int(reference))
        if account is None:
            raise ValidationError(
                f"no account named {reference!r}",
                code=E_ACCOUNT_NOT_FOUND,
                remedy="`pt account list` shows the accounts in this portfolio.",
                reference=reference,
                known_accounts=[a.name for a in self.all()],
            )
        return account

    def all(self, *, include_closed: bool = True) -> list[Account]:
        sql = f"SELECT {self._COLUMNS} FROM account"  # noqa: S608 -- interpolates fixed literals only; all values are bound
        if not include_closed:
            sql += " WHERE status = 'open'"
        sql += " ORDER BY name"
        return [mappers.to_account(r) for r in self.con.execute(sql)]

    def close(self, account_id: int, on: date) -> None:
        self.con.execute(
            "UPDATE account SET status = 'closed', closed_date = ?, updated_at = ? "
            "WHERE account_id = ?",
            (on.isoformat(), _now(), account_id),
        )

    #: Columns `update` will write. Interpolated into SQL, so the set is
    #: fixed here rather than taken from the caller -- a column name cannot be
    #: bound as a parameter, so an allow-list is the only real guard.
    _UPDATABLE = frozenset(
        {
            "name",
            "custodian",
            "account_alias",
            "cash_treatment",
            "default_relief_method",
            "allows_fractional",
            "sweep_instrument_id",
            "note",
            "status",
            "closed_date",
        }
    )

    def update(self, account_id: int, **fields: Any) -> None:
        if not fields:
            return
        unknown = sorted(set(fields) - self._UPDATABLE)
        if unknown:
            raise ValidationError(
                f"cannot update account column(s): {', '.join(unknown)}",
                code="PT-E-USAGE",
                remedy=f"Updatable columns are: {', '.join(sorted(self._UPDATABLE))}.",
                unknown=unknown,
            )
        keys = sorted(fields)
        assignments = ", ".join(f"{key} = ?" for key in keys)
        self.con.execute(
            f"UPDATE account SET {assignments}, updated_at = ? "  # noqa: S608 -- keys are allow-listed above
            "WHERE account_id = ?",
            (*(fields[k] for k in keys), _now(), account_id),
        )

    # ── tax rates ────────────────────────────────────────────────────────────

    def add_rate_schedule(self, schedule: TaxRateSchedule) -> int:
        cursor = self.con.execute(
            "INSERT INTO tax_rate_schedule (account_id, effective_from, "
            "short_term_federal, long_term_federal, state, niit, qualified_dividend, "
            "note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                schedule.account_id,
                schedule.effective_from.isoformat(),
                to_text(schedule.short_term_federal),
                to_text(schedule.long_term_federal),
                to_text(schedule.state),
                to_text(schedule.niit),
                _text(schedule.qualified_dividend),
                schedule.note,
                _now(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def rate_schedules(self, account_id: int) -> list[TaxRateSchedule]:
        return [
            mappers.to_tax_rate_schedule(r)
            for r in self.con.execute(
                "SELECT * FROM tax_rate_schedule WHERE account_id = ? ORDER BY effective_from",
                (account_id,),
            )
        ]


# ── instruments ──────────────────────────────────────────────────────────────


class InstrumentRepository(_Repository):
    def add(self, instrument: Instrument) -> int:
        cursor = self.con.execute(
            "INSERT INTO instrument (symbol, instrument_type, name, currency, exchange, "
            "cusip, isin, figi, sector, industry, asset_class, country, is_active, "
            "source, provider_ref, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                instrument.symbol,
                str(instrument.instrument_type),
                instrument.name,
                instrument.currency,
                instrument.exchange,
                instrument.cusip,
                instrument.isin,
                instrument.figi,
                instrument.sector,
                instrument.industry,
                instrument.asset_class,
                instrument.country,
                int(instrument.is_active),
                instrument.source,
                instrument.provider_ref,
                _now(),
                _now(),
            ),
        )
        instrument_id = int(cursor.lastrowid or 0)

        if instrument.option is not None:
            option = instrument.option
            self.con.execute(
                "INSERT INTO instrument_option (instrument_id, underlier_instrument_id, "
                "option_right, strike, expiry, multiplier, occ_symbol, exercise_style, "
                "settlement) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    instrument_id,
                    option.underlier_instrument_id,
                    str(option.option_right),
                    to_text(option.strike),
                    option.expiry.isoformat(),
                    to_text(option.multiplier),
                    option.occ_symbol,
                    str(option.exercise_style),
                    option.settlement,
                ),
            )
        if instrument.bond is not None:
            bond = instrument.bond
            self.con.execute(
                "INSERT INTO instrument_bond (instrument_id, issuer, coupon_rate, "
                "coupon_frequency, maturity_date, day_count, face_value, "
                "first_coupon_date, quote_basis, is_callable, next_call_date, "
                "next_call_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    instrument_id,
                    bond.issuer,
                    to_text(bond.coupon_rate),
                    bond.coupon_frequency,
                    bond.maturity_date.isoformat(),
                    str(bond.day_count),
                    to_text(bond.face_value),
                    bond.first_coupon_date.isoformat() if bond.first_coupon_date else None,
                    bond.quote_basis,
                    int(bond.is_callable),
                    bond.next_call_date.isoformat() if bond.next_call_date else None,
                    _text(bond.next_call_price),
                ),
            )

        self.con.execute(
            "INSERT INTO instrument_symbol_history (instrument_id, symbol, valid_from, "
            "is_primary, source) VALUES (?, ?, ?, 1, ?)",
            (instrument_id, instrument.symbol, "1900-01-01", instrument.source),
        )
        return instrument_id

    def get(self, instrument_id: int) -> Instrument | None:
        row = self.con.execute(
            "SELECT * FROM instrument WHERE instrument_id = ?", (instrument_id,)
        ).fetchone()
        if row is None:
            return None
        return self._hydrate(row)

    def _hydrate(self, row: sqlite3.Row) -> Instrument:
        instrument_id = int(row["instrument_id"])
        option_row = self.con.execute(
            "SELECT * FROM instrument_option WHERE instrument_id = ?", (instrument_id,)
        ).fetchone()
        bond_row = self.con.execute(
            "SELECT * FROM instrument_bond WHERE instrument_id = ?", (instrument_id,)
        ).fetchone()
        return mappers.to_instrument(row, option_row, bond_row)

    def resolve(self, symbol: str, *, on: date | None = None) -> Instrument:
        """Resolve a symbol to an instrument, as of a date.

        Resolution order, documented in `docs/domain-model.md`: internal id,
        then CUSIP/ISIN/FIGI, then the symbol **as it stood on the relevant
        date**. Resolving by today's symbol would silently rewrite history
        whenever a ticker has been reassigned.
        """
        candidates = self.con.execute(
            "SELECT * FROM instrument WHERE symbol = ? ORDER BY instrument_id",
            (symbol,),
        ).fetchall()

        if not candidates and on is not None:
            historical = self.con.execute(
                "SELECT i.* FROM instrument i "
                "JOIN instrument_symbol_history h USING (instrument_id) "
                "WHERE h.symbol = ? AND h.valid_from <= ? "
                "AND (h.valid_to IS NULL OR h.valid_to >= ?) "
                "ORDER BY i.instrument_id",
                (symbol, on.isoformat(), on.isoformat()),
            ).fetchall()
            candidates = historical

        if not candidates:
            for column in ("cusip", "isin", "figi"):
                found = self.con.execute(
                    f"SELECT * FROM instrument WHERE {column} = ?",  # noqa: S608 -- interpolates fixed literals only; all values are bound
                    (symbol,),
                ).fetchall()
                if found:
                    candidates = found
                    break

        if not candidates:
            raise ValidationError(
                f"no instrument known as {symbol!r}" + (f" on {on.isoformat()}" if on else ""),
                code=E_INSTRUMENT_NOT_FOUND,
                remedy=(
                    f"Add it with `pt instrument add {symbol} --type equity`, or "
                    "hydrate it from a provider with `pt instrument sync`."
                ),
                symbol=symbol,
            )
        if len(candidates) > 1:
            raise ValidationError(
                f"{symbol!r} matches {len(candidates)} instruments",
                code=E_INSTRUMENT_AMBIGUOUS,
                remedy="Disambiguate by instrument id, CUSIP, or ISIN.",
                symbol=symbol,
                matches=[int(c["instrument_id"]) for c in candidates],
            )
        return self._hydrate(candidates[0])

    def all(self, *, active_only: bool = False) -> list[Instrument]:
        sql = "SELECT * FROM instrument"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY symbol"
        return [self._hydrate(r) for r in self.con.execute(sql).fetchall()]

    def rename(self, instrument_id: int, new_symbol: str, effective: date) -> None:
        """Record a symbol change without losing the old one.

        The history row is what lets a trade dated before the change still
        resolve to the right instrument.
        """
        self.con.execute(
            "UPDATE instrument_symbol_history SET valid_to = ? "
            "WHERE instrument_id = ? AND valid_to IS NULL",
            (effective.isoformat(), instrument_id),
        )
        self.con.execute(
            "INSERT INTO instrument_symbol_history (instrument_id, symbol, valid_from, "
            "is_primary, source) VALUES (?, ?, ?, 1, 'manual')",
            (instrument_id, new_symbol, effective.isoformat()),
        )
        self.con.execute(
            "UPDATE instrument SET symbol = ?, updated_at = ? WHERE instrument_id = ?",
            (new_symbol, _now(), instrument_id),
        )


# ── prices ───────────────────────────────────────────────────────────────────


class PriceRepository(_Repository):
    def add(self, price: Price) -> int:
        cursor = self.con.execute(
            "INSERT INTO price (instrument_id, price_date, price, currency, source, "
            "as_of, valuation_level, valuation_basis, is_estimate, provider_ref, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(instrument_id, price_date, source) DO UPDATE SET "
            "price = excluded.price, as_of = excluded.as_of, "
            "valuation_level = excluded.valuation_level, "
            "valuation_basis = excluded.valuation_basis, "
            "is_estimate = excluded.is_estimate, provider_ref = excluded.provider_ref",
            (
                price.instrument_id,
                price.price_date.isoformat(),
                to_text(price.price),
                price.currency,
                price.source,
                price.as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
                price.valuation_level,
                str(price.valuation_basis),
                int(price.is_estimate),
                price.provider_ref,
                _now(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def newest_on_or_before(self, instrument_id: int, on: date) -> Price | None:
        """The most recent price at or before *on*.

        Ordered by ``price_date`` then ``price_id`` -- the second is what makes
        the result deterministic when two sources supplied the same date, and
        the `price_id` order means the most recently written wins, which is the
        provider that ran last.
        """
        row = self.con.execute(
            "SELECT * FROM price WHERE instrument_id = ? AND price_date <= ? "
            "ORDER BY price_date DESC, price_id DESC LIMIT 1",
            (instrument_id, on.isoformat()),
        ).fetchone()
        return None if row is None else mappers.to_price(row)

    def on_date(self, instrument_id: int, on: date) -> Price | None:
        row = self.con.execute(
            "SELECT * FROM price WHERE instrument_id = ? AND price_date = ? "
            "ORDER BY price_id DESC LIMIT 1",
            (instrument_id, on.isoformat()),
        ).fetchone()
        return None if row is None else mappers.to_price(row)

    def series(self, instrument_id: int, start: date, end: date) -> list[Price]:
        return [
            mappers.to_price(r)
            for r in self.con.execute(
                "SELECT * FROM price WHERE instrument_id = ? AND price_date BETWEEN ? AND ? "
                "ORDER BY price_date, price_id",
                (instrument_id, start.isoformat(), end.isoformat()),
            )
        ]


# ── the ledger ───────────────────────────────────────────────────────────────


class TransactionRepository(_Repository):
    """The ledger. Append-only, enforced by trigger as well as by this class."""

    def next_seq(self, trade_date: date) -> int:
        """The next sequence number for *trade_date*.

        Assigned from the ledger's existing maximum for that date, so a
        back-dated entry lands after same-day entries already recorded. That is
        the honest ordering: the ledger records when we learned things, and a
        later-entered trade did happen later as far as this book knows.
        """
        row = self.con.execute(
            'SELECT COALESCE(MAX(seq), 0) AS m FROM "transaction" WHERE trade_date = ?',
            (trade_date.isoformat(),),
        ).fetchone()
        return int(row["m"]) + 1

    def append(self, txn: Transaction) -> int:
        """Append one ledger row. There is no update and no delete."""
        cursor = self.con.execute(
            'INSERT INTO "transaction" (account_id, trade_date, settlement_date, seq, '
            "txn_type, instrument_id, quantity, price, gross_amount, fees, commissions, "
            "taxes_withheld, withholding_reclaimable, fee_class, net_cash_effect, "
            "position_id, counter_account_id, related_txn_id, reverses_txn_id, "
            "lot_selection, relief_method, ex_date, pay_date, is_qualified, note, "
            "external_ref, source, created_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?)",
            (
                txn.account_id,
                txn.trade_date.isoformat(),
                txn.settlement_date.isoformat() if txn.settlement_date else None,
                txn.seq,
                str(txn.txn_type),
                txn.instrument_id,
                _text(txn.quantity),
                _text(txn.price),
                _text(txn.gross_amount),
                to_text(txn.fees),
                to_text(txn.commissions),
                to_text(txn.taxes_withheld),
                _text(txn.withholding_reclaimable),
                str(txn.fee_class) if txn.fee_class else None,
                to_text(txn.net_cash_effect),
                txn.position_id,
                txn.counter_account_id,
                txn.related_txn_id,
                txn.reverses_txn_id,
                txn.lot_selection,
                str(txn.relief_method) if txn.relief_method else None,
                txn.ex_date.isoformat() if txn.ex_date else None,
                txn.pay_date.isoformat() if txn.pay_date else None,
                None if txn.is_qualified is None else int(txn.is_qualified),
                txn.note,
                txn.external_ref,
                str(txn.source),
                txn.created_at or _now(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def get(self, txn_id: int) -> Transaction | None:
        row = self.con.execute(
            'SELECT * FROM "transaction" WHERE txn_id = ?', (txn_id,)
        ).fetchone()
        return None if row is None else mappers.to_transaction(row)

    def in_ledger_order(
        self,
        *,
        until: date | None = None,
        account_id: int | None = None,
    ) -> list[Transaction]:
        """Every transaction in replay order: ``(trade_date, seq, txn_id)``.

        This ordering is the replay contract (ADR 0010). Not ``created_at``,
        which is a wall clock and not a total order across machines.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if until is not None:
            clauses.append("trade_date <= ?")
            params.append(until.isoformat())
        if account_id is not None:
            clauses.append("account_id = ?")
            params.append(account_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return [
            mappers.to_transaction(r)
            for r in self.con.execute(
                f'SELECT * FROM "transaction"{where}'  # noqa: S608 -- interpolates fixed literals only; all values are bound
                " ORDER BY trade_date, seq, txn_id",
                params,
            )
        ]

    def by_external_ref(self, external_ref: str) -> list[Transaction]:
        """Used for duplicate detection on import."""
        return [
            mappers.to_transaction(r)
            for r in self.con.execute(
                'SELECT * FROM "transaction" WHERE external_ref = ? ORDER BY trade_date, seq',
                (external_ref,),
            )
        ]

    def count(self) -> int:
        row = self.con.execute('SELECT COUNT(*) AS n FROM "transaction"').fetchone()
        return int(row["n"])


# ── positions and lots ───────────────────────────────────────────────────────


class PositionRepository(_Repository):
    def add(self, position: Position) -> int:
        cursor = self.con.execute(
            "INSERT INTO position (account_id, strategy_type, opened_date, closed_date, "
            "status, label, note, opened_txn_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                position.account_id,
                str(position.strategy_type),
                position.opened_date.isoformat(),
                position.closed_date.isoformat() if position.closed_date else None,
                str(position.status),
                position.label,
                position.note,
                position.opened_txn_id,
            ),
        )
        return int(cursor.lastrowid or 0)

    def add_leg(self, leg: PositionLeg) -> int:
        cursor = self.con.execute(
            "INSERT INTO position_leg (position_id, instrument_id, role, sign, quantity, "
            "opened_date, closed_date, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                leg.position_id,
                leg.instrument_id,
                str(leg.role),
                leg.sign,
                to_text(leg.quantity),
                leg.opened_date.isoformat(),
                leg.closed_date.isoformat() if leg.closed_date else None,
                str(leg.status),
            ),
        )
        return int(cursor.lastrowid or 0)

    def get(self, position_id: int) -> Position | None:
        row = self.con.execute(
            "SELECT * FROM position WHERE position_id = ?", (position_id,)
        ).fetchone()
        if row is None:
            return None
        return mappers.to_position(row, tuple(self.legs(position_id)))

    def legs(self, position_id: int) -> list[PositionLeg]:
        return [
            mappers.to_position_leg(r)
            for r in self.con.execute(
                "SELECT * FROM position_leg WHERE position_id = ? ORDER BY leg_id",
                (position_id,),
            )
        ]

    def leg_for(
        self, account_id: int, instrument_id: int, *, open_only: bool = True
    ) -> PositionLeg | None:
        """The open leg for an instrument in an account, if there is exactly one.

        Used to apply the documented default when a trade names no position.
        """
        sql = (
            "SELECT l.* FROM position_leg l JOIN position p USING (position_id) "
            "WHERE p.account_id = ? AND l.instrument_id = ?"
        )
        if open_only:
            sql += " AND l.status = 'open'"
        sql += " ORDER BY l.leg_id"
        rows = self.con.execute(sql, (account_id, instrument_id)).fetchall()
        return mappers.to_position_leg(rows[0]) if len(rows) == 1 else None

    def all(self, *, account_id: int | None = None, open_only: bool = False) -> list[Position]:
        clauses: list[str] = []
        params: list[Any] = []
        if account_id is not None:
            clauses.append("account_id = ?")
            params.append(account_id)
        if open_only:
            clauses.append("status = 'open'")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.con.execute(
            f"SELECT * FROM position{where} ORDER BY position_id",  # noqa: S608 -- interpolates fixed literals only; all values are bound
            params,
        ).fetchall()
        return [mappers.to_position(r, tuple(self.legs(int(r["position_id"])))) for r in rows]

    def update_leg_quantity(self, leg_id: int, quantity: Decimal) -> None:
        self.con.execute(
            "UPDATE position_leg SET quantity = ? WHERE leg_id = ?",
            (to_text(quantity), leg_id),
        )

    def close_leg(self, leg_id: int, on: date) -> None:
        self.con.execute(
            "UPDATE position_leg SET status = 'closed', closed_date = ?, quantity = '0' "
            "WHERE leg_id = ?",
            (on.isoformat(), leg_id),
        )

    def close_position(self, position_id: int, on: date) -> None:
        self.con.execute(
            "UPDATE position SET status = 'closed', closed_date = ? WHERE position_id = ?",
            (on.isoformat(), position_id),
        )

    def position_id_for_leg(self, leg_id: int) -> int | None:
        """The position a leg belongs to.

        Exists so that `services/` never writes SQL: the replay engine needs
        this to decide whether closing a leg closes its position, and
        ADR 0002 keeps every query in this layer.
        """
        row = self.con.execute(
            "SELECT position_id FROM position_leg WHERE leg_id = ?", (leg_id,)
        ).fetchone()
        return None if row is None else int(row["position_id"])

    def move_leg(self, leg_id: int, position_id: int) -> None:
        """Regroup: one column, no lot touched, no basis touched (ADR 0009)."""
        self.con.execute(
            "UPDATE position_leg SET position_id = ? WHERE leg_id = ?",
            (position_id, leg_id),
        )


class LotRepository(_Repository):
    def add(self, lot: Lot) -> int:
        cursor = self.con.execute(
            "INSERT INTO lot (leg_id, position_id, instrument_id, account_id, open_date, "
            "open_txn_id, original_quantity, remaining_quantity, per_unit_price, "
            "allocated_fees, original_cost_basis, adjusted_cost_basis, "
            "holding_period_start, is_short, status, closed_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lot.leg_id,
                lot.position_id,
                lot.instrument_id,
                lot.account_id,
                lot.open_date.isoformat(),
                lot.open_txn_id,
                to_text(lot.original_quantity),
                to_text(lot.remaining_quantity),
                to_text(lot.per_unit_price),
                to_text(lot.allocated_fees),
                to_text(lot.original_cost_basis),
                to_text(lot.adjusted_cost_basis),
                lot.holding_period_start.isoformat(),
                int(lot.is_short),
                str(lot.status),
                lot.closed_date.isoformat() if lot.closed_date else None,
            ),
        )
        return int(cursor.lastrowid or 0)

    def get(self, lot_id: int) -> Lot | None:
        row = self.con.execute("SELECT * FROM lot WHERE lot_id = ?", (lot_id,)).fetchone()
        if row is None:
            return None
        return mappers.to_lot(row, tuple(self.adjustments(lot_id)))

    def open_lots(
        self, account_id: int, instrument_id: int, *, as_of: date | None = None
    ) -> list[Lot]:
        """Open lots for one instrument in one account, oldest first.

        Ordered by ``(open_date, lot_id)`` here so that the engine receives a
        stable sequence; the relief method reorders as it needs. Money is not
        ordered in SQL -- HIFO and LOFO sort in ``Decimal``, in the engine.
        """
        sql = (
            "SELECT * FROM lot WHERE account_id = ? AND instrument_id = ? "
            "AND status <> 'closed'"
        )
        params: list[Any] = [account_id, instrument_id]
        if as_of is not None:
            sql += " AND open_date <= ?"
            params.append(as_of.isoformat())
        sql += " ORDER BY open_date, lot_id"
        return [
            mappers.to_lot(r, tuple(self.adjustments(int(r["lot_id"]))))
            for r in self.con.execute(sql, params)
        ]

    def by_leg(self, leg_id: int, *, open_only: bool = True) -> list[Lot]:
        sql = "SELECT * FROM lot WHERE leg_id = ?"
        if open_only:
            sql += " AND status <> 'closed'"
        sql += " ORDER BY open_date, lot_id"
        return [mappers.to_lot(r) for r in self.con.execute(sql, (leg_id,))]

    def update_after_disposition(self, lot: Lot) -> None:
        self.con.execute(
            "UPDATE lot SET remaining_quantity = ?, adjusted_cost_basis = ?, "
            "status = ?, closed_date = ? WHERE lot_id = ?",
            (
                to_text(lot.remaining_quantity),
                to_text(lot.adjusted_cost_basis),
                str(lot.status),
                lot.closed_date.isoformat() if lot.closed_date else None,
                lot.lot_id,
            ),
        )

    def update_basis(self, lot: Lot) -> None:
        self.con.execute(
            "UPDATE lot SET original_quantity = ?, remaining_quantity = ?, "
            "per_unit_price = ?, adjusted_cost_basis = ?, holding_period_start = ? "
            "WHERE lot_id = ?",
            (
                to_text(lot.original_quantity),
                to_text(lot.remaining_quantity),
                to_text(lot.per_unit_price),
                to_text(lot.adjusted_cost_basis),
                lot.holding_period_start.isoformat(),
                lot.lot_id,
            ),
        )

    def add_adjustment(self, adjustment: BasisAdjustment) -> int:
        cursor = self.con.execute(
            "INSERT INTO lot_basis_adjustment (lot_id, adjustment_date, reason, "
            "basis_delta, quantity_delta, holding_period_start_after, txn_id, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                adjustment.lot_id,
                adjustment.adjustment_date.isoformat(),
                str(adjustment.reason),
                to_text(adjustment.basis_delta),
                to_text(adjustment.quantity_delta),
                (
                    adjustment.holding_period_start_after.isoformat()
                    if adjustment.holding_period_start_after
                    else None
                ),
                adjustment.txn_id,
                adjustment.note,
            ),
        )
        return int(cursor.lastrowid or 0)

    def adjustments(self, lot_id: int) -> list[BasisAdjustment]:
        return [
            mappers.to_basis_adjustment(r)
            for r in self.con.execute(
                "SELECT * FROM lot_basis_adjustment WHERE lot_id = ? "
                "ORDER BY adjustment_date, adjustment_id",
                (lot_id,),
            )
        ]

    # ── dispositions ─────────────────────────────────────────────────────────

    def add_disposition(self, disposition: LotDisposition) -> int:
        cursor = self.con.execute(
            "INSERT INTO lot_disposition (lot_id, txn_id, account_id, instrument_id, "
            "disposition_date, quantity, proceeds, allocated_fees, cost_basis_relieved, "
            "realized_gain, holding_period, days_held, relief_method, "
            "wash_sale_deferred) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                disposition.lot_id,
                disposition.txn_id,
                disposition.account_id,
                disposition.instrument_id,
                disposition.disposition_date.isoformat(),
                to_text(disposition.quantity),
                to_text(disposition.proceeds),
                to_text(disposition.allocated_fees),
                to_text(disposition.cost_basis_relieved),
                to_text(disposition.realized_gain),
                str(disposition.holding_period),
                disposition.days_held,
                str(disposition.relief_method),
                _text(disposition.wash_sale_deferred),
            ),
        )
        return int(cursor.lastrowid or 0)

    def dispositions(
        self,
        *,
        account_id: int | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> list[LotDisposition]:
        clauses: list[str] = []
        params: list[Any] = []
        if account_id is not None:
            clauses.append("account_id = ?")
            params.append(account_id)
        if start is not None:
            clauses.append("disposition_date >= ?")
            params.append(start.isoformat())
        if end is not None:
            clauses.append("disposition_date <= ?")
            params.append(end.isoformat())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return [
            mappers.to_disposition(r)
            for r in self.con.execute(
                f"SELECT * FROM lot_disposition{where}"  # noqa: S608 -- interpolates fixed literals only; all values are bound
                " ORDER BY disposition_date, disposition_id",
                params,
            )
        ]

    def methods_used_for(self, instrument_id: int) -> set[str]:
        """Relief methods already applied to an instrument.

        Feeds the average-cost/spec-ID conflict check.
        """
        return {
            str(r["relief_method"])
            for r in self.con.execute(
                "SELECT DISTINCT relief_method FROM lot_disposition WHERE instrument_id = ?",
                (instrument_id,),
            )
        }

    def add_realized_gain(self, gain: RealizedGain) -> int:
        cursor = self.con.execute(
            "INSERT INTO realized_gain (disposition_id, account_id, instrument_id, "
            "tax_year, disposition_date, holding_period, proceeds, cost_basis, gain, "
            "rate_id, federal_rate, state_rate, niit_rate, estimated_tax, is_taxable) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                gain.disposition_id,
                gain.account_id,
                gain.instrument_id,
                gain.tax_year,
                gain.disposition_date.isoformat(),
                str(gain.holding_period),
                to_text(gain.proceeds),
                to_text(gain.cost_basis),
                to_text(gain.gain),
                gain.rate_id,
                _text(gain.federal_rate),
                _text(gain.state_rate),
                _text(gain.niit_rate),
                _text(gain.estimated_tax),
                int(gain.is_taxable),
            ),
        )
        return int(cursor.lastrowid or 0)

    def realized_gains(
        self, *, tax_year: int | None = None, account_id: int | None = None
    ) -> list[RealizedGain]:
        clauses: list[str] = []
        params: list[Any] = []
        if tax_year is not None:
            clauses.append("tax_year = ?")
            params.append(tax_year)
        if account_id is not None:
            clauses.append("account_id = ?")
            params.append(account_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return [
            mappers.to_realized_gain(r)
            for r in self.con.execute(
                f"SELECT * FROM realized_gain{where}"  # noqa: S608 -- interpolates fixed literals only; all values are bound
                " ORDER BY disposition_date, realized_gain_id",
                params,
            )
        ]


# ── balances and valuations ──────────────────────────────────────────────────


class ValuationRepository(_Repository):
    def set_cash(
        self,
        account_id: int,
        balance: Decimal,
        *,
        currency: str = "USD",
        margin_loan: Decimal | None = None,
        last_txn_id: int | None = None,
    ) -> None:
        self.con.execute(
            "INSERT INTO cash_balance (account_id, currency, balance, margin_loan, "
            "last_txn_id) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, currency) DO UPDATE SET balance = excluded.balance, "
            "margin_loan = excluded.margin_loan, last_txn_id = excluded.last_txn_id",
            (
                account_id,
                currency,
                to_text(balance),
                to_text(margin_loan if margin_loan is not None else Decimal("0.00")),
                last_txn_id,
            ),
        )

    def cash(self, account_id: int, *, currency: str = "USD") -> tuple[Decimal, Decimal]:
        """Return ``(cash, margin_loan)``, defaulting to zero for a new account."""
        row = self.con.execute(
            "SELECT balance, margin_loan FROM cash_balance "
            "WHERE account_id = ? AND currency = ?",
            (account_id, currency),
        ).fetchone()
        if row is None:
            return Decimal("0.00"), Decimal("0.00")
        return mappers.to_decimal(row["balance"]), mappers.to_decimal(row["margin_loan"])

    def save_snapshot(self, snapshot: ValuationSnapshot) -> int:
        cursor = self.con.execute(
            "INSERT INTO valuation_snapshot (account_id, snapshot_date, "
            "beginning_market_value, ending_market_value, securities_value, "
            "cash_balance, margin_loan, accrued_interest, accrued_dividends, "
            "accrued_income, external_flow_account, external_flow_portfolio, "
            "income_amount, fees_amount, level5_market_value, is_complete, "
            "uses_estimates) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, snapshot_date) DO UPDATE SET "
            "beginning_market_value = excluded.beginning_market_value, "
            "ending_market_value = excluded.ending_market_value, "
            "securities_value = excluded.securities_value, "
            "cash_balance = excluded.cash_balance, margin_loan = excluded.margin_loan, "
            "accrued_interest = excluded.accrued_interest, "
            "accrued_dividends = excluded.accrued_dividends, "
            "accrued_income = excluded.accrued_income, "
            "external_flow_account = excluded.external_flow_account, "
            "external_flow_portfolio = excluded.external_flow_portfolio, "
            "income_amount = excluded.income_amount, "
            "fees_amount = excluded.fees_amount, "
            "level5_market_value = excluded.level5_market_value, "
            "is_complete = excluded.is_complete, "
            "uses_estimates = excluded.uses_estimates",
            (
                snapshot.account_id,
                snapshot.snapshot_date.isoformat(),
                to_text(snapshot.beginning_market_value),
                to_text(snapshot.ending_market_value),
                to_text(snapshot.securities_value),
                to_text(snapshot.cash_balance),
                to_text(snapshot.margin_loan),
                to_text(snapshot.accrued_interest),
                to_text(snapshot.accrued_dividends),
                to_text(snapshot.accrued_income),
                to_text(snapshot.external_flow_account),
                to_text(snapshot.external_flow_portfolio),
                to_text(snapshot.income_amount),
                to_text(snapshot.fees_amount),
                to_text(snapshot.level5_market_value),
                int(snapshot.is_complete),
                int(snapshot.uses_estimates),
            ),
        )
        snapshot_id = int(cursor.lastrowid or 0)
        if snapshot_id == 0:
            row = self.con.execute(
                "SELECT snapshot_id FROM valuation_snapshot "
                "WHERE account_id = ? AND snapshot_date = ?",
                (snapshot.account_id, snapshot.snapshot_date.isoformat()),
            ).fetchone()
            snapshot_id = int(row["snapshot_id"])

        # The price set is rewritten wholesale: a rebuilt snapshot consumed a
        # different set of prices, and a merge would leave the old ones behind
        # (PORT-GIPS-J03 wants the set that produced THIS figure).
        self.con.execute(
            "DELETE FROM valuation_snapshot_price WHERE snapshot_id = ?", (snapshot_id,)
        )
        for entry in snapshot.prices:
            self.con.execute(
                "INSERT INTO valuation_snapshot_price (snapshot_id, instrument_id, "
                "price_id, price, quantity, market_value, source, as_of, "
                "valuation_level, is_estimate, staleness_days) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    entry.instrument_id,
                    entry.price_id,
                    to_text(entry.price),
                    to_text(entry.quantity),
                    to_text(entry.market_value),
                    entry.source,
                    entry.as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    entry.valuation_level,
                    int(entry.is_estimate),
                    entry.staleness_days,
                ),
            )

        self.con.execute("DELETE FROM snapshot_flow WHERE snapshot_id = ?", (snapshot_id,))
        for flow in snapshot.flows:
            self.con.execute(
                "INSERT INTO snapshot_flow (snapshot_id, txn_id, account_id, flow_date, "
                "level, classification, amount, is_large, is_in_kind) "
                "VALUES (?, ?, ?, ?, 'account', 'external', ?, ?, ?)",
                (
                    snapshot_id,
                    flow.txn_id,
                    flow.account_id,
                    flow.flow_date.isoformat(),
                    to_text(flow.amount),
                    int(flow.is_large),
                    int(flow.is_in_kind),
                ),
            )
        return snapshot_id

    def snapshot(self, account_id: int, on: date) -> sqlite3.Row | None:
        row = self.con.execute(
            "SELECT * FROM valuation_snapshot WHERE account_id = ? AND snapshot_date = ?",
            (account_id, on.isoformat()),
        ).fetchone()
        return cast("sqlite3.Row | None", row)

    def latest_snapshot_date(self, account_id: int) -> date | None:
        row = self.con.execute(
            "SELECT MAX(snapshot_date) AS d FROM valuation_snapshot WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        return None if row["d"] is None else date.fromisoformat(str(row["d"]))


# ── policy and benchmarks ────────────────────────────────────────────────────


class PolicyRepository(_Repository):
    def add(self, policy: ReturnPolicy) -> int:
        cursor = self.con.execute(
            "INSERT INTO return_policy (effective_from, large_flow_basis, "
            "large_flow_value, significant_flow_basis, significant_flow_value, "
            "materiality_return_bps, materiality_value, risk_measure_basis, note, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                policy.effective_from.isoformat(),
                policy.large_flow_basis,
                to_text(policy.large_flow_value),
                policy.significant_flow_basis,
                _text(policy.significant_flow_value),
                _text(policy.materiality_return_bps),
                _text(policy.materiality_value),
                policy.risk_measure_basis,
                policy.note,
                _now(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def in_force(self, on: date) -> ReturnPolicy | None:
        """The policy effective on *on*, or None.

        Returning None rather than a default is the point: `pt` turns the None
        into ``PT-E-GIPS-NO-FLOW-POLICY`` at the boundary, because
        ``PORT-GIPS-B03`` makes a missing definition an error and not a zero.
        """
        row = self.con.execute(
            "SELECT * FROM return_policy WHERE effective_from <= ? "
            "ORDER BY effective_from DESC LIMIT 1",
            (on.isoformat(),),
        ).fetchone()
        return None if row is None else mappers.to_return_policy(row)

    def all(self) -> list[ReturnPolicy]:
        return [
            mappers.to_return_policy(r)
            for r in self.con.execute("SELECT * FROM return_policy ORDER BY effective_from")
        ]


class BenchmarkRepository(_Repository):
    def add(self, benchmark: Benchmark) -> int:
        cursor = self.con.execute(
            "INSERT INTO benchmark (name, description, return_type, periodicity, "
            "is_net_of_withholding, rebalance_rule, is_blend, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                benchmark.name,
                benchmark.description,
                str(benchmark.return_type),
                benchmark.periodicity,
                (
                    None
                    if benchmark.is_net_of_withholding is None
                    else int(benchmark.is_net_of_withholding)
                ),
                benchmark.rebalance_rule,
                int(benchmark.is_blend),
                benchmark.source,
                _now(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def by_name(self, name: str) -> Benchmark | None:
        row = self.con.execute("SELECT * FROM benchmark WHERE name = ?", (name,)).fetchone()
        return None if row is None else mappers.to_benchmark(row)

    def all(self) -> list[Benchmark]:
        return [
            mappers.to_benchmark(r)
            for r in self.con.execute("SELECT * FROM benchmark ORDER BY name")
        ]


# ── the bundle ───────────────────────────────────────────────────────────────


class Repositories:
    """Every repository over one connection.

    Services take this rather than a connection, which keeps them free of SQL
    and makes them testable with a fake bundle.
    """

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con
        self.meta = MetaRepository(con)
        self.accounts = AccountRepository(con)
        self.instruments = InstrumentRepository(con)
        self.prices = PriceRepository(con)
        self.transactions = TransactionRepository(con)
        self.positions = PositionRepository(con)
        self.lots = LotRepository(con)
        self.valuations = ValuationRepository(con)
        self.policies = PolicyRepository(con)
        self.benchmarks = BenchmarkRepository(con)

    #: Derived tables, with the column ordering that makes a digest of them
    #: stable, and the surrogate key to exclude from it. Surrogate ids are
    #: re-assigned by a rebuild -- the CONTENT must match, not the rowids
    #: (ADR 0010).
    DERIVED_DIGEST_TABLES: ClassVar[dict[str, tuple[str, str]]] = {
        "position": ("account_id, opened_date, strategy_type, status", "position_id"),
        "position_leg": ("instrument_id, role, opened_date", "leg_id"),
        "lot": ("account_id, instrument_id, open_date, original_quantity", "lot_id"),
        "lot_basis_adjustment": ("lot_id, adjustment_date, reason", "adjustment_id"),
        "lot_disposition": (
            "account_id, instrument_id, disposition_date, quantity",
            "disposition_id",
        ),
        "realized_gain": ("account_id, disposition_date, gain", "realized_gain_id"),
        "cash_balance": ("account_id, currency", ""),
        "valuation_snapshot": ("account_id, snapshot_date", "snapshot_id"),
    }

    def derived_rows(self, table: str) -> tuple[list[str], list[tuple[str | None, ...]]]:
        """Content columns and rows of one derived table, in digest order.

        Returns ``(columns, rows)`` with every value stringified, so the caller
        can hash them without knowing anything about SQLite. Surrogate id
        columns are excluded, for the reason on
        :data:`DERIVED_DIGEST_TABLES`.
        """
        if table not in self.DERIVED_DIGEST_TABLES:
            raise ValidationError(
                f"{table!r} is not a derived table",
                code="PT-E-USAGE",
                known=sorted(self.DERIVED_DIGEST_TABLES),
            )
        ordering, id_column = self.DERIVED_DIGEST_TABLES[table]
        columns = [
            str(row["name"])
            for row in self.con.execute(f'PRAGMA table_info("{table}")')
            if str(row["name"]) != id_column and not str(row["name"]).endswith("_id")
        ]
        if not columns:
            return [], []
        selected = ", ".join(f'"{c}"' for c in columns)
        rows = [
            tuple(None if row[c] is None else str(row[c]) for c in columns)
            for row in self.con.execute(
                f"SELECT {selected} FROM {table} ORDER BY {ordering}"  # noqa: S608 -- table and columns come from DERIVED_DIGEST_TABLES
            )
        ]
        return columns, rows

    def clear_derived(self) -> None:
        """Drop every derived table. The first half of `pt rebuild` (ADR 0010).

        The order matters only for foreign keys; the list itself is the
        authoritative statement of what "derived" means, and adding derived
        state without adding it here is the bug ADR 0010 exists to prevent.
        """
        for table in (
            "snapshot_flow",
            "valuation_snapshot_price",
            "valuation_snapshot",
            "realized_gain",
            "lot_disposition",
            "lot_basis_adjustment",
            "lot",
            "position_leg",
            "position",
            "cash_balance",
        ):
            self.con.execute(f"DELETE FROM {table}")  # noqa: S608 -- fixed literal list
