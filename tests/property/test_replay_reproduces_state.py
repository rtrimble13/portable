"""Ledger replay: determinism, idempotence, order-stability, and the invariants.

CLAUDE.md invariant 3 -- "replaying the ledger must reproduce materialized
state" -- and ADR 0010. This is the audit that makes every derived number in
the file worth trusting, so it is a property test rather than an example.

GIPS acceptance test: the ledger-replay determinism check named at
PORT-GIPS-J03 and PORT-GIPS-J06.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from portable_core.domain.enums import (
    AccountType,
    InstrumentType,
    ReliefMethod,
    TransactionType,
)
from portable_core.domain.models import Account, CorporateAction, Instrument
from portable_core.persistence.repositories import Repositories
from portable_core.schema import migrations as M
from portable_core.services.replay import ReplayEngine, derived_state_digest
from tests.conftest import append

pytestmark = [pytest.mark.property, pytest.mark.unit]

D = Decimal
START = date(2024, 1, 2)


def _buy(
    repos: Repositories, account_id: int, instrument_id: int, on: date, qty: str, price: str
) -> int:
    return append(
        repos,
        account_id,
        TransactionType.BUY,
        on,
        instrument_id=instrument_id,
        quantity=D(qty),
        price=D(price),
        gross_amount=D(qty) * D(price),
        net_cash_effect=-(D(qty) * D(price)),
    )


def _sell(
    repos: Repositories, account_id: int, instrument_id: int, on: date, qty: str, price: str
) -> int:
    return append(
        repos,
        account_id,
        TransactionType.SELL,
        on,
        instrument_id=instrument_id,
        quantity=D(qty),
        price=D(price),
        gross_amount=D(qty) * D(price),
        net_cash_effect=D(qty) * D(price),
    )


# ── the three replay properties ──────────────────────────────────────────────


def test_replay_is_idempotent(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """Rebuilding twice equals rebuilding once."""
    append(
        repos,
        taxable_account.account_id,
        TransactionType.DEPOSIT,
        START,
        net_cash_effect=D("100000.00"),
    )
    _buy(
        repos, taxable_account.account_id, aapl.instrument_id, date(2024, 1, 3), "100", "185.00"
    )
    _sell(
        repos, taxable_account.account_id, aapl.instrument_id, date(2025, 6, 2), "40", "210.00"
    )

    engine = ReplayEngine(repos)
    first = engine.rebuild()
    second = engine.rebuild()

    assert first.digest == second.digest
    assert (first.transactions_replayed, first.lots_created, first.dispositions_created) == (
        second.transactions_replayed,
        second.lots_created,
        second.dispositions_created,
    )


def test_replay_is_deterministic_across_connections(
    repos: Repositories,
    taxable_account: Account,
    aapl: Instrument,
    tmp_path: Path,
) -> None:
    """Same ledger, same derived state -- in a different file.

    This is what makes PORT-GIPS-J01's report content hash meaningful: if
    rebuilding could produce a different byte, comparing hashes would tell you
    nothing (PORT-GIPS-J06).
    """
    from portable_core.persistence.connection import open_portfolio
    from portable_core.schema import migrations as M

    for i in range(5):
        _buy(
            repos,
            taxable_account.account_id,
            aapl.instrument_id,
            START + timedelta(days=i),
            "100",
            f"{180 + i}.00",
        )
    _sell(
        repos, taxable_account.account_id, aapl.instrument_id, date(2025, 3, 1), "250", "220.00"
    )

    original = ReplayEngine(repos).rebuild()

    # Rebuild the same ledger in a fresh file.
    other_con = open_portfolio(tmp_path / "clone.port", must_exist=False)
    M.initialise(other_con)
    clone = Repositories(other_con)
    clone.accounts.add(taxable_account)
    for schedule in repos.accounts.rate_schedules(taxable_account.account_id):
        clone.accounts.add_rate_schedule(schedule)
    clone.instruments.add(aapl)
    for txn in repos.transactions.in_ledger_order():
        clone.transactions.append(txn)

    assert ReplayEngine(clone).rebuild().digest == original.digest


def test_replay_is_order_stable(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """Insertion order does not matter; ``(trade_date, seq)`` does.

    A back-dated entry lands after same-day entries already recorded, which is
    the honest ordering: the ledger records when we learned things.
    """
    _buy(
        repos, taxable_account.account_id, aapl.instrument_id, date(2024, 3, 1), "100", "185.00"
    )
    _buy(
        repos, taxable_account.account_id, aapl.instrument_id, date(2024, 1, 5), "100", "170.00"
    )
    _sell(
        repos, taxable_account.account_id, aapl.instrument_id, date(2025, 1, 5), "150", "200.00"
    )

    result = ReplayEngine(repos).rebuild()
    dispositions = repos.lots.dispositions()

    # FIFO must have taken the January lot first despite it being entered second.
    assert dispositions[0].quantity == D("100")
    lot = repos.lots.get(dispositions[0].lot_id)
    assert lot is not None
    assert lot.open_date == date(2024, 1, 5)
    assert result.transactions_replayed == 3


# ── the invariants ───────────────────────────────────────────────────────────


def test_cash_is_conserved(
    repos: Repositories, taxable_account: Account, ira_account: Account, aapl: Instrument
) -> None:
    """CLAUDE.md invariant 4, over a mixed sequence including a transfer."""
    append(
        repos,
        taxable_account.account_id,
        TransactionType.DEPOSIT,
        START,
        net_cash_effect=D("100000.00"),
    )
    _buy(
        repos, taxable_account.account_id, aapl.instrument_id, date(2024, 1, 3), "100", "185.00"
    )
    append(
        repos,
        taxable_account.account_id,
        TransactionType.DIVIDEND,
        date(2024, 3, 1),
        instrument_id=aapl.instrument_id,
        gross_amount=D("24.00"),
        net_cash_effect=D("24.00"),
        ex_date=date(2024, 2, 9),
        pay_date=date(2024, 3, 1),
    )
    append(
        repos,
        taxable_account.account_id,
        TransactionType.TRANSFER,
        date(2024, 4, 1),
        counter_account_id=ira_account.account_id,
        net_cash_effect=D("-5000.00"),
    )
    _sell(
        repos, taxable_account.account_id, aapl.instrument_id, date(2025, 6, 2), "40", "210.00"
    )

    engine = ReplayEngine(repos)
    engine.rebuild()
    assert engine.check_cash_conservation() == []

    taxable_cash, _ = repos.valuations.cash(taxable_account.account_id)
    ira_cash, _ = repos.valuations.cash(ira_account.account_id)
    assert ira_cash == D("5000.00"), "the transfer's other side moved too"
    assert taxable_cash == D("100000.00") - D("18500.00") + D("24.00") - D("5000.00") + D(
        "8400.00"
    )


def test_leg_quantity_equals_the_sum_of_its_open_lots(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """CLAUDE.md invariant 5, per leg (ADR 0009)."""
    for i in range(4):
        _buy(
            repos,
            taxable_account.account_id,
            aapl.instrument_id,
            START + timedelta(days=i),
            "100",
            f"{180 + i}.00",
        )
    _sell(
        repos, taxable_account.account_id, aapl.instrument_id, date(2025, 2, 1), "250", "220.00"
    )

    engine = ReplayEngine(repos)
    engine.rebuild()
    assert engine.check_leg_invariants() == []

    positions = repos.positions.all()
    assert len(positions) == 1
    (leg,) = positions[0].legs
    assert leg.quantity == D("150")


def test_a_fully_closed_position_closes_its_leg_and_itself(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    _buy(repos, taxable_account.account_id, aapl.instrument_id, START, "100", "185.00")
    _sell(
        repos, taxable_account.account_id, aapl.instrument_id, date(2025, 2, 1), "100", "220.00"
    )

    ReplayEngine(repos).rebuild()
    (position,) = repos.positions.all()
    assert position.status.value == "closed"
    assert position.legs[0].status.value == "closed"
    assert position.legs[0].quantity == D("0")


def test_realized_gain_rows_reference_their_stored_disposition(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """A foreign key onto a provisional id would dangle or point at the wrong row."""
    _buy(repos, taxable_account.account_id, aapl.instrument_id, START, "100", "185.00")
    _sell(
        repos, taxable_account.account_id, aapl.instrument_id, date(2025, 6, 2), "100", "210.00"
    )
    ReplayEngine(repos).rebuild()

    dispositions = repos.lots.dispositions()
    gains = repos.lots.realized_gains()
    assert len(gains) == len(dispositions) == 1
    assert gains[0].disposition_id == dispositions[0].disposition_id
    assert gains[0].gain == D("2500.00")
    assert gains[0].estimated_tax == D("720.00"), "long-term at 28.8%"


# ── generated sequences ──────────────────────────────────────────────────────


def _fresh_repos() -> Repositories:
    """A migrated, in-memory portfolio.

    Built inside the test body rather than taken from a fixture: pytest
    fixtures are function-scoped and hypothesis runs many examples inside one
    function call, so a fixture-supplied database would accumulate state across
    examples and every example after the first would fail on a unique
    constraint. That failure looks like a bug in the code under test, which is
    the worst kind of test infrastructure.
    """
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    M.initialise(con)
    return Repositories(con)


@settings(max_examples=40, deadline=None)
@given(
    trades=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=400),  # day offset
            st.integers(min_value=1, max_value=500),  # quantity
            st.integers(min_value=1000, max_value=50000),  # price in cents
            st.booleans(),  # buy or sell
        ),
        min_size=1,
        max_size=25,
    )
)
def test_replay_properties_over_generated_sequences(
    trades: list[tuple[int, int, int, bool]],
) -> None:
    """Determinism, idempotence, cash conservation, and the leg invariant.

    Over generated ledgers, because the interesting failures are the sequences
    nobody thought to write down. Sells that would oversell are skipped rather
    than expected to fail: the refusal path has its own tests, and here we want
    sequences that get all the way through.
    """
    repos = _fresh_repos()
    account_id = repos.accounts.add(
        Account(
            account_id=0,
            name="Generated",
            account_type=AccountType.TAX_EXEMPT,  # no rate schedule needed
            opened_date=START,
            # FIFO rather than the spec-ID default: a generated sell carries no
            # lot designation, and spec-ID correctly refuses one that does not
            # (tested in test_lot_engine.py). Here we want sequences that get
            # all the way through, so the relief method has to be one that can.
            default_relief_method=ReliefMethod.FIFO,
        )
    )
    instrument_id = repos.instruments.add(
        Instrument(instrument_id=0, symbol="GEN", instrument_type=InstrumentType.EQUITY)
    )

    held = D(0)
    applied = 0
    # Sorted by trade date before applying. The holdings tracker below decides
    # whether a sell is legal, and it has to make that decision in the order
    # replay will apply the trades -- not the order they were generated in.
    # Hypothesis found this: a buy on day 1 and a sell on day 0 leaves the sell
    # with no lots, which is correct behaviour and a broken generator.
    for offset, quantity, price_cents, is_buy in sorted(trades, key=lambda t: t[0]):
        on = START + timedelta(days=offset)
        qty, price = D(quantity), D(price_cents) / D(100)
        if is_buy:
            _buy(repos, account_id, instrument_id, on, str(qty), str(price))
            held += qty
            applied += 1
        elif held >= qty:
            _sell(repos, account_id, instrument_id, on, str(qty), str(price))
            held -= qty
            applied += 1

    assume(applied > 0)

    engine = ReplayEngine(repos)
    first = engine.rebuild()
    second = engine.rebuild()

    assert first.digest == second.digest, "replay is not idempotent"
    assert first.warnings == (), first.warnings
    assert engine.check_cash_conservation() == []
    assert engine.check_leg_invariants() == []

    # And the invariant stated directly, rather than through the checker.
    for position in repos.positions.all():
        for leg in position.legs:
            lots = repos.lots.by_leg(leg.leg_id)
            assert leg.quantity == sum(
                (lot.remaining_quantity for lot in lots if lot.is_open), D(0)
            )


def test_no_relief_method_can_leave_a_negative_lot(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """A property the bootstrap names explicitly."""
    for i in range(3):
        _buy(
            repos,
            taxable_account.account_id,
            aapl.instrument_id,
            START + timedelta(days=i),
            "100",
            "185.00",
        )
    _sell(
        repos, taxable_account.account_id, aapl.instrument_id, date(2025, 1, 1), "250", "200.00"
    )
    ReplayEngine(repos).rebuild()

    for lot_row in repos.con.execute("SELECT lot_id FROM lot"):
        lot = repos.lots.get(int(lot_row["lot_id"]))
        assert lot is not None
        assert lot.remaining_quantity >= 0
        assert lot.adjusted_cost_basis >= 0


def test_the_digest_changes_when_derived_state_changes(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """A digest that never changed would prove nothing."""
    _buy(repos, taxable_account.account_id, aapl.instrument_id, START, "100", "185.00")
    engine = ReplayEngine(repos)
    before = engine.rebuild().digest

    _buy(
        repos, taxable_account.account_id, aapl.instrument_id, date(2024, 2, 1), "50", "190.00"
    )
    assert engine.rebuild().digest != before
    assert derived_state_digest(repos) == engine.rebuild().digest


# ── corporate actions must survive a rebuild ─────────────────────────────────


def _corporate_action_portfolio(repos: Repositories) -> tuple[int, int]:
    """An account holding 100 shares, ready for a split or a spinoff."""
    account_id = repos.accounts.add(
        Account(
            account_id=0,
            name="CA",
            account_type=AccountType.TAX_EXEMPT,
            opened_date=START,
            default_relief_method=ReliefMethod.FIFO,
        )
    )
    instrument_id = repos.instruments.add(
        Instrument(instrument_id=0, symbol="ACME", instrument_type=InstrumentType.EQUITY)
    )
    _buy(repos, account_id, instrument_id, date(2024, 2, 1), "100", "60.00")
    return account_id, instrument_id


def test_a_split_survives_a_rebuild(repos: Repositories) -> None:
    """CLAUDE.md invariant 3, for the case that actually broke.

    `pt rebuild` used to silently revert a split -- 300 shares back to 100 --
    because replay had no case for a SPLIT transaction and a free-text note is
    not machine-readable. The parameters now live on the `corporate_action`
    REFERENCE row, which replay reads.
    """
    account_id, instrument_id = _corporate_action_portfolio(repos)
    ex_date = date(2024, 6, 3)

    txn_id = append(
        repos,
        account_id,
        TransactionType.SPLIT,
        ex_date,
        instrument_id=instrument_id,
        quantity=D("300"),
        ex_date=ex_date,
    )
    repos.corporate_actions.add(
        CorporateAction(
            instrument_id=instrument_id,
            action_type="split",
            ex_date=ex_date,
            split_numerator=D("3"),
            split_denominator=D("1"),
            applied_txn_id=txn_id,
        )
    )

    engine = ReplayEngine(repos)
    first = engine.rebuild()
    assert first.warnings == (), first.warnings

    lots = repos.lots.open_lots(account_id, instrument_id)
    assert len(lots) == 1
    assert lots[0].remaining_quantity == D("300")
    assert lots[0].adjusted_cost_basis == D("6000.00"), "total basis unchanged"
    assert lots[0].holding_period_start == date(2024, 2, 1), "period NOT reset"

    # And it is stable: a second rebuild does not compound the split.
    assert engine.rebuild().digest == first.digest
    assert repos.lots.open_lots(account_id, instrument_id)[0].remaining_quantity == D("300")


def test_a_spinoff_survives_a_rebuild(repos: Repositories) -> None:
    """The same bug's sibling, which hid one level deeper.

    A spinoff carries an instrument but no quantity -- the quantity is a
    consequence of the action, not an input to it -- so an early return on a
    missing quantity skipped it while splits, which do carry one, went through.
    """
    account_id, instrument_id = _corporate_action_portfolio(repos)
    spun_id = repos.instruments.add(
        Instrument(instrument_id=0, symbol="NEWCO", instrument_type=InstrumentType.EQUITY)
    )
    ex_date = date(2024, 9, 3)

    txn_id = append(
        repos,
        account_id,
        TransactionType.SPINOFF,
        ex_date,
        instrument_id=instrument_id,
        ex_date=ex_date,
    )
    repos.corporate_actions.add(
        CorporateAction(
            instrument_id=instrument_id,
            action_type="spinoff",
            ex_date=ex_date,
            target_instrument_id=spun_id,
            target_ratio=D("0.5"),
            parent_fmv=D("18.00"),
            target_fmv=D("6.00"),
            applied_txn_id=txn_id,
        )
    )

    engine = ReplayEngine(repos)
    first = engine.rebuild()
    assert first.warnings == (), first.warnings

    parent = repos.lots.open_lots(account_id, instrument_id)
    spun = repos.lots.open_lots(account_id, spun_id)
    assert len(parent) == 1 and len(spun) == 1
    assert spun[0].remaining_quantity == D("50")
    assert parent[0].adjusted_cost_basis + spun[0].adjusted_cost_basis == D("6000.00"), (
        "no basis created or destroyed"
    )
    assert spun[0].holding_period_start == date(2024, 2, 1), (
        "the spun shares inherit the parent's holding period"
    )

    assert engine.rebuild().digest == first.digest


def test_a_corporate_action_with_no_parameters_is_reported_not_ignored(
    repos: Repositories,
) -> None:
    """A ledger row saying "a split happened" cannot be reapplied on its own.

    Silently skipping it is how the original bug looked from the outside: the
    numbers just quietly changed. Replay reports it instead.
    """
    account_id, instrument_id = _corporate_action_portfolio(repos)
    append(
        repos,
        account_id,
        TransactionType.SPLIT,
        date(2024, 6, 3),
        instrument_id=instrument_id,
        quantity=D("300"),
        ex_date=date(2024, 6, 3),
    )

    result = ReplayEngine(repos).rebuild()
    assert any("cannot be reapplied" in w for w in result.warnings), result.warnings
