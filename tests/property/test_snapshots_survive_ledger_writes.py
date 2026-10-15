"""No ledger write leaves a stale valuation snapshot behind. ADR 0024.

Generated sequences of valuations and ledger writes -- deposits, withdrawals,
purchases and transfers dated anywhere in the range, and reversals of any of
them -- with every write going through the repository's append, as `pt` does.
After every step, every stored snapshot must:

* pass :func:`snapshots.verify`, the check `pt validate` runs; and
* agree with a fresh valuation of its own date on everything that does not
  depend on the day before.

A write dated before or on a stored snapshot changes it, so the writes that
land there are exactly the ones the append-time removal exists for. Without it
this fails within a few examples.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from portable_core.domain.enums import AccountType, InstrumentType, TransactionType
from portable_core.domain.models import Account, Instrument, Price
from portable_core.persistence.repositories import Repositories
from portable_core.schema import migrations as M
from portable_core.services import snapshots
from portable_core.services.replay import ReplayEngine
from tests.conftest import append

pytestmark = [pytest.mark.property, pytest.mark.unit]

D = Decimal
START = date(2024, 1, 2)
DAYS = 20

#: Compared with a fresh valuation. The rest chain from the day before.
_SAME_DAY = ("ending_market_value", "securities_value", "cash_balance", "external_flow_account")

#: ("value", first, span) | ("deposit"/"withdrawal"/"transfer", account, day, cents)
#: | ("buy", account, day, quantity) | ("reverse", pick, days_later)
Step = tuple[str, int, int, int]

steps = st.lists(
    st.one_of(
        st.tuples(st.just("value"), st.integers(0, DAYS - 1), st.integers(0, 4), st.just(0)),
        st.tuples(
            st.sampled_from(("deposit", "withdrawal", "transfer")),
            st.integers(0, 1),
            st.integers(0, DAYS - 1),
            st.integers(1, 100_000),
        ),
        st.tuples(
            st.just("buy"), st.integers(0, 1), st.integers(0, DAYS - 1), st.integers(1, 9)
        ),
        st.tuples(st.just("reverse"), st.integers(0, 50), st.integers(0, 5), st.just(0)),
    ),
    min_size=2,
    max_size=12,
)


@contextmanager
def _portfolio() -> Iterator[tuple[Repositories, list[Account], int]]:
    con = sqlite3.connect(":memory:", isolation_level=None)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        M.initialise(con)
        repos = Repositories(con)
        accounts: list[Account] = []
        for name in ("One", "Two"):
            account_id = repos.accounts.add(
                Account(
                    account_id=0,
                    name=name,
                    account_type=AccountType.TAX_EXEMPT,
                    opened_date=START,
                )
            )
            found = repos.accounts.get(account_id)
            assert found is not None
            accounts.append(found)
        instrument_id = repos.instruments.add(
            Instrument(instrument_id=0, symbol="GEN", instrument_type=InstrumentType.EQUITY)
        )
        for offset in range(DAYS + 5):
            repos.prices.add(
                Price(
                    instrument_id=instrument_id,
                    price_date=START + timedelta(days=offset),
                    price=D(100 + offset),
                    source="test",
                    as_of=datetime(2024, 1, 1, tzinfo=UTC),
                )
            )
        yield repos, accounts, instrument_id
    finally:
        con.close()


def _write(
    repos: Repositories,
    accounts: list[Account],
    instrument_id: int,
    step: Step,
    entries: list[int],
) -> None:
    kind, a, b, c = step
    engine = ReplayEngine(repos)
    if kind == "reverse":
        live = [txn_id for txn_id in entries if txn_id not in _reversed(repos)]
        if not live:
            return
        original = repos.transactions.get(live[a % len(live)])
        assert original is not None
        append(repos, original.account_id, TransactionType.REVERSAL,
               original.trade_date + timedelta(days=b), reverses_txn_id=original.txn_id,
               net_cash_effect=-original.net_cash_effect)  # fmt: skip
        engine.rebuild()  # as `pt trade reverse` does
        return

    account, on = accounts[a], START + timedelta(days=b)
    if kind == "buy":
        gross = D(c) * D(100 + b)
        txn_id = append(repos, account.account_id, TransactionType.BUY, on,
                        instrument_id=instrument_id, quantity=D(c), price=D(100 + b),
                        gross_amount=gross, net_cash_effect=-gross)  # fmt: skip
    else:
        amount = D(c) / 100
        txn_id = append(
            repos,
            account.account_id,
            TransactionType(kind),
            on,
            net_cash_effect=amount if kind == "deposit" else -amount,
            counter_account_id=accounts[1 - a].account_id if kind == "transfer" else None,
        )
    entries.append(txn_id)
    written = repos.transactions.get(txn_id)
    assert written is not None
    engine.apply_or_rebuild(written)


def _reversed(repos: Repositories) -> set[int]:
    return {
        txn.reverses_txn_id
        for txn in repos.transactions.in_ledger_order()
        if txn.reverses_txn_id is not None
    }


@settings(max_examples=40, deadline=None)
@given(plan=steps)
def test_every_stored_snapshot_still_holds_after_every_write(plan: list[Step]) -> None:
    with _portfolio() as (repos, accounts, instrument_id):
        entries: list[int] = []
        for step in plan:
            if step[0] == "value":
                first = START + timedelta(days=step[1])
                built, _ = snapshots.value_range(
                    repos, accounts, first, first + timedelta(days=step[2]),
                    staleness_tolerance_days=5,
                )  # fmt: skip
                for snapshot in built:
                    repos.valuations.save_snapshot(snapshot)
            else:
                _write(repos, accounts, instrument_id, step, entries)

            assert snapshots.verify(repos) == [], step

            by_id = {account.account_id: account for account in accounts}
            for snapshot_id, account_id, on in repos.valuations.stored_snapshots():
                kept = repos.valuations.record(snapshot_id)
                [fresh], _ = snapshots.value_range(
                    repos, [by_id[account_id]], on, on, staleness_tolerance_days=5
                )
                again = repos.valuations.record_of(fresh)
                assert {f: kept[f] for f in _SAME_DAY} == {f: again[f] for f in _SAME_DAY}, (
                    step,
                    on,
                )
