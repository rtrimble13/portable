"""In-kind transfers, and where a seeded lot's basis came from.

ADR 0015 and ADR 0017. The whole point of the transaction type is that a
`transfer_in` carries **two numbers that must not be conflated**:

- the market value on the transfer date, which is the flow amount;
- the delivering custodian's basis and acquisition date, which are unrelated to
  the transfer and are what the tax engine uses forever after.

Swap them and neither error announces itself. Value as basis makes every future
sale report the gain since the transfer instead of since the purchase. Basis as
value makes the period's return wrong by the entire unrealized gain. So most of
what follows is about keeping those two apart, and about the third number that
must not appear at all: the cash flow a back-dated `buy` would have invented.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from portable_core.domain.enums import (
    AccountType,
    BasisSource,
    FlowClassification,
    FlowLevel,
    InstrumentType,
    TransactionType,
)
from portable_core.domain.models import Account, Instrument, Transaction
from portable_core.errors import ValidationError
from portable_core.services.cash_flow import classify
from portable_core.services.trading import TradingService

pytestmark = pytest.mark.unit

TRANSFERRED = date(2022, 5, 1)
BOUGHT = date(2018, 3, 14)


@pytest.fixture
def account() -> Account:
    return Account(
        account_id=1,
        name="IRA",
        account_type=AccountType.TAX_DEFERRED,
        opened_date=date(2022, 5, 1),
        currency="USD",
    )


@pytest.fixture
def instrument() -> Instrument:
    return Instrument(
        instrument_id=1,
        symbol="VTI",
        name="Vanguard Total Market",
        instrument_type=InstrumentType.ETF,
        currency="USD",
    )


class _Ledger:
    """The one thing these builders read: the next sequence for a date.

    Stubbed rather than mocked away, because the reason it is read matters --
    two in-kind transfers on one date must not collide, and seeding a cutover
    puts dozens on a single date.
    """

    def __init__(self) -> None:
        self.issued: list[date] = []

    def next_seq(self, on: date) -> int:
        self.issued.append(on)
        return self.issued.count(on)


def _service() -> TradingService:
    """A service with just enough repository behind it to build a row."""
    service = TradingService.__new__(TradingService)
    service.repos = SimpleNamespace(transactions=_Ledger())  # type: ignore[assignment]
    service.check_external_ref = lambda *a, **k: None  # type: ignore[method-assign]
    return service


def _transfer_in(
    account: Account,
    instrument: Instrument,
    *,
    quantity: str = "100",
    value: str = "20000.00",
    basis: str | None = "12500.00",
    acquired: date | None = BOUGHT,
    basis_source: BasisSource = BasisSource.CUSTODIAN_ASSERTED,
    assumption: str | None = None,
) -> Transaction:
    return _service().record_transfer_in(
        account,
        instrument,
        Decimal(quantity),
        TRANSFERRED,
        value=Decimal(value),
        original_basis=Decimal(basis) if basis is not None else None,
        original_acquired_date=acquired,
        basis_source=basis_source,
        basis_assumption=assumption,
    )


# ── the two numbers ──────────────────────────────────────────────────────────


def test_the_value_and_the_basis_stay_apart(account: Account, instrument: Instrument) -> None:
    """The failure this type exists to prevent, asserted directly."""
    txn = _transfer_in(account, instrument)
    assert txn.gross_amount == Decimal("20000.00")  # what arrived, on the day
    assert txn.original_basis == Decimal("12500.00")  # what was paid, in 2018
    assert txn.gross_amount != txn.original_basis


def test_the_price_is_the_transfer_value_per_share(
    account: Account, instrument: Instrument
) -> None:
    """Not the basis per share: `price` is a market price on `trade_date`."""
    assert _transfer_in(account, instrument).price == Decimal("200.00")


def test_no_cash_moves(account: Account, instrument: Instrument) -> None:
    """The reason this is a transaction type and not a back-dated buy.

    A buy would invent a cash outflow, which would need an invented deposit to
    fund it -- and that deposit is an external cash flow. Inventing external
    flows is precisely how a track record is silently rewritten (ADR 0007).
    """
    assert _transfer_in(account, instrument).net_cash_effect == Decimal("0.00")


def test_the_acquisition_date_is_carried_not_the_transfer_date(
    account: Account, instrument: Instrument
) -> None:
    """A change of custodian is not a disposition.

    Restarting the holding period would convert long-term gains into short-term
    ones on the next sale -- a wrong number in the direction of a larger tax
    bill, arrived at silently.
    """
    txn = _transfer_in(account, instrument)
    assert txn.original_acquired_date == BOUGHT
    assert txn.trade_date == TRANSFERRED


# ── refusals ─────────────────────────────────────────────────────────────────


def test_a_derived_basis_is_refused(account: Account, instrument: Instrument) -> None:
    """'derived' means portable computed it from its own ledger.

    This basis came from somewhere else, and letting it claim otherwise would
    make an external assertion indistinguishable from `portable`'s own
    arithmetic -- the one thing the ladder exists to prevent.
    """
    with pytest.raises(ValidationError, match="cannot be 'derived'") as excinfo:
        _transfer_in(account, instrument, basis_source=BasisSource.DERIVED)
    assert excinfo.value.code == "PT-E-BASIS-SOURCE-INVALID"


def test_an_asserted_source_with_no_basis_is_refused(
    account: Account, instrument: Instrument
) -> None:
    with pytest.raises(ValidationError, match="asserts a basis, but none was given"):
        _transfer_in(account, instrument, basis=None)


def test_unavailable_carries_no_basis_and_is_refused_one(
    account: Account, instrument: Instrument
) -> None:
    """'unavailable' means no equation constrains this block (ADR 0017 §2b).

    A figure alongside it would be invented, and 'absent' and 'unavailable' are
    different claims about the evidence.
    """
    with pytest.raises(ValidationError, match="carries no basis, but one was given"):
        _transfer_in(
            account,
            instrument,
            basis_source=BasisSource.UNAVAILABLE,
            assumption="the block was fully consumed after the cutover",
        )


def test_unavailable_with_no_basis_is_accepted(
    account: Account, instrument: Instrument
) -> None:
    txn = _transfer_in(
        account,
        instrument,
        basis=None,
        basis_source=BasisSource.UNAVAILABLE,
        assumption="nothing of the block survives to anchor a solve",
    )
    assert txn.original_basis is None
    assert txn.basis_source is BasisSource.UNAVAILABLE


@pytest.mark.parametrize(
    "source",
    [BasisSource.RECONSTRUCTED, BasisSource.ESTIMATED, BasisSource.UNAVAILABLE],
)
def test_an_approximate_basis_must_say_how_it_was_arrived_at(
    account: Account, instrument: Instrument, source: BasisSource
) -> None:
    """ADR 0017 §2. An approximation with no stated reasoning is
    indistinguishable from a number somebody made up."""
    with pytest.raises(ValidationError, match="needs a stated assumption"):
        _transfer_in(
            account,
            instrument,
            basis=None if source is BasisSource.UNAVAILABLE else "12500.00",
            basis_source=source,
            assumption=None,
        )


def test_a_custodian_asserted_basis_needs_no_assumption(
    account: Account, instrument: Instrument
) -> None:
    """It is a figure somebody else stated, not one this code worked out."""
    txn = _transfer_in(account, instrument, basis_source=BasisSource.CUSTODIAN_ASSERTED)
    assert txn.basis_assumption is None


def test_an_acquisition_after_the_transfer_is_refused(
    account: Account, instrument: Instrument
) -> None:
    """The owner bought the shares before they were transferred, by definition."""
    with pytest.raises(ValidationError, match="after the transfer"):
        _transfer_in(account, instrument, acquired=date(2023, 1, 1))


def test_a_negative_basis_is_refused(account: Account, instrument: Instrument) -> None:
    """A genuine zero basis is legitimate; a negative one is a sign error."""
    with pytest.raises(ValidationError, match="cannot be negative"):
        _transfer_in(account, instrument, basis="-100.00")


def test_a_zero_basis_is_accepted(account: Account, instrument: Instrument) -> None:
    """A contra or CVR security from an acquisition arrives with one, and ADR
    0017 says a zero basis there is derived fact rather than estimate."""
    assert _transfer_in(account, instrument, basis="0.00").original_basis == Decimal("0.00")


@pytest.mark.parametrize("quantity", ["0", "-10"])
def test_a_non_positive_quantity_is_refused(
    account: Account, instrument: Instrument, quantity: str
) -> None:
    with pytest.raises(ValidationError, match="quantity must be positive"):
        _transfer_in(account, instrument, quantity=quantity)


def test_a_fractional_share_an_account_cannot_hold_is_refused(
    instrument: Instrument,
) -> None:
    whole_only = Account(
        account_id=1,
        name="IRA",
        account_type=AccountType.TAX_DEFERRED,
        opened_date=date(2022, 5, 1),
        currency="USD",
        allows_fractional=False,
    )
    with pytest.raises(ValidationError, match="cannot hold fractional shares"):
        _transfer_in(whole_only, instrument, quantity="10.5")


# ── the outward side ─────────────────────────────────────────────────────────


def test_a_transfer_out_asserts_no_basis(account: Account, instrument: Instrument) -> None:
    """The lots being relieved already carry their own."""
    txn = _service().record_transfer_out(
        account, instrument, Decimal("40"), TRANSFERRED, value=Decimal("9000.00")
    )
    assert txn.txn_type is TransactionType.TRANSFER_OUT
    assert txn.original_basis is None
    assert txn.basis_source is None
    assert txn.net_cash_effect == Decimal("0.00")
    assert txn.gross_amount == Decimal("9000.00")


# ── flow classification (ADR 0015, PORT-GIPS-C02) ────────────────────────────


def _row(kind: TransactionType, gross: str = "20000.00") -> Transaction:
    return Transaction(
        txn_id=1,
        account_id=1,
        trade_date=TRANSFERRED,
        seq=1,
        txn_type=kind,
        net_cash_effect=Decimal("0.00"),
        gross_amount=Decimal(gross),
        quantity=Decimal("100"),
        instrument_id=1,
    )


@pytest.mark.parametrize("level", list(FlowLevel))
def test_an_in_kind_transfer_is_external_at_both_levels(level: FlowLevel) -> None:
    """Unlike `transfer`, which is external at account level and no flow at
    portfolio level. These cross the boundary and do not net, which is exactly
    why ADR 0015 refused to overload one type."""
    result = classify(_row(TransactionType.TRANSFER_IN), level)
    assert result.classification is FlowClassification.EXTERNAL
    assert result.is_in_kind is True
    assert result.amount == Decimal("20000.00")


@pytest.mark.parametrize("level", list(FlowLevel))
def test_an_internal_transfer_still_nets_to_nothing_at_portfolio_level(
    level: FlowLevel,
) -> None:
    """The contrast that makes the previous test mean something."""
    row = Transaction(
        txn_id=1,
        account_id=1,
        trade_date=TRANSFERRED,
        seq=1,
        txn_type=TransactionType.TRANSFER,
        net_cash_effect=Decimal("-5000.00"),
        counter_account_id=2,
    )
    result = classify(row, level)
    if level is FlowLevel.PORTFOLIO:
        assert result.classification is FlowClassification.INTERNAL
        assert result.amount == Decimal("0.00")
    else:
        assert result.classification is FlowClassification.EXTERNAL


def test_a_transfer_out_is_an_outward_flow() -> None:
    result = classify(_row(TransactionType.TRANSFER_OUT), FlowLevel.PORTFOLIO)
    assert result.amount == Decimal("-20000.00")


def test_the_flow_is_the_market_value_never_the_basis() -> None:
    """Using basis as the flow amount would make the period's return wrong by
    the whole unrealized gain."""
    row = replace(_row(TransactionType.TRANSFER_IN), original_basis=Decimal("12500.00"))
    assert classify(row, FlowLevel.PORTFOLIO).amount == Decimal("20000.00")


def test_two_transfers_on_one_date_get_distinct_sequences(
    account: Account, instrument: Instrument
) -> None:
    """Seeding a cutover puts dozens of these on a single date.

    Every other write path assigns `seq` from the ledger; these two did not,
    and collided on `UNIQUE (trade_date, seq)` the first time two were recorded
    for one day — the ordinary case for the feature, not an edge of it. This
    fix is also in the pull request for the tax disclosure; it is here because
    without it `pt import broker` cannot seed a cutover at all.
    """
    service = _service()
    first = service.record_transfer_in(
        account,
        instrument,
        Decimal("100"),
        TRANSFERRED,
        value=Decimal("20000.00"),
        original_basis=Decimal("12500.00"),
        original_acquired_date=BOUGHT,
        basis_source=BasisSource.CUSTODIAN_ASSERTED,
    )
    second = service.record_transfer_in(
        account,
        instrument,
        Decimal("40"),
        TRANSFERRED,
        value=Decimal("8000.00"),
        original_basis=Decimal("5000.00"),
        original_acquired_date=BOUGHT,
        basis_source=BasisSource.CUSTODIAN_ASSERTED,
    )
    assert (first.seq, second.seq) == (1, 2)
