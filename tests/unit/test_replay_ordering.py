"""Out-of-order ledger appends, and the digest that has to notice them.

ADR 0016. `CLAUDE.md` invariant 3 says stored derived state must be exactly
reproducible by replaying the ledger. Two things were needed to make that true
rather than merely asserted:

* a live append that does not sort last must rebuild, because deriving
  incrementally against present-tense state consumes the wrong lots; and
* the digest must cover the relationships a derived row hangs on, or two lots
  that swapped instruments can hash identically.

Every test here fails without the corresponding half of the fix.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portable_core.domain.enums import ReliefMethod, TransactionType
from portable_core.domain.models import Account, Instrument
from portable_core.persistence.repositories import Repositories
from portable_core.services.replay import (
    ReplayEngine,
    derived_state_digest,
    derived_state_digests,
)
from portable_core.services.trading import CommitResult, TradeIntent, TradingService
from tests.conftest import append

pytestmark = pytest.mark.unit

D = Decimal


def _trade(
    repos: Repositories,
    account: Account,
    instrument: Instrument,
    txn_type: TransactionType,
    on: date,
    qty: str,
    price: str,
) -> CommitResult:
    """Record a trade the way `pt buy` does -- plan, then commit."""
    service = TradingService(repos)
    plan = service.plan(
        TradeIntent(
            account=account,
            instrument=instrument,
            txn_type=txn_type,
            quantity=D(qty),
            price=D(price),
            trade_date=on,
        )
    )
    return service.commit(plan)


def _fund(repos: Repositories, account: Account) -> None:
    append(
        repos,
        account.account_id,
        TransactionType.DEPOSIT,
        date(2024, 1, 2),
        net_cash_effect=D("100000.00"),
    )
    ReplayEngine(repos).rebuild()


# ── the reproduction from ADR 0016 ───────────────────────────────────────────


def test_a_back_dated_append_leaves_state_a_rebuild_would_not_change(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """Stored derived state must equal a replay, even after a back-dated entry.

    Buy, sell under FIFO, then record a purchase dated *earlier* than both. The
    ledger's order puts that purchase first, so FIFO must consume it -- which
    only happens if the append rebuilds rather than extending present-tense
    state.
    """
    _fund(repos, taxable_account)
    _trade(repos, taxable_account, aapl, TransactionType.BUY, date(2024, 1, 10), "100", "100")
    _trade(repos, taxable_account, aapl, TransactionType.SELL, date(2024, 6, 3), "50", "150")

    _trade(repos, taxable_account, aapl, TransactionType.BUY, date(2024, 1, 5), "50", "80")

    stored = derived_state_digest(repos)
    ReplayEngine(repos).rebuild()
    assert derived_state_digest(repos) == stored, (
        "derived state disagrees with a replay of the ledger after a back-dated entry"
    )


def test_a_back_dated_append_consumes_the_lots_the_ledger_order_says(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """The same divergence, as the number a person would actually read.

    The back-dated purchase at 80 sorts ahead of the one at 100, so FIFO relieves
    it: proceeds 7,500 less basis 4,000. Deriving incrementally relieves the lot
    that happened to exist when the sale was recorded and reports 2,500 -- a
    thousand dollars of gain, and the tax on it, turning on entry order.
    """
    _fund(repos, taxable_account)
    _trade(repos, taxable_account, aapl, TransactionType.BUY, date(2024, 1, 10), "100", "100")
    _trade(repos, taxable_account, aapl, TransactionType.SELL, date(2024, 6, 3), "50", "150")
    _trade(repos, taxable_account, aapl, TransactionType.BUY, date(2024, 1, 5), "50", "80")

    gains = repos.lots.realized_gains(account_id=taxable_account.account_id)
    assert len(gains) == 1
    assert gains[0].gain == D("3500.00")


def test_an_append_that_sorts_last_does_not_rebuild(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """The common case stays incremental -- a rebuild per trade would be waste."""
    _fund(repos, taxable_account)
    first = _trade(
        repos, taxable_account, aapl, TransactionType.BUY, date(2024, 1, 10), "100", "100"
    )
    later = _trade(
        repos, taxable_account, aapl, TransactionType.BUY, date(2024, 2, 10), "10", "120"
    )
    assert first.rebuilt is False
    assert later.rebuilt is False


def test_a_back_dated_append_reports_that_it_rebuilt(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """`rebuilt` is surfaced, because it can change a number already reported."""
    _fund(repos, taxable_account)
    _trade(repos, taxable_account, aapl, TransactionType.BUY, date(2024, 3, 10), "100", "100")
    back_dated = _trade(
        repos, taxable_account, aapl, TransactionType.BUY, date(2024, 2, 1), "10", "90"
    )
    assert back_dated.rebuilt is True


def test_same_day_entry_after_an_existing_row_still_sorts_last(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """`seq` is assigned as the day's maximum plus one, so a same-day entry is last.

    This is the boundary the ordering check has to get right: a second trade on a
    date that already has one is *not* back-dated, and rebuilding for it would be
    a full replay on every ordinary trading day.
    """
    _fund(repos, taxable_account)
    _trade(repos, taxable_account, aapl, TransactionType.BUY, date(2024, 1, 10), "100", "100")
    same_day = _trade(
        repos, taxable_account, aapl, TransactionType.BUY, date(2024, 1, 10), "25", "101"
    )
    assert same_day.rebuilt is False


# ── the predicate underneath it ──────────────────────────────────────────────


def test_count_after_sees_only_rows_later_in_ledger_order(
    repos: Repositories, taxable_account: Account
) -> None:
    early = date(2024, 1, 5)
    late = date(2024, 3, 5)
    append(repos, taxable_account.account_id, TransactionType.DEPOSIT, late)
    append(repos, taxable_account.account_id, TransactionType.DEPOSIT, early)
    first_on_late = repos.transactions.next_seq(late) - 1

    # The row on the later date has nothing after it; the back-dated one does.
    assert repos.transactions.count_after(late, first_on_late) == 0
    assert repos.transactions.count_after(early, 1) == 1


def test_count_after_is_strict_on_the_row_itself(
    repos: Repositories, taxable_account: Account
) -> None:
    """A row does not count as sorting after itself."""
    on = date(2024, 1, 5)
    append(repos, taxable_account.account_id, TransactionType.DEPOSIT, on)
    append(repos, taxable_account.account_id, TransactionType.DEPOSIT, on)
    assert repos.transactions.count_after(on, 1) == 1
    assert repos.transactions.count_after(on, 2) == 0


# ── the digest has to cover relationships, not only values ───────────────────


def test_the_digest_distinguishes_lots_on_different_instruments(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """Moving a lot to another instrument must change the digest.

    Excluding every ``*_id`` column left the digest blind to this: the quantity
    and basis were hashed and *which instrument they belonged to* was not, so a
    lot pointing somewhere else could hash identically.
    """
    msft = repos.instruments.get(
        repos.instruments.add(
            Instrument(
                instrument_id=0,
                symbol="MSFT",
                instrument_type=aapl.instrument_type,
                name="Microsoft",
            )
        )
    )
    assert msft is not None

    _fund(repos, taxable_account)
    _trade(repos, taxable_account, aapl, TransactionType.BUY, date(2024, 1, 10), "100", "100")

    before = derived_state_digests(repos)["lot"]
    repos.con.execute(
        "UPDATE lot SET instrument_id = ? WHERE instrument_id = ?",
        (msft.instrument_id, aapl.instrument_id),
    )
    assert derived_state_digests(repos)["lot"] != before


def test_the_digest_distinguishes_positions_in_different_accounts(
    repos: Repositories, taxable_account: Account, ira_account: Account, aapl: Instrument
) -> None:
    """The same, for the other relationship a derived row hangs on."""
    _fund(repos, taxable_account)
    _trade(repos, taxable_account, aapl, TransactionType.BUY, date(2024, 1, 10), "100", "100")

    before = derived_state_digests(repos)["position"]
    repos.con.execute(
        "UPDATE position SET account_id = ? WHERE account_id = ?",
        (ira_account.account_id, taxable_account.account_id),
    )
    assert derived_state_digests(repos)["position"] != before


def test_the_digest_still_ignores_reassigned_surrogate_keys(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """A rebuild may renumber rowids, so the digest must be indifferent to them.

    This is the property the ``*_id`` exclusion was protecting, and resolving
    foreign keys to natural keys must not cost it.
    """
    _fund(repos, taxable_account)
    _trade(repos, taxable_account, aapl, TransactionType.BUY, date(2024, 1, 10), "100", "100")
    _trade(repos, taxable_account, aapl, TransactionType.BUY, date(2024, 2, 10), "50", "110")

    before = derived_state_digest(repos)
    ReplayEngine(repos).rebuild()
    repos.con.execute("UPDATE lot SET lot_id = lot_id + 1000")
    assert derived_state_digest(repos) == before


def test_fee_class_is_still_required_on_a_back_dated_trade(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """Rebuilding on a back-dated append must not bypass validation.

    The rebuild happens after the row is planned and appended, so every refusal
    that guards an ordinary trade still guards a back-dated one (PORT-GIPS-D01).
    """
    from portable_core.errors import ValidationError

    _fund(repos, taxable_account)
    _trade(repos, taxable_account, aapl, TransactionType.BUY, date(2024, 3, 10), "100", "100")

    service = TradingService(repos)
    with pytest.raises(ValidationError):
        service.plan(
            TradeIntent(
                account=taxable_account,
                instrument=aapl,
                txn_type=TransactionType.BUY,
                quantity=D("10"),
                price=D("90"),
                trade_date=date(2024, 2, 1),
                fees=D("4.95"),
                fee_class=None,
                relief_method=ReliefMethod.FIFO,
            )
        )
