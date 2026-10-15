"""A grouping is a ledger row, so a rebuild reproduces it. ADR 0023.

`pt position group` used to edit `position_leg` directly -- derived state with
no ledger entry -- so the next rebuild undid it, and `pt validate` failed the
moment one was made. Every back-dated import, every reversal, every
`--incremental` update rebuilds, so a covered call quietly fell apart into a
stock and a short call.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portable_core.domain.enums import (
    InstrumentType,
    LegRole,
    PositionStatus,
    StrategyType,
    TransactionType,
)
from portable_core.domain.models import Account, Instrument, PositionLeg
from portable_core.errors import ValidationError
from portable_core.persistence.repositories import Repositories
from portable_core.services.positions import PositionEngine
from portable_core.services.regrouping import Regroup, record_regroup
from portable_core.services.replay import ReplayEngine, derived_state_digest, state_on
from portable_core.services.trading import TradeIntent, TradingService
from tests.conftest import append

pytestmark = pytest.mark.unit

D = Decimal


@pytest.fixture
def msft(repos: Repositories) -> Instrument:
    found = repos.instruments.get(
        repos.instruments.add(
            Instrument(instrument_id=0, symbol="MSFT", instrument_type=InstrumentType.EQUITY)
        )
    )
    assert found is not None
    return found


def _buy(repos: Repositories, account: Account, instrument: Instrument, on: date) -> int:
    service = TradingService(repos)
    committed = service.commit(
        service.plan(
            TradeIntent(
                account=account,
                instrument=instrument,
                txn_type=TransactionType.BUY,
                quantity=D("10"),
                price=D("100.00"),
                trade_date=on,
            )
        )
    )
    return committed.transaction.txn_id


@pytest.fixture
def two_holdings(
    repos: Repositories, taxable_account: Account, aapl: Instrument, msft: Instrument
) -> tuple[int, int]:
    """AAPL bought 2024-01-03 and MSFT 2024-01-04, each in a position of its own."""
    append(repos, taxable_account.account_id, TransactionType.DEPOSIT, date(2024, 1, 2),
           net_cash_effect=D("100000.00"))  # fmt: skip
    ReplayEngine(repos).rebuild()
    return (
        _buy(repos, taxable_account, aapl, date(2024, 1, 3)),
        _buy(repos, taxable_account, msft, date(2024, 1, 4)),
    )


def _grouping(repos: Repositories) -> dict[str, tuple[int | None, str]]:
    """Symbol -> (the txn that opened its position, that position's strategy).

    By opening transaction, not position_id: a rebuild may renumber positions.
    """
    found: dict[str, tuple[int | None, str]] = {}
    for position in repos.positions.all(open_only=True):
        for leg in position.legs:
            instrument = repos.instruments.get(leg.instrument_id)
            assert instrument is not None
            found[instrument.symbol] = (position.opened_txn_id, str(position.strategy_type))
    return found


def _group_msft_into_aapl(
    repos: Repositories, account: Account, msft: Instrument, aapl_buy: int, on: date
) -> int:
    return record_regroup(
        repos,
        Regroup(
            account_id=account.account_id,
            instrument_id=msft.instrument_id,
            on=on,
            into_opened_by=aapl_buy,
            strategy=StrategyType.CUSTOM,
        ),
    ).transaction.txn_id


def test_a_grouping_survives_a_rebuild(
    repos: Repositories,
    taxable_account: Account,
    msft: Instrument,
    two_holdings: tuple[int, int],
) -> None:
    """The acceptance test of the task: group, rebuild, still grouped."""
    aapl_buy, _ = two_holdings
    _group_msft_into_aapl(repos, taxable_account, msft, aapl_buy, date(2024, 2, 1))
    live = derived_state_digest(repos)

    ReplayEngine(repos).rebuild()

    assert _grouping(repos) == {"AAPL": (aapl_buy, "custom"), "MSFT": (aapl_buy, "custom")}
    assert derived_state_digest(repos) == live, "the live path and replay agree (ADR 0016)"


def test_a_regroup_moves_no_lot_no_basis_and_no_cash(
    repos: Repositories,
    taxable_account: Account,
    msft: Instrument,
    two_holdings: tuple[int, int],
) -> None:
    """ADR 0009: a change of intent does not change tax history."""

    def books() -> list[tuple[object, ...]]:
        lots = [
            (
                lot.lot_id,
                lot.instrument_id,
                lot.open_date,
                lot.remaining_quantity,
                lot.adjusted_cost_basis,
                lot.holding_period_start,
            )
            for leg in [*_legs(repos)]
            for lot in repos.lots.by_leg(leg.leg_id)
        ]
        return [*lots, repos.valuations.cash(taxable_account.account_id)]

    before = books()
    aapl_buy, _ = two_holdings
    _group_msft_into_aapl(repos, taxable_account, msft, aapl_buy, date(2024, 2, 1))

    assert books() == before
    for leg in _legs(repos):
        for lot in repos.lots.by_leg(leg.leg_id):
            assert lot.position_id == leg.position_id, "the lots' copy moved with the leg"


def _legs(repos: Repositories) -> list[PositionLeg]:
    return [leg for position in repos.positions.all() for leg in position.legs]


def test_the_position_a_last_leg_leaves_is_closed_that_day(
    repos: Repositories,
    taxable_account: Account,
    msft: Instrument,
    two_holdings: tuple[int, int],
) -> None:
    aapl_buy, msft_buy = two_holdings
    _group_msft_into_aapl(repos, taxable_account, msft, aapl_buy, date(2024, 2, 1))

    emptied = repos.positions.opened_by(msft_buy)

    assert emptied is not None
    assert (emptied.status, emptied.closed_date) == (PositionStatus.CLOSED, date(2024, 2, 1))


def test_ungrouping_opens_a_position_of_its_own(
    repos: Repositories,
    taxable_account: Account,
    msft: Instrument,
    two_holdings: tuple[int, int],
) -> None:
    aapl_buy, _ = two_holdings
    _group_msft_into_aapl(repos, taxable_account, msft, aapl_buy, date(2024, 2, 1))
    ungroup = record_regroup(
        repos,
        Regroup(taxable_account.account_id, msft.instrument_id, date(2024, 3, 1), None),
    ).transaction.txn_id

    ReplayEngine(repos).rebuild()

    assert _grouping(repos) == {"AAPL": (aapl_buy, "single"), "MSFT": (ungroup, "single")}


def test_a_regroup_is_reversed_like_any_other_entry(
    repos: Repositories,
    taxable_account: Account,
    msft: Instrument,
    two_holdings: tuple[int, int],
) -> None:
    aapl_buy, msft_buy = two_holdings
    regroup = _group_msft_into_aapl(repos, taxable_account, msft, aapl_buy, date(2024, 2, 1))
    append(repos, taxable_account.account_id, TransactionType.REVERSAL, date(2024, 2, 5),
           net_cash_effect=D("0.00"), reverses_txn_id=regroup)  # fmt: skip

    ReplayEngine(repos).rebuild()

    assert _grouping(repos) == {"AAPL": (aapl_buy, "single"), "MSFT": (msft_buy, "single")}


def test_a_grouping_applies_from_its_own_date(
    repos: Repositories,
    taxable_account: Account,
    msft: Instrument,
    two_holdings: tuple[int, int],
) -> None:
    """Dated like any entry, so the state as of an earlier day is ungrouped (ADR 0021)."""
    aapl_buy, msft_buy = two_holdings
    _group_msft_into_aapl(repos, taxable_account, msft, aapl_buy, date(2024, 2, 1))

    before = state_on(repos, date(2024, 1, 31), lambda: _grouping(repos)).results[0]

    assert before == {"AAPL": (aapl_buy, "single"), "MSFT": (msft_buy, "single")}


def test_a_regroup_replay_could_not_apply_is_refused_at_entry(
    repos: Repositories,
    taxable_account: Account,
    msft: Instrument,
    two_holdings: tuple[int, int],
) -> None:
    """A ledger row can never be edited, so an unappliable one is refused before it exists."""
    aapl_buy, _ = two_holdings

    with pytest.raises(ValidationError, match="held no MSFT on 2024-01-03"):
        _group_msft_into_aapl(repos, taxable_account, msft, aapl_buy, date(2024, 1, 3))
    with pytest.raises(ValidationError, match="was not open on 2024-01-04"):
        record_regroup(
            repos,
            Regroup(taxable_account.account_id, msft.instrument_id, date(2024, 1, 4), 999),
        )


def test_two_stocks_grouped_are_custom_not_single() -> None:
    """Inference named any one-role set `single`, so two stocks read as one holding."""
    legs = [
        PositionLeg(
            leg_id=i,
            position_id=1,
            instrument_id=i,
            role=LegRole.LONG_STOCK,
            sign=1,
            quantity=D("1"),
            opened_date=date(2024, 1, 3),
        )
        for i in (1, 2)
    ]

    assert PositionEngine.infer_strategy(legs) is StrategyType.CUSTOM
    assert PositionEngine.infer_strategy(legs[:1]) is StrategyType.SINGLE
