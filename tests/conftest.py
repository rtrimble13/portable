"""Shared fixtures."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portable_core.domain.enums import (
    AccountType,
    InstrumentType,
    ReliefMethod,
    TransactionType,
)
from portable_core.domain.models import Account, Instrument, TaxRateSchedule, Transaction
from portable_core.persistence.connection import open_portfolio
from portable_core.persistence.repositories import Repositories
from portable_core.schema import migrations as M

D = Decimal


@pytest.fixture
def portfolio_path(tmp_path: Path) -> Path:
    return tmp_path / "test.port"


@pytest.fixture
def con(portfolio_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_portfolio(portfolio_path, must_exist=False)
    M.initialise(connection)
    connection.execute(
        "INSERT INTO meta (key, value) VALUES ('portfolio_name', 'Test'), "
        "('inception_date', '2024-01-02'), ('base_currency', 'USD'), "
        "('fiscal_year_end', '12-31'), ('schema_version', '1')"
    )
    yield connection
    connection.close()


@pytest.fixture
def repos(con: sqlite3.Connection) -> Repositories:
    return Repositories(con)


@pytest.fixture
def taxable_account(repos: Repositories) -> Account:
    """One taxable account with a rate schedule in force from inception."""
    account_id = repos.accounts.add(
        Account(
            account_id=0,
            name="Brokerage",
            account_type=AccountType.TAXABLE,
            opened_date=date(2024, 1, 2),
            custodian="Example Broker",
            default_relief_method=ReliefMethod.FIFO,
        )
    )
    repos.accounts.add_rate_schedule(
        TaxRateSchedule(
            rate_id=0,
            account_id=account_id,
            effective_from=date(2024, 1, 1),
            short_term_federal=D("0.37"),
            long_term_federal=D("0.20"),
            state=D("0.05"),
            niit=D("0.038"),
        )
    )
    account = repos.accounts.get(account_id)
    assert account is not None
    return account


@pytest.fixture
def ira_account(repos: Repositories) -> Account:
    account_id = repos.accounts.add(
        Account(
            account_id=0,
            name="IRA",
            account_type=AccountType.TAX_DEFERRED,
            opened_date=date(2024, 1, 2),
        )
    )
    account = repos.accounts.get(account_id)
    assert account is not None
    return account


@pytest.fixture
def aapl(repos: Repositories) -> Instrument:
    instrument_id = repos.instruments.add(
        Instrument(
            instrument_id=0,
            symbol="AAPL",
            instrument_type=InstrumentType.EQUITY,
            name="Apple Inc.",
            exchange="NASDAQ",
            sector="Technology",
        )
    )
    instrument = repos.instruments.get(instrument_id)
    assert instrument is not None
    return instrument


def append(
    repos: Repositories,
    account_id: int,
    txn_type: TransactionType,
    trade_date: date,
    **fields: object,
) -> int:
    """Append a ledger row, assigning the sequence number as `pt` would."""
    return repos.transactions.append(
        Transaction(
            txn_id=0,
            account_id=account_id,
            trade_date=trade_date,
            seq=repos.transactions.next_seq(trade_date),
            txn_type=txn_type,
            net_cash_effect=fields.pop("net_cash_effect", D("0.00")),  # type: ignore[arg-type]
            **fields,  # type: ignore[arg-type]
        )
    )
