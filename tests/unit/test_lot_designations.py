"""A lot is designated by its lineage, not by its row number. ADR 0036.

Before schema 10 a spec-ID sale recorded lot ids, and a rebuild renumbers lots.
Both reproductions are here: a back-dated buy that turned a designated 500.00
loss into a 500.00 gain, and a reversal of an unrelated buy that tripled it.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portable_core.domain.enums import (
    AccountType,
    DesignationStatus,
    FeeClass,
    InstrumentType,
    ReliefMethod,
    TransactionSource,
    TransactionType,
)
from portable_core.domain.models import (
    Account,
    Instrument,
    LotDesignation,
    TaxRateSchedule,
    Transaction,
)
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_LOT_DESIGNATION_UNRESOLVED
from portable_core.persistence.connection import open_portfolio, transaction
from portable_core.persistence.repositories import Repositories
from portable_core.schema import migrations as M
from portable_core.services import designations
from portable_core.services.lots import (
    derived_lot_key,
    designate,
    format_keyed_selection,
    is_keyed,
    opening_lot_key,
    parse_keyed_selection,
)
from portable_core.services.replay import ReplayEngine
from portable_core.services.trading import TradeIntent, TradingService

pytestmark = pytest.mark.unit

D = Decimal
JAN_02, JAN_05, JAN_10, JAN_20, JAN_25 = (
    date(2024, 1, 2),
    date(2024, 1, 5),
    date(2024, 1, 10),
    date(2024, 1, 20),
    date(2024, 1, 25),
)
FEB_01, FEB_05, MAR_01 = date(2024, 2, 1), date(2024, 2, 5), date(2024, 3, 1)


class Book:
    """A spec-ID taxable account holding one stock, and the ledger helpers."""

    def __init__(self, repos: Repositories) -> None:
        self.repos = repos
        self.account = self._account()
        self.instrument = self._instrument()
        self._append(
            trade_date=JAN_02,
            txn_type=TransactionType.DEPOSIT,
            net_cash_effect=D("100000.00"),
        )

    def _account(self) -> Account:
        account_id = self.repos.accounts.add(
            Account(
                account_id=0,
                name="Taxable",
                account_type=AccountType.TAXABLE,
                opened_date=JAN_02,
                default_relief_method=ReliefMethod.SPEC,
            )
        )
        self.repos.accounts.add_rate_schedule(
            TaxRateSchedule(
                rate_id=0,
                account_id=account_id,
                effective_from=JAN_02,
                short_term_federal=D("0.37"),
                long_term_federal=D("0.20"),
                state=D("0"),
                niit=D("0"),
            )
        )
        account = self.repos.accounts.get(account_id)
        assert account is not None
        return account

    def _instrument(self) -> Instrument:
        instrument_id = self.repos.instruments.add(
            Instrument(instrument_id=0, symbol="XYZ", instrument_type=InstrumentType.EQUITY)
        )
        instrument = self.repos.instruments.get(instrument_id)
        assert instrument is not None
        return instrument

    def _append(self, **fields: object) -> int:
        on = fields["trade_date"]
        assert isinstance(on, date)
        fields.setdefault("account_id", self.account.account_id)
        return self.repos.transactions.append(
            Transaction(txn_id=0, seq=self.repos.transactions.next_seq(on), **fields)  # type: ignore[arg-type]
        )

    def buy(self, on: date, price: str) -> int:
        gross = D(price) * 10
        return self._append(
            trade_date=on,
            txn_type=TransactionType.BUY,
            instrument_id=self.instrument.instrument_id,
            quantity=D("10"),
            price=D(price),
            gross_amount=gross,
            net_cash_effect=-gross,
            fee_class=FeeClass.TRANSACTION_COST,
        )

    def old_sale(self, on: date, lot_ids: str, **extra: object) -> int:
        """A sale as written before schema 10: its designation names lot ids."""
        return self._append(
            trade_date=on,
            txn_type=TransactionType.SELL,
            instrument_id=self.instrument.instrument_id,
            quantity=D("10"),
            price=D("150.00"),
            gross_amount=D("1500.00"),
            net_cash_effect=D("1500.00"),
            relief_method=ReliefMethod.SPEC,
            lot_selection=lot_ids,
            fee_class=FeeClass.TRANSACTION_COST,
            **extra,
        )

    def sell(self, on: date, lot_ids: str) -> int:
        """A sale through the trading service, as `pt sell --lots` makes one."""
        service = TradingService(self.repos)
        plan = service.plan(
            TradeIntent(
                account=self.account,
                instrument=self.instrument,
                txn_type=TransactionType.SELL,
                quantity=D("10"),
                price=D("150.00"),
                trade_date=on,
                fee_class=FeeClass.TRANSACTION_COST,
                relief_method=ReliefMethod.SPEC,
                lot_selection=lot_ids,
            )
        )
        return service.commit(plan).transaction.txn_id

    def reverse(self, on: date, txn_id: int) -> int:
        original = self.repos.transactions.get(txn_id)
        assert original is not None
        return self._append(
            trade_date=on,
            txn_type=TransactionType.REVERSAL,
            instrument_id=original.instrument_id,
            quantity=original.quantity,
            price=original.price,
            gross_amount=-(original.gross_amount or D("0")),
            reverses_txn_id=txn_id,
            net_cash_effect=-original.net_cash_effect,
        )

    def rebuild(self) -> tuple[str, ...]:
        return ReplayEngine(self.repos).rebuild().warnings

    def gain(self, txn_id: int) -> Decimal:
        dispositions = [d for d in self.repos.lots.dispositions() if d.txn_id == txn_id]
        assert dispositions, f"transaction {txn_id} relieved no lot"
        return sum((d.realized_gain for d in dispositions), D("0"))


@pytest.fixture
def book(repos: Repositories) -> Book:
    return Book(repos)


# ── the keys ─────────────────────────────────────────────────────────────────


def test_a_lot_is_keyed_by_the_row_that_opened_it(book: Book) -> None:
    first = book.buy(JAN_10, "100.00")
    book.rebuild()
    (lot,) = book.repos.lots.open_lots(book.account.account_id, book.instrument.instrument_id)
    assert lot.lot_key == opening_lot_key(first) == str(first)


def test_a_derived_lot_is_keyed_by_its_action_and_its_parent() -> None:
    assert derived_lot_key(1500, "1234") == "1500.1234"
    assert derived_lot_key(1600, "1500.1234") == "1600.1500.1234"


def test_a_keyed_designation_round_trips() -> None:
    spec = format_keyed_selection({"1234": D("10"), "1500.1234": D("3")})
    assert spec == "@1234:10;@1500.1234:3"
    assert is_keyed(spec)
    assert parse_keyed_selection(spec) == {"1234": D("10"), "1500.1234": D("3")}
    assert not is_keyed("12:10")


@pytest.mark.parametrize("spec", ["@:10", "@12", "@12:-1", "@12:10;7:3", "@12:x"])
def test_a_malformed_keyed_designation_is_refused(spec: str) -> None:
    with pytest.raises(ValidationError):
        parse_keyed_selection(spec)


# ── new designations survive what used to re-point them ─────────────────────


def test_a_sale_records_its_lots_by_key_not_by_id(book: Book) -> None:
    book.buy(JAN_10, "100.00")
    second = book.buy(JAN_20, "200.00")
    book.rebuild()
    sale = book.sell(FEB_01, "2:10")  # lot 2, as `pt lot list` showed it
    stored = book.repos.transactions.get(sale)
    assert stored is not None
    assert stored.lot_selection == f"@{second}:10"


def test_a_back_dated_buy_no_longer_turns_a_loss_into_a_gain(book: Book) -> None:
    """Reproduction 1. The designated lot cost 2,000.00; the sale is a 500.00 loss."""
    book.buy(JAN_10, "100.00")
    book.buy(JAN_20, "200.00")
    book.rebuild()
    sale = book.sell(FEB_01, "2:10")
    assert book.gain(sale) == D("-500.00")

    book.buy(JAN_05, "50.00")  # back-dated: every later lot id shifts by one
    assert book.rebuild() == ()
    assert book.gain(sale) == D("-500.00")


def test_a_reversal_no_longer_re_points_a_sale(book: Book) -> None:
    """Reproduction 2. Reversing an unrelated buy used to make the loss 1,500.00."""
    first = book.buy(JAN_10, "100.00")
    book.buy(JAN_20, "200.00")
    book.buy(JAN_25, "300.00")
    book.rebuild()
    sale = book.sell(FEB_01, "2:10")
    book.reverse(FEB_05, first)
    assert book.rebuild() == ()
    assert book.gain(sale) == D("-500.00")


def test_designating_a_lot_that_is_not_open_is_refused(book: Book) -> None:
    book.buy(JAN_10, "100.00")
    book.rebuild()
    lots = book.repos.lots.open_lots(book.account.account_id, book.instrument.instrument_id)
    with pytest.raises(ValidationError, match="not an open lot"):
        designate("99:10", lots)
    with pytest.raises(ValidationError, match="not open"):
        designate("@99:10", lots)


# ── old designations ─────────────────────────────────────────────────────────


def test_replay_refuses_an_old_designation_with_no_recorded_meaning(book: Book) -> None:
    book.buy(JAN_10, "100.00")
    book.buy(JAN_20, "200.00")
    sale = book.old_sale(FEB_01, "2:10")
    warnings = book.rebuild()
    assert any(f"txn {sale}" in w and "not recorded" in w for w in warnings)


def test_an_undisturbed_old_designation_is_recorded_as_agreed(book: Book) -> None:
    book.buy(JAN_10, "100.00")
    second = book.buy(JAN_20, "200.00")
    sale = book.old_sale(FEB_01, "2:10")

    recovery = designations.recover(book.repos)
    assert recovery.agreed == 1 and not recovery.pending
    recorded = book.repos.designations.get(sale)
    assert recorded is not None
    assert recorded.status is DesignationStatus.AGREED
    assert recorded.lot_selection == recorded.as_written == recorded.as_read == f"@{second}:10"

    assert book.rebuild() == ()
    assert book.gain(sale) == D("-500.00")


def test_an_old_designation_a_back_dated_buy_re_pointed_is_put_to_the_owner(
    book: Book,
) -> None:
    book.buy(JAN_10, "100.00")
    intended = book.buy(JAN_20, "200.00")
    sale = book.old_sale(FEB_01, "2:10")
    book.buy(JAN_05, "50.00")  # appended after the sale, sorting before it

    recovery = designations.recover(book.repos)
    (row,) = recovery.pending
    assert row.txn_id == sale
    assert row.as_written == f"@{intended}:10"
    assert row.as_read != row.as_written
    assert "renumbered" in (row.note or "")

    # Pending is refused, not guessed.
    warnings = book.rebuild()
    assert any(f"txn {sale}" in w and "not decided" in w for w in warnings)

    gains = designations.gains_by_reading(book.repos)
    assert gains[sale] == {"as_written": D("-500.00"), "as_read": D("500.00")}

    decided = designations.resolve(
        book.repos, sale, reading="as_written", reason="custodian lot report: 2024-01-20"
    )
    assert decided.status is DesignationStatus.AS_WRITTEN
    assert book.rebuild() == ()
    assert book.gain(sale) == D("-500.00")


def test_an_old_designation_a_reversal_re_pointed_is_put_to_the_owner(book: Book) -> None:
    first = book.buy(JAN_10, "100.00")
    intended = book.buy(JAN_20, "200.00")
    book.buy(JAN_25, "300.00")
    sale = book.old_sale(FEB_01, "2:10")
    book.reverse(FEB_05, first)

    (row,) = designations.recover(book.repos).pending
    assert row.as_written == f"@{intended}:10"
    gains = designations.gains_by_reading(book.repos)
    assert gains[sale] == {"as_written": D("-500.00"), "as_read": D("-1500.00")}


def test_an_interleaved_batch_import_cannot_be_established(book: Book) -> None:
    """Its rows applied one by one against a numbering no replay reproduces."""
    book.buy(JAN_10, "100.00")
    book.buy(JAN_20, "200.00")
    book.buy(MAR_01, "250.00")  # already in the file, dated after the import's sale
    sale = book.old_sale(FEB_01, "2:10", source=TransactionSource.IMPORT)

    (row,) = designations.recover(book.repos).pending
    assert row.txn_id == sale
    assert row.as_written is None
    assert "batch import" in (row.note or "")
    with pytest.raises(ValidationError, match="no as written reading"):
        designations.resolve(book.repos, sale, reading="as_written", reason="lot report")
    decided = designations.resolve(book.repos, sale, reading="as_read", reason="lot report")
    assert decided.lot_selection == row.as_read


def test_a_reversed_old_designation_needs_no_meaning(book: Book) -> None:
    book.buy(JAN_10, "100.00")
    sale = book.old_sale(FEB_01, "1:10")
    book.reverse(FEB_05, sale)
    assert designations.unrecorded(book.repos) == []
    assert designations.recover(book.repos).rows == ()


def test_recovery_records_each_row_once(book: Book) -> None:
    book.buy(JAN_10, "100.00")
    book.old_sale(FEB_01, "1:10")
    assert len(designations.recover(book.repos).rows) == 1
    assert designations.recover(book.repos).rows == ()


def test_a_decision_needs_a_reason_and_is_made_once(book: Book) -> None:
    book.buy(JAN_10, "100.00")
    book.buy(JAN_20, "200.00")
    sale = book.old_sale(FEB_01, "2:10")
    book.buy(JAN_05, "50.00")
    designations.recover(book.repos)

    with pytest.raises(ValidationError, match="needs a reason"):
        designations.resolve(book.repos, sale, reading="as_written", reason="  ")
    with pytest.raises(ValidationError, match="unknown reading"):
        designations.resolve(book.repos, sale, reading="agreed", reason="x")
    designations.resolve(book.repos, sale, reading="as_written", reason="lot report")
    with pytest.raises(ValidationError) as again:
        designations.resolve(book.repos, sale, reading="as_read", reason="changed my mind")
    assert again.value.code == E_LOT_DESIGNATION_UNRESOLVED


def test_a_recorded_designation_cannot_be_rewritten_or_deleted(book: Book) -> None:
    book.buy(JAN_10, "100.00")
    sale = book.old_sale(FEB_01, "1:10")
    designations.recover(book.repos)
    with pytest.raises(sqlite3.IntegrityError, match="DESIGNATION-IMMUTABLE"):
        book.repos.con.execute(
            "UPDATE lot_designation SET lot_selection = '@9:10' WHERE txn_id = ?", (sale,)
        )
    with pytest.raises(sqlite3.IntegrityError, match="DESIGNATION-IMMUTABLE"):
        book.repos.con.execute("DELETE FROM lot_designation WHERE txn_id = ?", (sale,))


def test_a_pending_row_must_say_why_and_a_decided_one_who(repos: Repositories) -> None:
    """The schema's own checks, behind the service's."""
    book = Book(repos)
    sale = book.old_sale(FEB_01, "1:10")
    with pytest.raises(sqlite3.IntegrityError):
        repos.designations.record(
            LotDesignation(
                txn_id=sale,
                status=DesignationStatus.AS_WRITTEN,
                as_written="@1:10",
                lot_selection="@1:10",
            )
        )


# ── the migration ────────────────────────────────────────────────────────────


def test_a_schema_9_file_keeps_what_its_designations_meant(tmp_path: Path) -> None:
    """The owner's path: a file written before schema 10, migrated and recovered."""
    path = tmp_path / "old.port"
    con = open_portfolio(path, must_exist=False)
    try:
        for migration in M.available_migrations():
            if migration.version <= 9:
                M._apply(con, migration)
        assert M.schema_version(con) == 9
        repos = Repositories(con)
        with transaction(con):
            for key, value in {
                "portfolio_name": "old",
                "inception_date": "2024-01-02",
                "base_currency": "USD",
                "fiscal_year_end": "12-31",
                "schema_version": "9",
            }.items():
                repos.meta.set(key, value)
            book = Book(repos)
            book.buy(JAN_10, "100.00")
            intended = book.buy(JAN_20, "200.00")
            sale = book.old_sale(FEB_01, "2:10")

        M.migrate(con, path, backup=False)
        assert M.schema_version(con) == M.CURRENT_SCHEMA_VERSION
        with transaction(con):
            recovery = designations.recover(repos)
            outcome = ReplayEngine(repos).rebuild()
        assert recovery.agreed == 1
        assert outcome.warnings == ()
        recorded = repos.designations.get(sale)
        assert recorded is not None and recorded.lot_selection == f"@{intended}:10"
        assert book.gain(sale) == D("-500.00")
    finally:
        con.close()
