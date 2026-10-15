"""Stored valuation snapshots: written to the right row, and checkable. ADR 0024."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from portable_core.domain.enums import TransactionType
from portable_core.domain.models import Account, Instrument, Price, ReturnPolicy
from portable_core.persistence.repositories import RemovedSnapshots, Repositories
from portable_core.services import snapshots
from portable_core.services.replay import ReplayEngine
from tests.conftest import append

D = Decimal
ON = date(2024, 1, 3)


def _price(instrument: Instrument, value: str, *, on: date = ON) -> Price:
    return Price(
        instrument_id=instrument.instrument_id,
        price_date=on,
        price=D(value),
        source="manual",
        as_of=datetime(2024, 1, 3, 21, tzinfo=UTC),
    )


def _value(repos: Repositories, accounts: list[Account], first: date, last: date) -> None:
    built, warnings = snapshots.value_range(
        repos, accounts, first, last, staleness_tolerance_days=5
    )
    assert warnings == []
    for snapshot in built:
        repos.valuations.save_snapshot(snapshot)


def _funded(repos: Repositories, taxable: Account, ira: Account, aapl: Instrument) -> None:
    """Both accounts take a deposit on ON, and the taxable one buys AAPL."""
    for account in (taxable, ira):
        append(
            repos, account.account_id, TransactionType.DEPOSIT, ON, net_cash_effect=D("1000.00")
        )
    append(repos, taxable.account_id, TransactionType.BUY, ON, instrument_id=aapl.instrument_id,
           quantity=D("2"), price=D("100.00"), gross_amount=D("200.00"),
           net_cash_effect=D("-200.00"))  # fmt: skip
    ReplayEngine(repos).rebuild()
    repos.prices.add(_price(aapl, "101.00"))


def test_valuing_a_day_again_keeps_each_price_set_on_its_own_snapshot(
    repos: Repositories, taxable_account: Account, ira_account: Account, aapl: Instrument
) -> None:
    """`save_snapshot` took `lastrowid` after an upsert.

    On the update path SQLite leaves it at the last row inserted on the
    connection -- the IRA's flow row, from the first valuation -- so the
    taxable account's new price set went to the IRA's snapshot, the IRA's save
    then wrote over it, and the taxable snapshot kept the price it had before:
    a snapshot whose figures and recorded prices disagree.
    """
    _funded(repos, taxable_account, ira_account, aapl)
    accounts = [taxable_account, ira_account]
    _value(repos, accounts, ON, ON)
    repos.prices.add(_price(aapl, "101.25"))

    _value(repos, accounts, ON, ON)

    taxable = repos.valuations.snapshot(taxable_account.account_id, ON)
    ira = repos.valuations.snapshot(ira_account.account_id, ON)
    assert taxable is not None and ira is not None
    assert taxable["securities_value"] == "202.50"
    assert [p.price for p in repos.valuations.recorded_prices(taxable["snapshot_id"])] == [
        D("101.25")
    ]
    assert repos.valuations.recorded_prices(ira["snapshot_id"]) == []
    assert len(repos.valuations.record(taxable["snapshot_id"])["flows"]) == 1
    assert len(repos.valuations.record(ira["snapshot_id"])["flows"]) == 1
    assert snapshots.verify(repos) == []


def test_replacing_a_price_returns_its_own_id(repos: Repositories, aapl: Instrument) -> None:
    """The same `lastrowid` defect: `pt price set` reported another row's id."""
    first = repos.prices.add(_price(aapl, "101.00"))
    other = repos.prices.add(_price(aapl, "102.00", on=date(2024, 1, 4)))

    replaced = repos.prices.add(_price(aapl, "101.50"))

    assert replaced == first != other


def test_verify_names_a_price_its_snapshot_no_longer_agrees_with(
    repos: Repositories, taxable_account: Account, ira_account: Account, aapl: Instrument
) -> None:
    _funded(repos, taxable_account, ira_account, aapl)
    _value(repos, [taxable_account, ira_account], ON, ON)

    repos.prices.add(_price(aapl, "101.25"))

    assert snapshots.verify(repos) == [
        "Brokerage 2024-01-03: recorded AAPL at 101.00 at level 1, and its price row now "
        "says 101.25 at level 1 -- value it again with `pt value --date 2024-01-03`, or a "
        "range that covers it"
    ]


def test_verify_names_the_field_a_changed_ledger_moves(
    repos: Repositories, taxable_account: Account, ira_account: Account, aapl: Instrument
) -> None:
    """Appended behind the repository's back, so nothing removed the snapshot."""
    _funded(repos, taxable_account, ira_account, aapl)
    _value(repos, [taxable_account, ira_account], ON, ON)
    repos.con.execute(
        "UPDATE valuation_snapshot SET cash_balance = '0.00' WHERE account_id = ?",
        (ira_account.account_id,),
    )

    [problem] = snapshots.verify(repos)

    assert problem.startswith("IRA 2024-01-03: re-derived from the ledger with the prices it ")
    assert "its cash_balance differs from what is stored" in problem


def test_removal_notices_merge_per_account_and_say_how_to_restore() -> None:
    notices = snapshots.removal_notices(
        [
            RemovedSnapshots("B", date(2024, 1, 4), date(2024, 1, 5), 2),
            RemovedSnapshots("IRA", date(2024, 1, 5), date(2024, 1, 5), 1),
            RemovedSnapshots("B", date(2024, 1, 3), date(2024, 1, 3), 1),
        ],
        dry_run=False,
    )

    assert notices == [
        "removed 3 valuation snapshots of B (2024-01-03 to 2024-01-05): this entry changes "
        'them. Value them again with `pt value --from 2024-01-03 --to 2024-01-05 -a "B"`.',
        "removed 1 valuation snapshot of IRA (2024-01-05): this entry changes them. Value "
        'them again with `pt value --from 2024-01-05 --to 2024-01-05 -a "IRA"`.',
    ]


def test_port_gips_b03_a_policy_dated_before_a_snapshot_names_it(
    repos: Repositories, taxable_account: Account, ira_account: Account, aapl: Instrument
) -> None:
    """A large-flow threshold restates the flows of every snapshot it predates.

    ADR 0010 says a policy row dated before derived state is refused, and
    nothing refuses one. For snapshots, `pt validate` now names what it restates.
    """
    _funded(repos, taxable_account, ira_account, aapl)
    _value(repos, [taxable_account, ira_account], ON, ON)

    repos.policies.add(
        ReturnPolicy(
            policy_id=0,
            effective_from=date(2024, 1, 1),
            large_flow_basis="amount",
            large_flow_value=D("500.00"),
        )
    )

    problems = snapshots.verify(repos)
    assert len(problems) == 2, problems
    assert all("its flows differs from what is stored" in p for p in problems), problems


def test_a_rehearsal_says_what_it_would_remove() -> None:
    """`pt import batch --dry-run` appends for real and rolls the work back."""
    [notice] = snapshots.removal_notices(
        [RemovedSnapshots("B", date(2024, 1, 4), date(2024, 1, 4), 1)], dry_run=True
    )

    assert notice.startswith("would remove 1 valuation snapshot of B (2024-01-04)")
