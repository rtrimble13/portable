"""Spec-ID designations through the real CLI. ADR 0036.

A sale records its lots by lineage key, so neither a back-dated buy nor a
reversal moves it onto another lot. A sale recorded before schema 10 is put to
the owner by `pt migrate` when its meaning drifted, and decided once with
`pt lot designation resolve`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from portable_core.lint._common import repo_root
from tests.integration.conftest import CliRunner

pytestmark = pytest.mark.integration

D = Decimal

#: Writes a schema-9 file whose spec-ID sale (txn 4, "2:10") a back-dated buy,
#: appended after it, re-pointed. Run in its own interpreter: the file must be
#: written by the code paths a schema-9 file was written by, before migration.
_SCHEMA_9 = """
import sys
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from portable_core.domain.enums import (
    AccountType, FeeClass, InstrumentType, ReliefMethod, TransactionType,
)
from portable_core.domain.models import Account, Instrument, TaxRateSchedule, Transaction
from portable_core.persistence.connection import open_portfolio, transaction
from portable_core.persistence.repositories import Repositories
from portable_core.schema import migrations as M

con = open_portfolio(Path(sys.argv[1]), must_exist=False)
for m in M.available_migrations():
    if m.version <= 9:
        M._apply(con, m)
repos = Repositories(con)
def txn(**f):
    return repos.transactions.append(
        Transaction(txn_id=0, seq=repos.transactions.next_seq(f["trade_date"]), **f)
    )
with transaction(con):
    for k, v in {
        "portfolio_name": "old", "inception_date": "2024-01-01", "base_currency": "USD",
        "fiscal_year_end": "12-31", "schema_version": "9",
        "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z",
        "portable_version": "0.1.0",
    }.items():
        repos.meta.set(k, v)
    a = repos.accounts.add(Account(
        account_id=0, name="B", account_type=AccountType.TAXABLE,
        opened_date=date(2024, 1, 1), default_relief_method=ReliefMethod.SPEC,
    ))
    repos.accounts.add_rate_schedule(TaxRateSchedule(
        rate_id=0, account_id=a, effective_from=date(2024, 1, 1),
        short_term_federal=D("0.37"), long_term_federal=D("0.2"), state=D("0"), niit=D("0"),
    ))
    x = repos.instruments.add(
        Instrument(instrument_id=0, symbol="XYZ", instrument_type=InstrumentType.EQUITY)
    )
    txn(account_id=a, trade_date=date(2024, 1, 2), txn_type=TransactionType.DEPOSIT,
        net_cash_effect=D("10000.00"))
    def buy(on, px):
        g = D(px) * 10
        return txn(account_id=a, trade_date=on, txn_type=TransactionType.BUY,
                   instrument_id=x, quantity=D("10"), price=D(px), gross_amount=g,
                   net_cash_effect=-g, fee_class=FeeClass.TRANSACTION_COST)
    buy(date(2024, 1, 10), "100.00")
    buy(date(2024, 1, 20), "200.00")
    txn(account_id=a, trade_date=date(2024, 2, 1), txn_type=TransactionType.SELL,
        instrument_id=x, quantity=D("10"), price=D("150.00"), gross_amount=D("1500.00"),
        net_cash_effect=D("1500.00"), relief_method=ReliefMethod.SPEC,
        lot_selection="2:10", fee_class=FeeClass.TRANSACTION_COST)
    buy(date(2024, 1, 5), "50.00")
con.close()
"""


def _trade(
    run_pt: CliRunner, port: str, verb: str, on: str, price: str, *extra: str
) -> dict[str, Any]:
    return run_pt(
        "--port", port, verb, "XYZ", "--qty", "10", "--price", price,
        "--date", on, "-a", "B", *extra,
    ).data  # fmt: skip


def _short_term(run_pt: CliRunner, port: str) -> Decimal:
    return D(run_pt("--port", port, "tax", "--year", "2024").data["short_term_gain"])


def test_a_designated_sale_survives_a_back_dated_buy_and_a_reversal(
    run_pt: CliRunner, portfolio: Path
) -> None:
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    first = _trade(run_pt, port, "buy", "2024-01-10", "100.00")
    _trade(run_pt, port, "buy", "2024-01-20", "200.00")
    _trade(run_pt, port, "buy", "2024-01-25", "300.00")

    lots = run_pt("--port", port, "lot", "list", "XYZ", "--as-of", "2024-01-31").data["rows"]
    intended = next(row for row in lots if row["open_date"] == "2024-01-20")
    sold = _trade(
        # More than 30 days after the last buy, so no wash sale (ADR 0037)
        # disallows the loss whose amount identifies the lot relieved.
        run_pt, port, "sell", "2024-02-26", "150.00",
        "--method", "spec", "--lots", f"{intended['lot_id']}:10",
    )  # fmt: skip
    shown = run_pt("--port", port, "trade", "show", str(sold["txn_id"])).data
    assert shown["lot_selection"] == f"@{intended['lot_key']}:10"
    assert _short_term(run_pt, port) == D("-500.00")

    _trade(run_pt, port, "buy", "2024-01-05", "50.00")  # back-dated: renumbers every lot
    assert _short_term(run_pt, port) == D("-500.00")
    run_pt("--port", port, "trade", "reverse", str(first["txn_id"]), "--date", "2024-03-01")
    assert _short_term(run_pt, port) == D("-500.00")
    run_pt("--port", port, "validate")


def test_spun_lots_are_keyed_by_the_spinoff_and_validate(
    run_pt: CliRunner, portfolio: Path
) -> None:
    """`pt ca spinoff` derives lots itself; they must be the lots replay derives."""
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    bought = _trade(run_pt, port, "buy", "2024-01-10", "100.00")
    run_pt(
        "--port", port, "ca", "spinoff", "XYZ", "--spun", "NEWCO", "--ratio", "0.5",
        "--parent-fmv", "90", "--spun-fmv", "20", "--ex-date", "2024-03-01",
    )  # fmt: skip
    spun = run_pt("--port", port, "lot", "list", "NEWCO", "--as-of", "2024-03-31").data["rows"]
    (lot,) = spun
    spinoff_txn, parent = lot["lot_key"].split(".", 1)
    assert parent == str(bought["txn_id"])
    assert int(spinoff_txn) > bought["txn_id"]
    run_pt("--port", port, "validate")


def test_pt_migrate_puts_a_drifted_sale_to_the_owner(run_pt: CliRunner, tmp_path: Path) -> None:
    path = tmp_path / "old.port"
    subprocess.run(
        [sys.executable, "-c", _SCHEMA_9, str(path)],
        check=True,
        cwd=repo_root(),
        env={**os.environ, "PYTHONPATH": str(repo_root() / "src")},
    )
    port = str(path)

    migrated = run_pt("--port", port, "migrate", "--no-backup").data
    assert migrated["to_version"] >= 10
    (pending,) = migrated["designations"]["pending"]
    assert pending["txn_id"] == 4 and pending["written"] == "2:10"
    assert pending["as_written"] == "@3:10"  # the 2024-01-20 buy, txn 3

    run_pt("--port", port, "validate", expect=4)
    listed = run_pt("--port", port, "lot", "designation", "list", "--pending").data
    (row,) = listed["designations"]
    assert D(row["gain_as_written"]) == D("-500.00")
    assert D(row["gain_as_read"]) == D("500.00")

    run_pt("--port", port, "lot", "designation", "resolve", "4", "--as-written", expect=4)
    resolved = run_pt(
        "--port", port, "lot", "designation", "resolve", "4", "--as-written",
        "--reason", "custodian lot report: the 2024-01-20 lot",
    ).data  # fmt: skip
    assert D(resolved["realized_gain"]) == D("-500.00")
    run_pt("--port", port, "validate")
    run_pt(
        "--port", port, "lot", "designation", "resolve", "4", "--as-read",
        "--reason", "second thoughts",
        expect=4,
    )  # fmt: skip
