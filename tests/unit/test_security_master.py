"""Correcting a symbol the security master recorded in error. ADR 0034.

The case that forced it: a crosswalk mapped a fund's description to another
share class's ticker. Quantities reconciled, because units are units; every
price fetched under that ticker would have been the other class's, with
nothing to refuse it. The custodian's statements named the right ticker every
month the fund was held.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from portable_core.domain.enums import InstrumentType
from portable_core.domain.models import Instrument, Price
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_INSTRUMENT_AMBIGUOUS
from portable_core.persistence.repositories import Repositories
from portable_core.services.security_master import correct_symbol

pytestmark = pytest.mark.unit

START = date(1900, 1, 1)
REASON = "the custodian's statements name ARIMX for this fund in every month it was held"


def _fund(repos: Repositories, symbol: str) -> Instrument:
    instrument_id = repos.instruments.add(
        Instrument(
            instrument_id=0,
            symbol=symbol,
            instrument_type=InstrumentType.MUTUAL_FUND,
            name="AMG RIVER ROAD SMALL MID CAP VALUE I",
        )
    )
    instrument = repos.instruments.get(instrument_id)
    assert instrument is not None
    return instrument


def _correct(repos: Repositories, instrument: Instrument, old: str, new: str) -> None:
    correct_symbol(repos, instrument, new, old_symbol=old, reason=REASON)


def test_the_corrected_symbol_resolves_and_the_wrong_one_no_longer_does(
    repos: Repositories,
) -> None:
    fund = _fund(repos, "ARSMX")
    _correct(repos, fund, "ARSMX", "ARIMX")

    assert repos.instruments.resolve("ARIMX").instrument_id == fund.instrument_id
    assert repos.instruments.resolve("ARIMX", on=date(2023, 1, 31)).instrument_id == (
        fund.instrument_id
    )
    assert repos.instruments.find("ARSMX", on=date(2023, 1, 31)) is None


def test_a_correction_opens_no_period_and_writes_no_ledger_row(
    repos: Repositories, con: sqlite3.Connection
) -> None:
    """Not a rename: nothing happened to the security, so the history still
    has one period -- now carrying the right symbol -- and the ledger, which
    records events, records none."""
    fund = _fund(repos, "ARSMX")
    _correct(repos, fund, "ARSMX", "ARIMX")

    assert repos.instruments.symbol_history(fund.instrument_id) == [("ARIMX", START, None)]
    assert con.execute('SELECT COUNT(*) FROM "transaction"').fetchone()[0] == 0


def test_the_change_log_records_what_changed_and_why(
    repos: Repositories, con: sqlite3.Connection
) -> None:
    fund = _fund(repos, "ARSMX")
    _correct(repos, fund, "ARSMX", "ARIMX")

    row = con.execute(
        "SELECT key, old_value, new_value, effective_from, reason FROM meta_change_log"
    ).fetchone()
    assert tuple(row) == (
        f"instrument:{fund.instrument_id}:symbol",
        "ARSMX",
        "ARIMX",
        START.isoformat(),
        REASON,
    )


def test_a_correction_then_a_rename_records_a_ticker_the_master_never_had(
    repos: Repositories,
) -> None:
    """The master named a renamed company by its new ticker for all of
    history. Correct it to the old one, then record the rename as the event it
    was: each day now resolves to the ticker the security carried that day."""
    company = _fund(repos, "P")
    _correct(repos, company, "P", "PSTG")
    repos.instruments.rename(company.instrument_id, "P", date(2026, 4, 20))

    assert repos.instruments.symbol_history(company.instrument_id) == [
        ("PSTG", START, date(2026, 4, 20)),
        ("P", date(2026, 4, 20), None),
    ]
    assert repos.instruments.resolve("PSTG", on=date(2025, 6, 30)).instrument_id == (
        company.instrument_id
    )


def test_only_the_periods_that_carried_the_wrong_symbol_are_corrected(
    repos: Repositories,
) -> None:
    fund = _fund(repos, "WSMRX")
    repos.instruments.rename(fund.instrument_id, "NEWER", date(2025, 1, 2))
    _correct(repos, fund, "WSMRX", "WBCRX")

    assert repos.instruments.symbol_history(fund.instrument_id) == [
        ("WBCRX", START, date(2025, 1, 2)),
        ("NEWER", date(2025, 1, 2), None),
    ]
    assert repos.instruments.get(fund.instrument_id).symbol == "NEWER"  # type: ignore[union-attr]


def test_a_correction_needs_a_reason(repos: Repositories) -> None:
    fund = _fund(repos, "ARSMX")
    with pytest.raises(ValidationError, match="--reason"):
        correct_symbol(repos, fund, "ARIMX", old_symbol="ARSMX", reason="  ")


def test_a_symbol_the_instrument_never_carried_is_refused(repos: Repositories) -> None:
    fund = _fund(repos, "ARSMX")
    with pytest.raises(ValidationError, match="never carried"):
        correct_symbol(repos, fund, "ARIMX", old_symbol="FZAMX", reason=REASON)


def test_correcting_a_symbol_to_itself_is_refused(repos: Repositories) -> None:
    fund = _fund(repos, "ARSMX")
    with pytest.raises(ValidationError, match="to itself"):
        correct_symbol(repos, fund, "arsmx", old_symbol="ARSMX", reason=REASON)


def test_a_symbol_another_instrument_carries_over_the_same_days_is_refused(
    repos: Repositories,
) -> None:
    """Resolution by it would answer with two instruments, and a quantity
    would go against whichever came first."""
    fund = _fund(repos, "ARSMX")
    other = _fund(repos, "ARIMX")
    with pytest.raises(ValidationError) as caught:
        _correct(repos, fund, "ARSMX", "ARIMX")
    assert caught.value.code == E_INSTRUMENT_AMBIGUOUS
    assert repos.instruments.get(fund.instrument_id).symbol == "ARSMX"  # type: ignore[union-attr]
    assert other.instrument_id in caught.value.context["matches"]


def test_a_ticker_another_instrument_carried_on_other_days_is_not_a_clash(
    repos: Repositories,
) -> None:
    """Tickers are reused. One that named something else before this
    instrument's period began is not ambiguous on any day."""
    fund = _fund(repos, "ARSMX")
    repos.instruments.rename(fund.instrument_id, "LATER", date(2024, 1, 2))
    other = _fund(repos, "OLDCO")
    repos.instruments.rename(other.instrument_id, "ARIMX", date(2024, 6, 3))

    _correct(repos, fund, "ARSMX", "ARIMX")

    assert repos.instruments.symbol_history(fund.instrument_id)[0] == (
        "ARIMX",
        START,
        date(2024, 1, 2),
    )


def test_an_instrument_with_stored_prices_is_refused(repos: Repositories) -> None:
    """Each price reached it by the wrong symbol and may be another
    security's. Correct first, then price."""
    fund = _fund(repos, "FZAMX")
    repos.prices.add(
        Price(
            instrument_id=fund.instrument_id,
            price_date=date(2022, 12, 30),
            price=Decimal("10.00"),
            source="fafnir:duk ph",
            as_of=datetime(2022, 12, 30, tzinfo=UTC),
        )
    )
    with pytest.raises(ValidationError, match="stored price"):
        correct_symbol(repos, fund, "FIDFX", old_symbol="FZAMX", reason=REASON)
    assert repos.instruments.get(fund.instrument_id).symbol == "FZAMX"  # type: ignore[union-attr]
