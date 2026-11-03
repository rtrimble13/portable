"""The clock every persisted stamp is read from (ADR 0035, `CLAUDE.md` invariant 6)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from portable_core.clock import SYSTEM_CLOCK, FixedClock, SystemClock
from portable_core.domain.enums import AccountType, InstrumentType, ReliefMethod
from portable_core.domain.models import Account, Instrument, ReturnPolicy
from portable_core.persistence.connection import open_portfolio
from portable_core.persistence.repositories import Repositories, _Repository
from portable_core.schema import migrations as M

pytestmark = pytest.mark.unit

MOMENT = datetime(2026, 8, 27, tzinfo=UTC)


def test_a_fixed_clock_reads_its_moment_every_time() -> None:
    clock = FixedClock(MOMENT)
    assert clock.now() == MOMENT
    assert clock.stamp() == clock.stamp() == "2026-08-27T00:00:00Z"


def test_a_stamp_is_utc_whatever_zone_the_moment_was_given_in() -> None:
    """Stored stamps say ``Z``, so a moment in another zone is converted, not relabelled."""
    eastern = timezone(timedelta(hours=-4))
    assert FixedClock(datetime(2026, 8, 26, 20, 0, 0, tzinfo=eastern)).stamp() == (
        "2026-08-27T00:00:00Z"
    )


def test_a_stamp_drops_fractions_of_a_second() -> None:
    assert FixedClock(MOMENT.replace(microsecond=999_999)).stamp() == "2026-08-27T00:00:00Z"


def test_a_naive_moment_is_refused() -> None:
    """Converting it would assume the machine's zone: a stamp that depends on where it ran."""
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(datetime(2026, 8, 27))


def test_the_system_clock_reads_the_wall_clock_in_utc() -> None:
    before = datetime.now(UTC).replace(microsecond=0)
    reading = SystemClock().now()
    assert reading.utcoffset() == timedelta(0)
    assert before <= reading <= datetime.now(UTC)
    assert isinstance(SYSTEM_CLOCK, SystemClock)


def test_every_repository_in_the_bundle_holds_the_bundles_clock(
    con: sqlite3.Connection,
) -> None:
    """A repository added to the bundle without the clock would stamp from the wall clock."""
    clock = FixedClock(MOMENT)
    repos = Repositories(con, clock=clock)
    members = [value for value in vars(repos).values() if isinstance(value, _Repository)]
    assert len(members) >= 12
    assert all(member.clock is clock for member in members)


def test_the_bundle_defaults_to_the_system_clock(con: sqlite3.Connection) -> None:
    assert Repositories(con).clock is SYSTEM_CLOCK


def test_rows_written_through_the_bundle_carry_its_clocks_stamp(
    con: sqlite3.Connection,
) -> None:
    """Config and reference rows, not only the ledger: the gap ADR 0035 closed."""
    repos = Repositories(con, clock=FixedClock(MOMENT))
    account_id = repos.accounts.add(
        Account(
            account_id=0,
            name="Brokerage",
            account_type=AccountType.TAXABLE,
            opened_date=date(2024, 1, 2),
            default_relief_method=ReliefMethod.FIFO,
        )
    )
    repos.instruments.add(
        Instrument(instrument_id=0, symbol="VTI", instrument_type=InstrumentType.ETF)
    )
    repos.policies.add(
        ReturnPolicy(
            policy_id=0,
            effective_from=date(2024, 1, 2),
            large_flow_basis="percent",
            large_flow_value=Decimal("0.10"),
        )
    )
    repos.accounts.update(account_id, note="renamed")

    stamps = {
        *con.execute("SELECT created_at, updated_at FROM account").fetchone(),
        *con.execute("SELECT created_at, updated_at FROM instrument").fetchone(),
        *con.execute("SELECT created_at FROM return_policy").fetchone(),
    }
    assert stamps == {"2026-08-27T00:00:00Z"}


def test_each_migration_is_recorded_at_the_clocks_moment(tmp_path: Path) -> None:
    connection = open_portfolio(tmp_path / "fresh.port", must_exist=False)
    try:
        M.initialise(connection, clock=FixedClock(MOMENT))
        applied = {
            row[0] for row in connection.execute("SELECT applied_at FROM schema_migration")
        }
    finally:
        connection.close()
    assert applied == {"2026-08-27T00:00:00Z"}
