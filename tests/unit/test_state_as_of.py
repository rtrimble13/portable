"""Derived state as of a past date: the walk, its fast path, and its netting.

ADR 0021. Five commands answer for an earlier date, and every one of them used
to read *current* derived state and label it with that date -- today's book at
yesterday's prices, marked complete. The walk replays the effective ledger
forward in a savepoint that is rolled back, pausing at each requested date.

Every test here fails against the code before ADR 0021, or pins a property the
fix depends on.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from portable_core.domain.enums import InstrumentType, TransactionType
from portable_core.domain.models import Account, Instrument, Price
from portable_core.persistence.connection import transaction
from portable_core.persistence.repositories import Repositories
from portable_core.services.replay import (
    ReplayEngine,
    derived_state_digest,
    effective_ledger,
    over_dates,
    state_on,
)
from tests.conftest import append

pytestmark = pytest.mark.unit

D = Decimal

DEPOSITED = date(2024, 1, 2)
BOUGHT = date(2024, 1, 3)
SOLD = date(2025, 3, 3)
REBOUGHT = date(2025, 3, 4)
BEFORE_SALE = date(2024, 6, 30)


def _book(repos: Repositories, account: Account, aapl: Instrument, msft: Instrument) -> None:
    """The reproduction in ADR 0021: 100 AAPL held through mid-2024, then MSFT."""
    account_id = account.account_id
    append(
        repos, account_id, TransactionType.DEPOSIT, DEPOSITED, net_cash_effect=D("100000.00")
    )
    append(
        repos,
        account_id,
        TransactionType.BUY,
        BOUGHT,
        instrument_id=aapl.instrument_id,
        quantity=D("100"),
        price=D("150.00"),
        gross_amount=D("15000.00"),
        net_cash_effect=D("-15000.00"),
    )
    append(
        repos,
        account_id,
        TransactionType.SELL,
        SOLD,
        instrument_id=aapl.instrument_id,
        quantity=D("100"),
        price=D("200.00"),
        gross_amount=D("20000.00"),
        net_cash_effect=D("20000.00"),
    )
    append(
        repos,
        account_id,
        TransactionType.BUY,
        REBOUGHT,
        instrument_id=msft.instrument_id,
        quantity=D("10"),
        price=D("400.00"),
        gross_amount=D("4000.00"),
        net_cash_effect=D("-4000.00"),
    )
    ReplayEngine(repos).rebuild()


@pytest.fixture
def msft(repos: Repositories) -> Instrument:
    instrument_id = repos.instruments.add(
        Instrument(instrument_id=0, symbol="MSFT", instrument_type=InstrumentType.EQUITY)
    )
    instrument = repos.instruments.get(instrument_id)
    assert instrument is not None
    return instrument


def _held(repos: Repositories, account_id: int) -> dict[str, Decimal]:
    """Open quantity per symbol, read from derived state as it stands."""
    held: dict[str, Decimal] = {}
    for position in repos.positions.all(account_id=account_id, open_only=True):
        for leg in position.legs:
            if leg.quantity == 0:
                continue
            instrument = repos.instruments.get(leg.instrument_id)
            assert instrument is not None
            held[instrument.symbol] = leg.quantity
    return held


def _cash(repos: Repositories, account_id: int) -> Decimal:
    return repos.valuations.cash(account_id)[0]


# ── the reproduction ─────────────────────────────────────────────────────────


def test_state_on_a_past_date_is_the_book_as_it_stood(
    repos: Repositories, taxable_account: Account, aapl: Instrument, msft: Instrument
) -> None:
    """100 AAPL and 85,000 cash on 2024-06-30 -- not today's MSFT and 101,000.

    Before ADR 0021 every as-of command read current state here.
    """
    _book(repos, taxable_account, aapl, msft)
    account_id = taxable_account.account_id

    walked = state_on(
        repos, BEFORE_SALE, lambda: (_held(repos, account_id), _cash(repos, account_id))
    )

    held, cash = walked.results[0]
    assert held == {"AAPL": D("100")}
    assert cash == D("85000.00")
    assert walked.warnings == ()


def test_one_walk_answers_every_date_in_a_range(
    repos: Repositories, taxable_account: Account, aapl: Instrument, msft: Instrument
) -> None:
    """Each date sees exactly the rows dated on or before it, in ledger order."""
    _book(repos, taxable_account, aapl, msft)
    account_id = taxable_account.account_id
    days = [DEPOSITED, BOUGHT, SOLD, REBOUGHT]

    walked = ReplayEngine(repos).walk(
        reversed(days),  # order and duplicates are the walk's problem, not the caller's
        lambda _on: (_held(repos, account_id), _cash(repos, account_id)),
    )

    assert list(walked.results) == [
        ({}, D("100000.00")),
        ({"AAPL": D("100")}, D("85000.00")),
        ({}, D("105000.00")),
        ({"MSFT": D("10")}, D("101000.00")),
    ]


def test_the_walk_leaves_stored_derived_state_byte_identical(
    repos: Repositories, taxable_account: Account, aapl: Instrument, msft: Instrument
) -> None:
    """Rewinding is a question, not a repair (the ADR 0016 discipline)."""
    _book(repos, taxable_account, aapl, msft)
    before = derived_state_digest(repos)

    ReplayEngine(repos).walk([BEFORE_SALE, SOLD], lambda _on: None)

    assert derived_state_digest(repos) == before
    assert _held(repos, taxable_account.account_id) == {"MSFT": D("10")}
    assert _cash(repos, taxable_account.account_id) == D("101000.00")


def test_the_walk_at_the_last_date_reproduces_stored_state_exactly(
    repos: Repositories, taxable_account: Account, aapl: Instrument, msft: Instrument
) -> None:
    """The walk is the replay, truncated -- not a second derivation.

    At the ledger's last date it must reach the very state `rebuild()` wrote,
    digest for digest. If the two ever drifted, as-of reports and current
    reports would disagree about the same day.
    """
    _book(repos, taxable_account, aapl, msft)
    stored = derived_state_digest(repos)

    walked = ReplayEngine(repos).walk([REBOUGHT], lambda _on: derived_state_digest(repos))

    assert walked.results == (stored,)


def test_the_fast_path_and_the_walk_agree(
    repos: Repositories, taxable_account: Account, aapl: Instrument, msft: Instrument
) -> None:
    """With nothing dated after D, stored state is read directly -- and is right.

    `over_dates` skips the replay only when no ledger row is dated after the
    earliest date asked for. Both routes must give the same answer, or the
    fast path is a second, unaudited truth.
    """
    _book(repos, taxable_account, aapl, msft)
    account_id = taxable_account.account_id
    later = date(2025, 12, 31)

    def read() -> tuple[dict[str, Decimal], Decimal]:
        return _held(repos, account_id), _cash(repos, account_id)

    assert not repos.transactions.any_after(later)
    fast = state_on(repos, later, read).results
    walked = ReplayEngine(repos).walk([later], lambda _on: read()).results

    assert fast == walked == (({"MSFT": D("10")}, D("101000.00")),)
    assert repos.transactions.any_after(BEFORE_SALE)


# ── reversals: the corrected ledger, truncated ───────────────────────────────


def test_a_reversal_dated_later_nets_its_original_out_of_earlier_state(
    repos: Repositories, taxable_account: Account
) -> None:
    """ADR 0021 decision 1: a reversal corrects the record; it is not an event.

    A 50,000 deposit keyed in error on 2024-02-01 and reversed on 2024-02-20.
    On 2024-02-10 the account held 10,000. "As booked through" that date it
    held 60,000 -- the reading a truncated rebuild would give, and the reason
    `rebuild(until=)` was removed.
    """
    account_id = taxable_account.account_id
    append(repos, account_id, TransactionType.DEPOSIT, DEPOSITED, net_cash_effect=D("10000.00"))
    mistake = append(
        repos,
        account_id,
        TransactionType.DEPOSIT,
        date(2024, 2, 1),
        net_cash_effect=D("50000.00"),
    )
    append(
        repos,
        account_id,
        TransactionType.REVERSAL,
        date(2024, 2, 20),
        net_cash_effect=D("-50000.00"),
        reverses_txn_id=mistake,
    )
    ReplayEngine(repos).rebuild()

    walked = state_on(repos, date(2024, 2, 10), lambda: _cash(repos, account_id))

    assert walked.results == (D("10000.00"),)


def test_effective_ledger_nets_over_the_whole_ledger_and_keeps_order(
    repos: Repositories, taxable_account: Account
) -> None:
    """Both rows of a reversed pair go; everything else stays, in ledger order."""
    account_id = taxable_account.account_id
    first = append(
        repos, account_id, TransactionType.DEPOSIT, DEPOSITED, net_cash_effect=D("1.00")
    )
    wrong = append(
        repos, account_id, TransactionType.DEPOSIT, date(2024, 2, 1), net_cash_effect=D("2.00")
    )
    reversal = append(
        repos,
        account_id,
        TransactionType.REVERSAL,
        date(2024, 3, 1),
        net_cash_effect=D("-2.00"),
        reverses_txn_id=wrong,
    )
    last = append(
        repos, account_id, TransactionType.DEPOSIT, date(2024, 4, 1), net_cash_effect=D("3.00")
    )

    kept = [txn.txn_id for txn in effective_ledger(repos.transactions.in_ledger_order())]

    assert kept == [first, last]
    assert wrong not in kept and reversal not in kept


# ── the savepoint ────────────────────────────────────────────────────────────


def test_a_walk_inside_a_transaction_keeps_the_callers_writes(
    repos: Repositories, taxable_account: Account, aapl: Instrument, msft: Instrument
) -> None:
    """`pt value` walks and then persists in one transaction.

    The walk must undo only what it did: a write the caller made before it in
    the same transaction survives, and the transaction still commits.
    """
    _book(repos, taxable_account, aapl, msft)
    before = derived_state_digest(repos)

    with transaction(repos.con):
        price_id = repos.prices.add(
            Price(
                instrument_id=aapl.instrument_id,
                price_date=BEFORE_SALE,
                price=D("155.00"),
                source="test",
                as_of=datetime(2024, 6, 30, tzinfo=UTC),
            )
        )
        walked = over_dates(
            repos, [BEFORE_SALE], lambda _on: _held(repos, taxable_account.account_id)
        )

    assert walked.results == ({"AAPL": D("100")},)
    assert derived_state_digest(repos) == before
    stored = repos.prices.newest_on_or_before(aapl.instrument_id, BEFORE_SALE)
    assert stored is not None and stored.price_id == price_id
