"""What a historical price load asks for: held spans, and the ticker each day.

ADR 0020 R2: the instruments a return has to value include every one since
sold, for the days it was owned -- not the instruments held today. And a
provider resolves a ticker as of a date (ADR 0006), so a renamed instrument is
asked for under each ticker it carried, for the days it carried it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portable_core.domain.enums import InstrumentType, ReliefMethod, TransactionType
from portable_core.domain.models import Account, Instrument
from portable_core.errors import ValidationError
from portable_core.persistence.repositories import Repositories
from portable_core.services.price_history import (
    PriceRequest,
    Span,
    differences,
    held_spans,
    lookups_when_last_held,
    requests_for,
    requests_while_held,
    ticker_spans,
)
from portable_core.services.replay import ReplayEngine
from tests.conftest import append

pytestmark = pytest.mark.unit

D = Decimal


def _trade(
    repos: Repositories, account: Account, instrument: Instrument, kind: str, on: date, qty: str
) -> None:
    gross = D(qty) * D("100.00")
    append(
        repos,
        account.account_id,
        TransactionType.BUY if kind == "buy" else TransactionType.SELL,
        on,
        instrument_id=instrument.instrument_id,
        quantity=D(qty),
        price=D("100.00"),
        gross_amount=gross,
        net_cash_effect=-gross if kind == "buy" else gross,
        # Per trade, because the IRA fixture defaults to spec-ID, which rightly
        # refuses a sale that names no lots.
        relief_method=None if kind == "buy" else ReliefMethod.FIFO,
    )


@pytest.fixture
def msft(repos: Repositories) -> Instrument:
    instrument_id = repos.instruments.add(
        Instrument(instrument_id=0, symbol="MSFT", instrument_type=InstrumentType.EQUITY)
    )
    found = repos.instruments.get(instrument_id)
    assert found is not None
    return found


@pytest.fixture
def book(
    repos: Repositories, taxable_account: Account, ira_account: Account, aapl: Instrument,
    msft: Instrument,
) -> Repositories:  # fmt: skip
    """AAPL held, sold out, bought back; MSFT held in two accounts that overlap."""
    for account in (taxable_account, ira_account):
        append(repos, account.account_id, TransactionType.DEPOSIT, date(2024, 1, 2),
               net_cash_effect=D("100000.00"))  # fmt: skip
    _trade(repos, taxable_account, aapl, "buy", date(2024, 1, 3), "10")
    _trade(repos, taxable_account, aapl, "sell", date(2024, 3, 1), "10")
    _trade(repos, taxable_account, aapl, "buy", date(2024, 6, 3), "5")
    _trade(repos, taxable_account, msft, "buy", date(2024, 2, 1), "10")
    _trade(repos, ira_account, msft, "buy", date(2024, 2, 15), "10")
    _trade(repos, taxable_account, msft, "sell", date(2024, 4, 1), "10")
    _trade(repos, ira_account, msft, "sell", date(2024, 5, 1), "10")
    assert ReplayEngine(repos).rebuild().warnings == ()
    return repos


def test_an_instrument_sold_since_is_held_for_the_days_it_was_owned(
    book: Repositories, aapl: Instrument, msft: Instrument
) -> None:
    """Held at the end of a day: the sale day is not held, the purchase day is."""
    spans, warnings = held_spans(book, through=date(2024, 12, 31))

    assert spans[aapl.instrument_id] == [
        Span(date(2024, 1, 3), date(2024, 2, 29)),
        Span(date(2024, 6, 3), date(2024, 12, 31)),
    ]
    assert spans[msft.instrument_id] == [Span(date(2024, 2, 1), date(2024, 4, 30))], (
        "held in either account counts, so two overlapping holdings are one span"
    )
    assert warnings == ()


def test_held_spans_stop_at_the_date_asked(book: Repositories, aapl: Instrument) -> None:
    spans, _ = held_spans(book, through=date(2024, 2, 10))

    assert spans[aapl.instrument_id] == [Span(date(2024, 1, 3), date(2024, 2, 10))]


def test_requests_cover_only_held_days_in_the_range_with_a_lead_in(
    book: Repositories, aapl: Instrument, msft: Instrument
) -> None:
    requests, _ = requests_while_held(book, date(2024, 2, 1), date(2024, 6, 30), lead_in_days=5)

    assert [(r.symbol, r.start, r.end, r.lead_in) for r in requests] == [
        ("AAPL", date(2024, 1, 27), date(2024, 1, 31), True),
        ("AAPL", date(2024, 2, 1), date(2024, 2, 29), False),
        ("AAPL", date(2024, 5, 29), date(2024, 6, 2), True),
        ("AAPL", date(2024, 6, 3), date(2024, 6, 30), False),
        ("MSFT", date(2024, 1, 27), date(2024, 1, 31), True),
        ("MSFT", date(2024, 2, 1), date(2024, 4, 30), False),
    ]
    assert all(r.instrument in (aapl, msft) for r in requests)


def test_nothing_held_in_the_range_asks_for_nothing(book: Repositories) -> None:
    requests, _ = requests_while_held(
        book, date(2023, 1, 1), date(2023, 12, 31), lead_in_days=5
    )

    assert requests == []


def test_a_renamed_instrument_is_asked_for_under_each_ticker(
    repos: Repositories, aapl: Instrument
) -> None:
    """portable records the old symbol through the effective date and the new
    one from it, so both cover that day -- and the new one is asked for it."""
    repos.instruments.rename(aapl.instrument_id, "AAPX", date(2024, 5, 1))

    spans = ticker_spans(repos, aapl, Span(date(2024, 4, 1), date(2024, 5, 31)))

    assert spans == [
        ("AAPL", Span(date(2024, 4, 1), date(2024, 4, 30))),
        ("AAPX", Span(date(2024, 5, 1), date(2024, 5, 31))),
    ]
    assert requests_for(repos, [aapl], date(2024, 4, 29), date(2024, 5, 2)) == [
        PriceRequest(aapl, "AAPL", date(2024, 4, 29), date(2024, 4, 30)),
        PriceRequest(aapl, "AAPX", date(2024, 5, 1), date(2024, 5, 2)),
    ]


def test_a_gap_in_portables_own_symbol_history_is_refused(
    repos: Repositories, aapl: Instrument
) -> None:
    """No provider can be asked around a day the portfolio has no ticker for."""
    repos.con.execute(
        "UPDATE instrument_symbol_history SET valid_from = '2024-03-01' "
        "WHERE instrument_id = ?",
        (aapl.instrument_id,),
    )

    with pytest.raises(ValidationError, match="no symbol on record for 2024-02-01"):
        ticker_spans(repos, aapl, Span(date(2024, 2, 1), date(2024, 3, 31)))


def test_an_instrument_is_looked_up_as_of_the_last_day_it_was_held(
    book: Repositories, aapl: Instrument, msft: Instrument
) -> None:
    """`pt instrument sync`: as of today, a ticker sold years ago can name anyone."""
    book.instruments.rename(msft.instrument_id, "MSFX", date(2024, 9, 1))

    lookups, _ = lookups_when_last_held(book, through=date(2024, 12, 31))

    assert [(i.symbol, ticker, on) for i, ticker, on in lookups] == [
        ("AAPL", "AAPL", date(2024, 12, 31)),
        ("MSFX", "MSFT", date(2024, 4, 30)),
    ], "MSFT was sold in April, five months before the ticker changed"


def test_a_comparison_lists_what_the_provider_states_differently() -> None:
    here = Instrument(
        instrument_id=1, symbol="QQQ", instrument_type=InstrumentType.ETF, cusip=None,
        exchange="NASDAQ",
    )  # fmt: skip
    there = Instrument(
        instrument_id=0, symbol="QQQ", instrument_type=InstrumentType.EQUITY,
        cusip="46090E103", exchange=None, name="Invesco QQQ",
    )  # fmt: skip

    assert differences(here, there) == [
        "instrument_type: etf -> equity",
        "name: - -> Invesco QQQ",
        "cusip: - -> 46090E103",
    ], "an empty field there is not a claim; an empty one here is a gap"
