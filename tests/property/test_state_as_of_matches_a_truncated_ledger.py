"""The walk agrees with an independent oracle at every date, over generated ledgers.

ADR 0021: state as of D is the corrected ledger -- every reversal and the entry
it reverses netted out over the *whole* ledger -- replayed through D. The
oracle builds that literally: a fresh file holding only the surviving trades
dated on or before D, rebuilt from nothing. The walk does it by rewinding the
real file in a savepoint. Generated reversals land after the trades they
reverse, which is exactly where "as booked through D" and "corrected through D"
part company.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from portable_core.domain.enums import (
    AccountType,
    InstrumentType,
    ReliefMethod,
    TransactionType,
)
from portable_core.domain.models import Account, Instrument
from portable_core.persistence.repositories import Repositories
from portable_core.schema import migrations as M
from portable_core.services.replay import ReplayEngine, derived_state_digest
from tests.conftest import append

pytestmark = [pytest.mark.property, pytest.mark.unit]

D = Decimal
START = date(2024, 1, 2)

#: (day offset, quantity, price in cents, is a buy, reversed this many days later)
Trade = tuple[int, int, int, bool, int | None]

Summary = tuple[
    Decimal,
    tuple[tuple[str, Decimal], ...],
    tuple[tuple[date, Decimal, Decimal], ...],
    tuple[tuple[date, Decimal, Decimal, Decimal], ...],
]


@contextmanager
def _portfolio() -> Iterator[tuple[Repositories, int, int]]:
    """A migrated in-memory file with one FIFO account and one instrument.

    Built per example rather than taken from a fixture -- see the note on
    ``_fresh_repos`` in test_replay_reproduces_state.py -- and closed after,
    because an unclosed connection is a ResourceWarning on Python 3.13.
    """
    con = sqlite3.connect(":memory:", isolation_level=None)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        M.initialise(con)
        repos = Repositories(con)
        account_id = repos.accounts.add(
            Account(
                account_id=0,
                name="Generated",
                account_type=AccountType.TAX_EXEMPT,  # no rate schedule needed
                opened_date=START,
                default_relief_method=ReliefMethod.FIFO,
            )
        )
        instrument_id = repos.instruments.add(
            Instrument(instrument_id=0, symbol="GEN", instrument_type=InstrumentType.EQUITY)
        )
        yield repos, account_id, instrument_id
    finally:
        con.close()


def _record(repos: Repositories, account_id: int, instrument_id: int, trade: Trade) -> int:
    offset, quantity, price_cents, is_buy, _ = trade
    qty, price = D(quantity), D(price_cents) / D(100)
    gross = qty * price
    return append(
        repos,
        account_id,
        TransactionType.BUY if is_buy else TransactionType.SELL,
        START + timedelta(days=offset),
        instrument_id=instrument_id,
        quantity=qty,
        price=price,
        gross_amount=gross,
        net_cash_effect=-gross if is_buy else gross,
    )


def _summary(repos: Repositories, account_id: int) -> Summary:
    """Everything a report reads, without surrogate keys."""
    legs: list[tuple[str, Decimal]] = []
    lots: list[tuple[date, Decimal, Decimal]] = []
    for position in repos.positions.all(account_id=account_id, open_only=True):
        for leg in position.legs:
            if leg.quantity == 0:
                continue
            legs.append(("GEN", leg.quantity))
            lots.extend(
                (lot.open_date, lot.remaining_quantity, lot.adjusted_cost_basis)
                for lot in repos.lots.by_leg(leg.leg_id)
            )
    gains = sorted(
        (g.disposition_date, g.proceeds, g.cost_basis, g.gain)
        for g in repos.lots.realized_gains(account_id=account_id)
    )
    return (
        repos.valuations.cash(account_id)[0],
        tuple(sorted(legs)),
        tuple(sorted(lots)),
        tuple(gains),
    )


@settings(max_examples=40, deadline=None)
@given(
    trades=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=300),
            st.integers(min_value=1, max_value=500),
            st.integers(min_value=1000, max_value=50000),
            st.booleans(),
            st.one_of(st.none(), st.integers(min_value=0, max_value=60)),
        ),
        min_size=1,
        max_size=20,
    ),
    probes=st.lists(st.integers(min_value=0, max_value=380), min_size=1, max_size=5),
)
def test_the_walk_matches_a_fresh_replay_of_the_surviving_rows(
    trades: list[Trade], probes: list[int]
) -> None:
    """At every probed date, walk == rebuild of a file holding only what survives.

    Sells that would oversell the *surviving* position are dropped from the
    generated ledger, so every example replays without warnings. A reversed
    trade is recorded whatever it does -- it never reaches derived state.
    """
    # Keyed by position, not by value or identity: two generated trades can be
    # equal, and each must still be recorded -- and reversed -- separately.
    kept: list[int] = []
    reversed_after: dict[int, int] = {}
    held = D(0)
    for index in sorted(range(len(trades)), key=lambda i: trades[i][0]):
        _, quantity, _, is_buy, after = trades[index]
        if after is not None:
            kept.append(index)
            reversed_after[index] = after
        elif is_buy:
            kept.append(index)
            held += quantity
        elif held >= quantity:
            kept.append(index)
            held -= quantity
    survivors = [trades[i] for i in kept if i not in reversed_after]
    assume(survivors)

    days = sorted({START + timedelta(days=p) for p in probes})

    with _portfolio() as (repos, account_id, instrument_id):
        recorded = {i: _record(repos, account_id, instrument_id, trades[i]) for i in kept}
        for index, after in reversed_after.items():
            offset, quantity, price_cents, is_buy, _ = trades[index]
            gross = D(quantity) * D(price_cents) / D(100)
            append(
                repos,
                account_id,
                TransactionType.REVERSAL,
                START + timedelta(days=offset + after),
                instrument_id=instrument_id,
                quantity=D(quantity),
                net_cash_effect=gross if is_buy else -gross,
                reverses_txn_id=recorded[index],
            )

        engine = ReplayEngine(repos)
        assert engine.rebuild().warnings == ()
        stored = derived_state_digest(repos)

        walked = engine.walk(days, lambda _on: _summary(repos, account_id))

        assert walked.warnings == ()
        assert derived_state_digest(repos) == stored, "the walk must roll its rewind back"

    for on, seen in zip(days, walked.results, strict=True):
        with _portfolio() as (oracle, oracle_account, oracle_instrument):
            for trade in survivors:
                if START + timedelta(days=trade[0]) <= on:
                    _record(oracle, oracle_account, oracle_instrument, trade)
            ReplayEngine(oracle).rebuild()
            assert seen == _summary(oracle, oracle_account), f"state as of {on}"
