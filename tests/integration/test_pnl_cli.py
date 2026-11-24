"""`pt pnl`'s unrealized figure, valued only at prices `pt value` would accept.

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
