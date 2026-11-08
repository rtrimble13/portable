"""Return of capital reduces basis, through the real CLI. ADR 0039.

Before: `pt income roc` recorded the cash and nothing else. Replay stopped at
the row because it carries no quantity, and the engine function that reduces
basis had no caller. A return of capital of 200.00 on shares bought for
5,000.00 left the basis at 5,000.00, and the sale a year later reported a gain
200.00 too small -- in a document the owner files.

Each scenario checks the figures, then that `pt validate` passes and that
`pt rebuild` leaves them unchanged (invariant 3).
"""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest

from tests.integration.conftest import CliResult, CliRunner

pytestmark = pytest.mark.integration


def _buy(run_pt: CliRunner, port: str, qty: str, price: str, on: str) -> None:
    run_pt("--port", port, "buy", "XYZ", "--qty", qty, "--price", price, "--date", on,
           "-a", "B")  # fmt: skip


def _roc(run_pt: CliRunner, port: str, amount: str, *, pay: str, ex: str | None = None,
         expect: int | None = 0) -> CliResult:  # fmt: skip
    return run_pt("--port", port, "income", "roc", "XYZ", "-a", "B", "--amount", amount,
                  "--pay-date", pay, *(("--ex-date", ex) if ex else ()),
                  expect=expect)  # fmt: skip


def _lots(run_pt: CliRunner, port: str, on: str) -> list[tuple[str, str, str]]:
    rows = run_pt("--port", port, "lot", "list", "XYZ", "--as-of", on).data["rows"]
    return [(r["open_date"], r["quantity"], r["basis"]) for r in rows]


Book = tuple[list[tuple[str, str, str]], str, str]


def _stable(run_pt: CliRunner, port: str, on: str, year: str) -> Book:
    """Validate, rebuild, and assert nothing moved. Returns what was checked."""

    def book() -> Book:
        tax = run_pt("--port", port, "tax", "--year", year).data
        return (_lots(run_pt, port, on), tax["short_term_gain"], tax["long_term_gain"])

    before = book()
    run_pt("--port", port, "validate")
    run_pt("--port", port, "rebuild", "--yes")
    assert book() == before, "a rebuild changed the book"
    return before


@pytest.fixture
def port(run_pt: CliRunner, portfolio: Path) -> str:
    run_pt("--port", str(portfolio), "instrument", "add", "XYZ", "--type", "equity")
    return str(portfolio)


def test_return_of_capital_reduces_basis_and_so_raises_the_later_gain(
    run_pt: CliRunner, port: str
) -> None:
    _buy(run_pt, port, "100", "50.00", "2024-01-03")
    _roc(run_pt, port, "200.00", pay="2024-06-03")
    assert _lots(run_pt, port, "2024-06-04") == [("2024-01-03", "100", "4800.00")]

    run_pt("--port", port, "sell", "XYZ", "--qty", "100", "--price", "60.00",
           "--date", "2025-07-01", "-a", "B")  # fmt: skip
    _lots_after, short_term, long_term = _stable(run_pt, port, "2025-07-02", "2025")
    assert D(long_term) == D("1200.00"), "6,000 of proceeds against 4,800 of basis"
    assert D(short_term) == D("0.00"), "a return of capital does not reset the holding period"


def test_the_total_is_allocated_by_quantity_and_sums_exactly(
    run_pt: CliRunner, port: str
) -> None:
    _buy(run_pt, port, "1", "50.00", "2024-01-03")
    _buy(run_pt, port, "2", "50.00", "2024-01-04")
    _roc(run_pt, port, "1.00", pay="2024-06-03")
    lots, _st, _lt = _stable(run_pt, port, "2024-06-04", "2024")
    assert lots == [
        ("2024-01-03", "1", "49.67"),
        ("2024-01-04", "2", "99.33"),
    ], "0.33 and 0.67: largest remainder, and the parts sum to the 1.00 paid"


def test_a_distribution_beyond_basis_is_capital_gain_at_the_lots_own_period(
    run_pt: CliRunner, port: str
) -> None:
    _buy(run_pt, port, "100", "1.00", "2024-01-03")
    _roc(run_pt, port, "150.00", pay="2025-03-03")
    lots, short_term, long_term = _stable(run_pt, port, "2025-03-04", "2025")
    assert lots == [("2024-01-03", "100", "0.00")], "there is no negative basis"
    assert D(long_term) == D("50.00"), "the 50.00 beyond basis, held over a year"
    assert D(short_term) == D("0.00")


def test_shares_bought_on_or_after_the_ex_date_are_not_entitled(
    run_pt: CliRunner, port: str
) -> None:
    _buy(run_pt, port, "100", "50.00", "2024-01-03")
    _buy(run_pt, port, "100", "50.00", "2024-05-20")  # the ex-date itself
    _roc(run_pt, port, "200.00", ex="2024-05-20", pay="2024-06-03")
    lots, _st, _lt = _stable(run_pt, port, "2024-06-04", "2024")
    assert lots == [
        ("2024-01-03", "100", "4800.00"),
        ("2024-05-20", "100", "5000.00"),
    ]


def test_shares_sold_between_ex_date_and_pay_date_are_refused(
    run_pt: CliRunner, port: str
) -> None:
    _buy(run_pt, port, "100", "50.00", "2024-01-03")
    run_pt("--port", port, "sell", "XYZ", "--qty", "40", "--price", "55.00",
           "--date", "2024-05-25", "-a", "B")  # fmt: skip
    refused = _roc(run_pt, port, "200.00", ex="2024-05-20", pay="2024-06-03", expect=4)
    assert "PT-E-ROC-ENTITLEMENT" in refused.stdout
    assert _lots(run_pt, port, "2024-06-04") == [("2024-01-03", "60", "3000.00")], (
        "nothing was written"
    )


def test_a_return_of_capital_with_no_shares_held_is_refused(
    run_pt: CliRunner, port: str
) -> None:
    refused = _roc(run_pt, port, "200.00", pay="2024-06-03", expect=4)
    assert "PT-E-ROC-ENTITLEMENT" in refused.stdout
