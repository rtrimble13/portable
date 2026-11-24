"""`pt pnl` and `pt holdings` value a holding only at a price `pt value` would accept.

Account B is funded with 500,000 on 2024-01-02 by the base template. Here it
buys 100 XYZ at 100 and 10 ABC at 50 on 2024-01-03. XYZ is priced once, at
120 on 2024-01-03. ABC is priced at 60 on 2024-03-01.

Valued at that months-old 120, XYZ would add 2,000 of unrealized gain that
nothing current supports. That is a plausible, wrong figure (PORT-GIPS-A09).
"""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner, build_template, stack_template

pytestmark = pytest.mark.integration


def _build(run_pt: CliRunner, portfolio: Path) -> str:
    port = str(portfolio)
    for symbol in ("XYZ", "ABC"):
        run_pt("--port", port, "instrument", "add", symbol, "--type", "equity")
    run_pt("--port", port, "buy", "XYZ", "--qty", "100", "--price", "100", "--date",
           "2024-01-03", "-a", "B")  # fmt: skip
    run_pt("--port", port, "buy", "ABC", "--qty", "10", "--price", "50", "--date",
           "2024-01-03", "-a", "B")  # fmt: skip
    for symbol, px, on in (("XYZ", "120", "2024-01-03"), ("ABC", "60", "2024-03-01")):
        run_pt("--port", port, "price", "set", symbol, "--price", px, "--date", on)
    return port


@pytest.fixture(scope="module")
def book_template(tmp_path_factory: pytest.TempPathFactory, portfolio_template: Path) -> Path:
    return build_template(tmp_path_factory, _build, start_from=portfolio_template)


@pytest.fixture
def book(book_template: Path, portfolio: Path) -> str:
    return str(stack_template(book_template, portfolio))


@pytest.mark.gips
def test_port_gips_a09_a_stale_price_is_not_carried_into_unrealized(
    run_pt: CliRunner, book: str
) -> None:
    """On 2024-03-01, XYZ's only price is 58 days old: excluded and named.
    Unrealized is ABC's 10 x (60 - 50) alone."""
    result = run_pt("--port", book, "--as-of", "2024-03-01", "pnl").json()
    data = result["data"]
    assert D(data["unrealized"]) == D("100.00")
    assert data["stale_instruments"] == [
        {"symbol": "XYZ", "price_date": "2024-01-03", "days_old": 58}
    ]
    assert data["unpriced_instruments"] == []
    assert data["staleness_tolerance_days"] == 5
    assert any(
        w.startswith("XYZ: the newest price is 58 days old (2024-01-03), beyond the 5-day")
        for w in result["warnings"]
    )


@pytest.mark.gips
@pytest.mark.parametrize(
    ("as_of", "unrealized", "stale"),
    [("2024-01-08", "2000.00", []), ("2024-01-09", "0.00", ["XYZ"])],
    ids=["five-days-old", "six-days-old"],
)
def test_the_tolerance_is_the_one_pt_value_applies(
    run_pt: CliRunner, book: str, as_of: str, unrealized: str, stale: list[str]
) -> None:
    """Five days old is within the default tolerance, and six is not, as for a
    snapshot. ABC has no price yet on either day, so it is unpriced."""
    data = run_pt("--port", book, "--as-of", as_of, "pnl").data
    assert D(data["unrealized"]) == D(unrealized)
    assert [entry["symbol"] for entry in data["stale_instruments"]] == stale
    assert data["unpriced_instruments"] == ["ABC"]


def test_a_raised_tolerance_is_honoured(run_pt: CliRunner, book: str) -> None:
    """With staleness_tolerance_days at 60, the 58-day-old price is accepted, as
    `pt value` would accept it."""
    data = run_pt("--port", book, "--as-of", "2024-03-01", "pnl",
                  extra_env={"PORTABLE_STALENESS_TOLERANCE_DAYS": "60"}).data  # fmt: skip
    assert D(data["unrealized"]) == D("2100.00")
    assert data["stale_instruments"] == []
    assert data["staleness_tolerance_days"] == 60


@pytest.mark.gips
def test_port_gips_a09_holdings_do_not_carry_a_stale_price_into_value(
    run_pt: CliRunner, book: str
) -> None:
    """On 2024-03-01 XYZ's newest price, 120 on 2024-01-03, is shown with its
    date, but values nothing: market value and unrealized are null, and the
    total is the cash and ABC alone. Cash is 500,000 less 10,000 and 500:
    489,500, so the total is 489,500 + 600 = 490,100, not 502,100."""
    from tests.unit.test_json_schemas import _validator

    result = run_pt("--port", book, "--as-of", "2024-03-01", "holdings").json()
    errors = sorted(_validator("holdings-1.0.json").iter_errors(result), key=str)
    assert not errors, [e.message for e in errors[:5]]
    data = result["data"]
    rows = {row["symbol"]: row for row in data["rows"]}
    xyz, abc = rows["XYZ"], rows["ABC"]
    assert (xyz["price"], xyz["price_date"]) == ("120", "2024-01-03")
    assert (xyz["market_value"], xyz["unrealized"], xyz["weight"]) == (None, None, None)
    assert (abc["market_value"], abc["price_date"]) == ("600.00", "2024-03-01")
    assert data["total_market_value"] == "490100.00"
    assert data["stale_instruments"] == [
        {"symbol": "XYZ", "price_date": "2024-01-03", "days_old": 58}
    ]
    assert any(w.startswith("XYZ: the newest price is 58 days old") for w in result["warnings"])

    table = run_pt("--port", book, "--as-of", "2024-03-01", "holdings", fmt="table").stdout
    assert "older than the 5-day staleness tolerance" in table


def test_holdings_grouped_leave_a_stale_holding_out_of_the_total(
    run_pt: CliRunner, book: str
) -> None:
    data = run_pt("--port", book, "--as-of", "2024-03-01", "holdings", "--by", "account").data
    (bucket,) = [row for row in data["rows"] if row["symbol"] == "B"]
    assert bucket["market_value"] == "490100.00"
    assert data["total_market_value"] == "490100.00"
