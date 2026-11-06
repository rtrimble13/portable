"""Short sales and the options lifecycle, through the real CLI. ADR 0038.

Before ADR 0038, a short sale was booked as a purchase: shorting at 100.00 and
covering at 80.00 debited cash, credited it back, and reported a 2,000.00 LOSS.
The option commands derived their effects outside replay, so `pt validate`
failed straight after them and `pt rebuild` erased them. Exercising a call
charged its premium to cash a second time and left it out of the stock's
basis, and exercising a put bought stock.

Every scenario here checks the live figures, then that `pt validate` passes
and that `pt rebuild` leaves them unchanged (invariant 3).
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal as D
from pathlib import Path

import pytest

from tests.integration.conftest import CliResult, CliRunner

pytestmark = pytest.mark.integration

#: The fixture deposits this into account B.
FUNDED = D("500000.00")
AFTER = "2025-02-01"


def _option(run_pt: CliRunner, port: str, right: str = "call") -> None:
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    run_pt("--port", port, "instrument", "add", "XYZOPT", "--type", "option",
           "--underlier", "XYZ", "--right", right, "--strike", "120",
           "--expiry", "2025-01-17", "--multiplier", "100")  # fmt: skip


def _trade(run_pt: CliRunner, port: str, verb: str, symbol: str, qty: str, price: str,
           on: str, expect: int | None = 0) -> CliResult:  # fmt: skip
    return run_pt("--port", port, verb, symbol, "--qty", qty, "--price", price,
                  "--date", on, "-a", "B", expect=expect)  # fmt: skip


def _book(run_pt: CliRunner, port: str, year: str = "2025") -> tuple[object, ...]:
    """Everything a rebuild must leave unchanged."""
    cash = run_pt("--port", port, "info", "--as-of", AFTER).data["total_cash"]
    tax = run_pt("--port", port, "tax", "--year", year).data
    lots = run_pt("--port", port, "lot", "list", "--as-of", AFTER).data["rows"]
    return (
        D(cash),
        D(tax["short_term_gain"]),
        D(tax["long_term_gain"]),
        tuple((r["symbol"], r["quantity"], r["basis"], r["open_date"]) for r in lots),
    )


def _holds_through_rebuild(
    run_pt: CliRunner, port: str, year: str = "2025"
) -> tuple[object, ...]:
    """Validate, rebuild, and assert the book did not move. Returns the book."""
    before = _book(run_pt, port, year)
    run_pt("--port", port, "validate")
    run_pt("--port", port, "rebuild", "--yes")
    assert _book(run_pt, port, year) == before, "a rebuild changed the book"
    run_pt("--port", port, "validate")
    return before


# ── short sales ──────────────────────────────────────────────────────────────


def test_a_short_sale_receives_cash_and_a_profitable_cover_is_a_gain(
    run_pt: CliRunner, portfolio: Path
) -> None:
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    short = _trade(run_pt, port, "short", "XYZ", "100", "100.00", "2024-06-03").data
    assert D(short["net_cash_effect"]) == D("10000.00"), "a short sale RECEIVES its proceeds"

    run_pt("--port", port, "price", "set", "XYZ", "--date", "2024-06-10",
           "--price", "90.00")  # fmt: skip
    held = run_pt("--port", port, "holdings", "--as-of", "2024-06-10").data["rows"]
    (line,) = [r for r in held if r["symbol"] == "XYZ"]
    assert D(line["quantity"]) == D("-100"), "a short position is a negative holding"
    assert D(line["market_value"]) == D("-9000.00"), "and a liability, not an asset"
    assert D(line["unrealized"]) == D("1000.00"), "the price fell, so the short gained"

    cover = _trade(run_pt, port, "cover", "XYZ", "100", "80.00", "2024-07-01").data
    assert D(cover["net_cash_effect"]) == D("-8000.00"), "a cover PAYS"
    assert D(cover["short_proceeds_relieved"]) == D("10000.00")

    cash, short_term, long_term, _lots = _holds_through_rebuild(run_pt, port, "2024")
    assert cash == FUNDED + D("2000.00")
    assert short_term == D("2000.00"), "short at 100, covered at 80: a GAIN of 2,000"
    assert long_term == D("0.00")


def test_a_cover_relieves_only_short_lots_and_a_sale_only_long_ones(
    run_pt: CliRunner, portfolio: Path
) -> None:
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    _trade(run_pt, port, "buy", "XYZ", "100", "100.00", "2024-06-03")
    refused = _trade(run_pt, port, "cover", "XYZ", "100", "90.00", "2024-06-04", expect=None)
    assert refused.returncode != 0, "a cover must not consume a long lot"

    run_pt("--port", port, "instrument", "add", "ABC", "--type", "equity")
    _trade(run_pt, port, "short", "ABC", "50", "40.00", "2024-06-03")
    refused = _trade(run_pt, port, "sell", "ABC", "50", "30.00", "2024-06-04", expect=None)
    assert refused.returncode != 0, "a sale must not consume a short lot"


def test_opening_against_the_other_direction_is_refused(
    run_pt: CliRunner, portfolio: Path
) -> None:
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    _trade(run_pt, port, "buy", "XYZ", "100", "100.00", "2024-06-03")
    against_the_box = _trade(run_pt, port, "short", "XYZ", "100", "105.00", "2024-06-04",
                             expect=4)  # fmt: skip
    assert "PT-E-POSITION-DIRECTION" in against_the_box.stdout
    assert "1259" in against_the_box.stdout, "it names the constructive-sale rule"

    run_pt("--port", port, "instrument", "add", "ABC", "--type", "equity")
    _trade(run_pt, port, "short", "ABC", "50", "40.00", "2024-06-03")
    buy_while_short = _trade(run_pt, port, "buy", "ABC", "50", "38.00", "2024-06-04",
                             expect=4)  # fmt: skip
    assert "PT-E-POSITION-DIRECTION" in buy_while_short.stdout
    assert "pt cover" in buy_while_short.stdout


# ── options ──────────────────────────────────────────────────────────────────


def test_a_covered_call_assigned_survives_a_rebuild(run_pt: CliRunner, portfolio: Path) -> None:
    """The scenario a rebuild used to erase: 2,420.00 of long-term gain."""
    port = str(portfolio)
    _option(run_pt, port)
    _trade(run_pt, port, "buy", "XYZ", "100", "100.00", "2024-01-03")
    written = _trade(run_pt, port, "short", "XYZOPT", "1", "4.20", "2024-06-03").data
    assert D(written["net_cash_effect"]) == D("420.00"), "writing a call RECEIVES the premium"

    assigned = run_pt("--port", port, "option", "assign", "XYZOPT", "-a", "B",
                      "--date", "2025-01-17").data  # fmt: skip
    assert D(assigned["total_proceeds"]) == D("12420.00"), "premium into the PROCEEDS"
    assert D(assigned["net_cash_effect"]) == D("12000.00"), (
        "cash is the strike alone: the premium arrived when the call was written"
    )

    cash, short_term, long_term, lots = _holds_through_rebuild(run_pt, port)
    assert cash == FUNDED - D("10000.00") + D("420.00") + D("12000.00")
    assert long_term == D("2420.00"), "the stock's own holding period governs"
    assert short_term == D("0.00")
    assert lots == (), "the stock was delivered and the call closed"


def test_a_long_call_exercised_puts_its_premium_in_basis_not_in_cash(
    run_pt: CliRunner, portfolio: Path
) -> None:
    port = str(portfolio)
    _option(run_pt, port)
    _trade(run_pt, port, "buy", "XYZOPT", "1", "4.20", "2024-06-03")
    exercised = run_pt("--port", port, "option", "exercise", "XYZOPT", "-a", "B",
                       "--date", "2025-01-17").data  # fmt: skip
    assert D(exercised["stock_basis"]) == D("12420.00")
    assert D(exercised["net_cash_effect"]) == D("-12000.00"), "the premium is not paid twice"

    cash, _short, _long, lots = _holds_through_rebuild(run_pt, port)
    assert cash == FUNDED - D("420.00") - D("12000.00")
    assert lots == (("XYZ", "100", "12420.00", "2025-01-17"),), (
        "the stock is held from the exercise, at strike plus premium; the call is gone"
    )


def test_a_written_put_assigned_deducts_its_premium_from_basis(
    run_pt: CliRunner, portfolio: Path
) -> None:
    port = str(portfolio)
    _option(run_pt, port, "put")
    _trade(run_pt, port, "short", "XYZOPT", "1", "4.20", "2024-06-03")
    assigned = run_pt("--port", port, "option", "assign", "XYZOPT", "-a", "B",
                      "--date", "2025-01-17").data  # fmt: skip
    assert D(assigned["stock_basis"]) == D("11580.00")
    assert D(assigned["net_cash_effect"]) == D("-12000.00")

    cash, _short, _long, lots = _holds_through_rebuild(run_pt, port)
    assert cash == FUNDED + D("420.00") - D("12000.00")
    assert lots == (("XYZ", "100", "11580.00", "2025-01-17"),)


@pytest.mark.parametrize(
    ("verb", "expected_gain"),
    [("short", D("420.00")), ("buy", D("-420.00"))],
    ids=["written", "long"],
)
def test_an_option_expiring_is_a_close_at_zero(
    run_pt: CliRunner, portfolio: Path, verb: str, expected_gain: D
) -> None:
    port = str(portfolio)
    _option(run_pt, port)
    _trade(run_pt, port, verb, "XYZOPT", "1", "4.20", "2024-06-03")
    early = run_pt("--port", port, "option", "expire", "XYZOPT", "-a", "B",
                   "--date", "2025-01-10", expect=4)  # fmt: skip
    assert "expires on 2025-01-17" in early.stdout

    expired = run_pt("--port", port, "option", "expire", "XYZOPT", "-a", "B",
                     "--date", "2025-01-17").data  # fmt: skip
    assert D(expired["realized_gain"]) == expected_gain

    cash, short_term, long_term, lots = _holds_through_rebuild(run_pt, port)
    assert short_term == expected_gain, "held under a year either way; a lapse is short-term"
    assert long_term == D("0.00")
    assert lots == ()
    assert cash == FUNDED + (D("420.00") if verb == "short" else D("-420.00"))


def test_a_long_put_exercise_is_refused_and_writes_nothing(
    run_pt: CliRunner, portfolio: Path
) -> None:
    port = str(portfolio)
    _option(run_pt, port, "put")
    _trade(run_pt, port, "buy", "XYZ", "100", "130.00", "2024-01-03")
    _trade(run_pt, port, "buy", "XYZOPT", "1", "4.20", "2024-06-03")
    before = _book(run_pt, port)
    refused = run_pt("--port", port, "option", "exercise", "XYZOPT", "-a", "B",
                     "--date", "2025-01-17", expect=4)  # fmt: skip
    assert "PT-E-OPTION-UNSUPPORTED" in refused.stdout
    assert "1233" in refused.stdout
    assert _book(run_pt, port) == before


# ── rows written before ADR 0038 ─────────────────────────────────────────────


def _copy_row(port: str, txn_id: int, **changes: object) -> None:
    """Append a copy of a ledger row with *changes*, as an older writer would have."""
    con = sqlite3.connect(port)
    try:
        columns = [
            r[1] for r in con.execute('PRAGMA table_info("transaction")') if r[1] != "txn_id"
        ]
        row = dict(
            zip(
                columns,
                con.execute(
                    f'SELECT {", ".join(columns)} FROM "transaction" WHERE txn_id = ?',
                    (txn_id,),
                ).fetchone(),
                strict=True,
            )
        )
        row.update(changes)
        con.execute(
            f'INSERT INTO "transaction" ({", ".join(row)}) '
            f"VALUES ({', '.join('?' for _ in row)})",
            tuple(row.values()),
        )
        con.commit()
    finally:
        con.close()


def _last_txn(port: str) -> int:
    con = sqlite3.connect(port)
    try:
        return int(con.execute('SELECT max(txn_id) FROM "transaction"').fetchone()[0])
    finally:
        con.close()


def test_validate_names_a_short_sale_stored_under_the_old_sign(
    run_pt: CliRunner, portfolio: Path
) -> None:
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    _trade(run_pt, port, "short", "XYZ", "100", "100.00", "2024-06-03")
    _copy_row(port, _last_txn(port), seq=900, net_cash_effect="-10000.00")
    found = run_pt("--port", port, "validate", expect=4)
    assert "a short sale that paid 10000.00 instead of receiving it" in found.stdout


def test_validate_names_an_option_left_open_past_its_expiry(
    run_pt: CliRunner, portfolio: Path
) -> None:
    port = str(portfolio)
    _option(run_pt, port)
    _trade(run_pt, port, "buy", "XYZOPT", "1", "4.20", "2024-06-03")
    run_pt("--port", port, "--as-of", "2025-01-17", "validate")
    found = run_pt("--port", port, "--as-of", "2025-01-18", "validate", expect=4)
    assert "XYZOPT is open in B after its expiry on 2025-01-17" in found.stdout


def test_an_assignment_recorded_on_the_underlier_is_refused_by_replay(
    run_pt: CliRunner, portfolio: Path
) -> None:
    """The old shape: the row named the stock, so nothing said which option."""
    port = str(portfolio)
    _option(run_pt, port)
    _trade(run_pt, port, "buy", "XYZ", "100", "100.00", "2024-01-03")
    _copy_row(port, _last_txn(port), txn_type="option_assignment", seq=900,
              trade_date="2025-01-17", net_cash_effect="12420.00")  # fmt: skip
    found = run_pt("--port", port, "validate", expect=4)
    assert "unreplayable" in found.stdout
    assert "which is not an option" in found.stdout
