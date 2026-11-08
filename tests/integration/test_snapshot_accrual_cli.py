"""What a valuation snapshot records for the return engine. ADR 0040 §13, through the CLI.

Four things the snapshot left out before:

* **A dividend between its ex-date and its pay-date** was not in value. Its
  price fell on the ex-date and the cash came on the pay-date, so a period
  boundary between the two moved return from one period to the next
  (``PORT-GIPS-A06``).
* **Entering that dividend** removed only the snapshots from its pay-date,
  leaving the ones that should have accrued it standing.
* **The day's income and fees** were always ``0.00``. A zero that means "not
  computed" reads as a zero.
* **Only account-level flows were stored.** A portfolio return weights each
  flow by its own sign, which a day's sum cannot give back.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal as D
from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner

pytestmark = pytest.mark.integration


@pytest.fixture
def holding(run_pt: CliRunner, portfolio: Path) -> str:
    """100 XYZ bought on 2024-02-01 with a 5.00 commission, priced through 02-12."""
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    run_pt("--port", port, "buy", "XYZ", "--qty", "100", "--price", "50", "--date",
           "2024-02-01", "-a", "B", "--commission", "5",
           "--fee-class", "transaction_cost")  # fmt: skip
    for on, px in (("2024-02-01", "50"), ("2024-02-05", "49"), ("2024-02-09", "49")):
        run_pt("--port", port, "price", "set", "XYZ", "--price", px, "--date", on,
               "--valuation-level", "1")  # fmt: skip
    return port


def _dividend(run_pt: CliRunner, port: str, *extra: str) -> None:
    run_pt("--port", port, "income", "dividend", "XYZ", "-a", "B", "--amount", "100",
           "--ex-date", "2024-02-05", "--pay-date", "2024-02-09", *extra)  # fmt: skip


def _value(run_pt: CliRunner, port: str) -> None:
    run_pt("--port", port, "value", "--from", "2024-02-01", "--to", "2024-02-12", "-a", "B")


def _column(port: str, column: str) -> dict[str, D]:
    con = sqlite3.connect(port)
    try:
        return {
            str(on): D(str(value))
            for on, value in con.execute(
                f"SELECT snapshot_date, {column} FROM valuation_snapshot ORDER BY snapshot_date"
            )
        }
    finally:
        con.close()


@pytest.mark.gips
def test_dividend_accrues_on_ex_date(run_pt: CliRunner, holding: str) -> None:
    """PORT-GIPS-A06: entitled on the ex-date, a receivable until the pay-date."""
    _dividend(run_pt, holding)
    _value(run_pt, holding)

    accrued = _column(holding, "accrued_dividends")
    assert accrued["2024-02-04"] == D("0.00")
    assert all(accrued[f"2024-02-0{day}"] == D("100.00") for day in (5, 6, 7, 8))
    assert accrued["2024-02-09"] == D("0.00"), "paid: the receivable is now cash"

    # The price fell by the dividend on the ex-date and the receivable makes it
    # up; the cash arriving on the pay-date takes the receivable's place.
    ending = _column(holding, "ending_market_value")
    assert ending["2024-02-08"] == ending["2024-02-09"]
    assert _column(holding, "accrued_income")["2024-02-06"] == D("100.00")


def test_tax_withheld_is_not_accrued(run_pt: CliRunner, holding: str) -> None:
    """What will arrive is accrued; withholding never arrives."""
    _dividend(run_pt, holding, "--withheld", "15")
    _value(run_pt, holding)

    assert _column(holding, "accrued_dividends")["2024-02-06"] == D("85.00")


def test_a_reversed_dividend_accrues_nothing(run_pt: CliRunner, holding: str) -> None:
    _dividend(run_pt, holding)
    con = sqlite3.connect(holding)
    try:
        txn_id = con.execute(
            "SELECT txn_id FROM \"transaction\" WHERE txn_type = 'dividend'"
        ).fetchone()[0]
    finally:
        con.close()
    run_pt("--port", holding, "trade", "reverse", str(txn_id), "--date", "2024-02-10")
    _value(run_pt, holding)

    assert set(_column(holding, "accrued_dividends").values()) == {D("0.00")}


def test_entering_a_dividend_removes_snapshots_from_its_ex_date(
    run_pt: CliRunner, holding: str
) -> None:
    """Before: only those from the pay-date went, and 02-05 to 02-08 stood
    without the receivable they should have carried."""
    _value(run_pt, holding)
    result = run_pt("--port", holding, "income", "dividend", "XYZ", "-a", "B",
                    "--amount", "100", "--ex-date", "2024-02-05",
                    "--pay-date", "2024-02-09")  # fmt: skip

    assert sorted(_column(holding, "accrued_dividends")) == [
        "2024-02-01", "2024-02-02", "2024-02-03", "2024-02-04"
    ]  # fmt: skip
    assert any("2024-02-05" in warning for warning in result.json()["warnings"])


def test_the_days_income_and_fees_are_recorded(run_pt: CliRunner, holding: str) -> None:
    """Informational, so that a stored zero means zero (ADR 0040 §13.3)."""
    _dividend(run_pt, holding, "--withheld", "15")
    run_pt("--port", holding, "cash", "fee", "-a", "B", "--amount", "25",
           "--fee-class", "internal_mgmt_cost", "--date", "2024-02-03")  # fmt: skip
    run_pt("--port", holding, "cash", "margin-interest", "-a", "B", "--amount", "7",
           "--date", "2024-02-06")  # fmt: skip
    _value(run_pt, holding)

    fees = _column(holding, "fees_amount")
    assert fees["2024-02-01"] == D("5.00"), "the buy's commission"
    assert fees["2024-02-03"] == D("25.00")
    assert fees["2024-02-06"] == D("0.00"), "margin interest is a financing cost, not a fee"

    income = _column(holding, "income_amount")
    assert income["2024-02-09"] == D("100.00"), "gross of withholding, on the pay-date"
    assert sum(income.values()) == D("100.00")


def test_flows_are_stored_at_both_levels(run_pt: CliRunner, portfolio: Path) -> None:
    """A transfer between the owner's accounts is a flow at account level and
    none at portfolio level; a deposit is a flow at both (PORT-GIPS-B02)."""
    port = str(portfolio)
    run_pt("--port", port, "account", "add", "--name", "IRA", "--type", "tax_deferred",
           "--opened", "2024-01-02")  # fmt: skip
    run_pt("--port", port, "cash", "transfer", "--from", "B", "--to", "IRA",
           "--amount", "1000", "--date", "2024-01-03")  # fmt: skip
    run_pt("--port", port, "value", "--from", "2024-01-02", "--to", "2024-01-03")

    con = sqlite3.connect(port)
    try:
        stored = sorted(
            (str(level), str(on), str(name), str(amount))
            for level, on, name, amount in con.execute(
                "SELECT f.level, f.flow_date, a.name, f.amount FROM snapshot_flow f "
                "JOIN account a USING (account_id)"
            )
        )
    finally:
        con.close()

    assert stored == [
        ("account", "2024-01-02", "B", "500000.00"),
        ("account", "2024-01-03", "B", "-1000.00"),
        ("account", "2024-01-03", "IRA", "1000.00"),
        ("portfolio", "2024-01-02", "B", "500000.00"),
    ]
