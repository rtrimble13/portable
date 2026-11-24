"""`pt holdings` values the portfolio as a snapshot values it: its total is `pt mark`'s.

Before, holdings-1.0 valued a bond at its clean price, with none of its
accrued interest, and left declared dividends out, so its total fell short of
the market value `pt mark` and `pt value` record by exactly the accrued income
(``PORT-GIPS-A06``). It counted an operating account's cash, which a snapshot
leaves out (``PORT-GIPS-A07``), and showed an instrument nothing quotes as
unpriced where a snapshot holds it at zero (ADR 0028).

Account B is funded with 500,000 on 2024-01-02 by the base template. Here it:
- sends 1,000 to OP, an account holding operating cash;
- buys 100 XYZ at 50 on 2024-01-03, which pays a 25.00 dividend with an
  ex-date of 2024-07-05 and a pay-date of 2024-07-20;
- buys 10,000 face of T-4.25-2030 at 101.24 on 2024-06-20, paying 42.73 of
  accrued interest to settle on the 21st;
- holds 100 CVR, a right nothing quotes.

On 2024-07-10, 56 days into the 184-day coupon period from 15 May, the bond
has accrued 10,000 x 0.0425 / 2 x 56 / 184 = 64.67, and the dividend is owed.
"""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import CliRunner, build_template, stack_template

pytestmark = pytest.mark.integration

BOND = "T-4.25-2030"
ON = "2024-07-10"
#: After the 15 November coupon, which the ledger does not record.
AFTER_COUPON = "2024-11-20"


def _build(run_pt: CliRunner, portfolio: Path) -> str:
    port = str(portfolio)

    def pt(*args: str) -> None:
        run_pt("--port", port, *args)

    pt("instrument", "add", BOND, "--type", "bond", "--issuer", "US Treasury", "--coupon",
       "0.0425", "--coupon-frequency", "2", "--maturity", "2030-05-15", "--face", "1000",
       "--asset-class", "fixed_income")  # fmt: skip
    pt("instrument", "add", "XYZ", "--type", "equity", "--asset-class", "equity")
    pt("instrument", "add", "CVR", "--type", "equity", "--no-market-price")
    pt("account", "add", "--name", "OP", "--type", "taxable", "--opened", "2024-01-02",
       "--relief-method", "fifo", "--cash-treatment", "operating")  # fmt: skip
    pt("cash", "transfer", "--from", "B", "--to", "OP", "--amount", "1000", "--date",
       "2024-01-02")  # fmt: skip
    pt("buy", "XYZ", "--qty", "100", "--price", "50", "--date", "2024-01-03", "-a", "B")
    pt("transfer", "in", "CVR", "-a", "B", "--qty", "100", "--value", "0",
       "--basis-source", "estimated", "--basis", "0", "--acquired", "2024-01-03",
       "--assumption", "a right received for nothing", "-d", "2024-01-03")  # fmt: skip
    pt("buy", BOND, "--qty", "10000", "--price", "101.24", "--date", "2024-06-20", "-a", "B",
       "--settlement", "2024-06-21", "--accrued-interest", "42.73")  # fmt: skip
    pt("income", "dividend", "XYZ", "-a", "B", "--amount", "25", "--ex-date", "2024-07-05",
       "--pay-date", "2024-07-20")  # fmt: skip
    for on in (ON, AFTER_COUPON):
        pt("price", "set", "XYZ", "--price", "55", "--date", on)
        pt("price", "set", BOND, "--price", "101.50", "--date", on)
    return port


@pytest.fixture(scope="module")
def held_template(tmp_path_factory: pytest.TempPathFactory, portfolio_template: Path) -> Path:
    return build_template(tmp_path_factory, _build, start_from=portfolio_template)


@pytest.fixture
def held(held_template: Path, portfolio: Path) -> str:
    return str(stack_template(held_template, portfolio))


def _holdings(run_pt: CliRunner, port: str, *extra: str, on: str = ON) -> dict[str, Any]:
    payload: dict[str, Any] = run_pt("--port", port, "--as-of", on, "holdings", *extra).json()
    from tests.unit.test_json_schemas import _validator

    errors = sorted(_validator("holdings-2.0.json").iter_errors(payload), key=str)
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors[:5]]
    return payload


def _marked(run_pt: CliRunner, port: str, on: str = ON) -> dict[str, D]:
    rows = run_pt("--port", port, "mark", "--as-of", on).data["rows"]
    return {row["account"]: D(row["ending"]) for row in rows}


def test_the_holdings_total_is_what_pt_mark_values(run_pt: CliRunner, held: str) -> None:
    data = _holdings(run_pt, held)["data"]
    marked = _marked(run_pt, held)
    assert data["is_complete"] is True
    assert D(data["total_market_value"]) == sum(marked.values())
    # B by hand: 500,000 - 1,000 - 5,000 - 10,166.73 of cash, 5,500 of XYZ,
    # 10,150 of bond, 64.67 accrued on it, 25.00 owed, and CVR at nothing.
    assert marked["B"] == D("499572.94")
    for account, value in marked.items():
        one = _holdings(run_pt, held, "-a", account)["data"]
        assert D(one["total_market_value"]) == value


def test_accrued_interest_and_a_declared_dividend_are_lines_of_their_own(
    run_pt: CliRunner, held: str
) -> None:
    """The bond's line stays at its clean value, so its unrealized gain is
    against its basis, which holds no accrued interest (ADR 0041)."""
    rows = _holdings(run_pt, held)["data"]["rows"]
    by_role = {(row["symbol"], row["role"]): row for row in rows}
    assert by_role[(BOND, "accrued_interest")]["market_value"] == "64.67"
    assert by_role[(BOND, "accrued_interest")]["cost_basis"] is None
    assert by_role[("XYZ", "accrued_dividend")]["market_value"] == "25.00"
    bond = by_role[(BOND, "bond")]
    assert (bond["market_value"], bond["cost_basis"]) == ("10150.00", "10124.00")
    assert bond["unrealized"] == "26.00"
    # Each weight is a quotient to 28 digits, so they sum to 1 within the last.
    assert abs(
        sum((D(row["weight"]) for row in rows if row["weight"] is not None), D(0)) - 1
    ) < D("1e-26")


def test_grouped_by_asset_class_income_goes_with_what_it_accrues_on(
    run_pt: CliRunner, held: str
) -> None:
    data = _holdings(run_pt, held, "--by", "asset-class")["data"]
    groups = {row["symbol"]: D(row["market_value"]) for row in data["rows"]}
    assert groups["fixed_income"] == D("10150.00") + D("64.67")
    assert groups["equity"] == D("5500.00") + D("25.00")
    assert abs(
        sum((D(r["weight"]) for r in data["rows"] if r["weight"] is not None), D(0)) - 1
    ) < D("1e-26")


@pytest.mark.gips
def test_port_gips_a07_operating_cash_is_shown_and_left_out_of_the_total(
    run_pt: CliRunner, held: str
) -> None:
    data = _holdings(run_pt, held)["data"]
    (cash,) = [row for row in data["rows"] if row["account"] == "OP"]
    assert (cash["symbol"], cash["market_value"], cash["weight"]) == ("CASH", "1000.00", None)
    assert data["excluded_operating_cash"] == [{"account": "OP", "amount": "1000.00"}]
    assert _marked(run_pt, held)["OP"] == D("0.00")


def test_an_instrument_nothing_quotes_is_held_at_zero_and_leaves_nothing_unvalued(
    run_pt: CliRunner, held: str
) -> None:
    """ADR 0028: the snapshot holds it at zero, at level 5, and is complete."""
    data = _holdings(run_pt, held)["data"]
    (cvr,) = [row for row in data["rows"] if row["symbol"] == "CVR"]
    assert (cvr["market_value"], cvr["price"]) == ("0.00", None)
    assert data["unvalued"] == []


def test_a_coupon_the_ledger_does_not_record_leaves_no_total(
    run_pt: CliRunner, held: str
) -> None:
    """On 2024-11-20 the 15 November coupon was owed and no row records it.
    `pt mark` marks B incomplete, and holdings has no total to give."""
    payload = _holdings(run_pt, held, on=AFTER_COUPON)
    data = payload["data"]
    assert (data["is_complete"], data["total_market_value"]) == (False, None)
    assert data["unvalued"] == [
        {"account": "B", "symbol": BOND, "code": "PT-E-COUPON-UNRECORDED"}
    ]
    (line,) = [row for row in data["rows"] if row["role"] == "accrued_interest"]
    assert line["market_value"] is None
    assert any("2024-11-15 coupon" in w for w in payload["warnings"])
    marked = run_pt("--port", held, "mark", "--as-of", AFTER_COUPON).data["rows"]
    assert {row["account"]: row["complete"] for row in marked}["B"] is False


def test_the_sample_total_is_what_pt_mark_values(run_pt: CliRunner) -> None:
    """On 2025-12-31 the sample's Treasury has accrued 270.03, which
    holdings-1.0 left out: it totalled 819,411.48."""
    from portable_core.lint._common import repo_root

    sample = str(repo_root() / "examples" / "sample.port")
    data = _holdings(run_pt, sample, on="2025-12-31")["data"]
    assert D(data["total_market_value"]) == sum(_marked(run_pt, sample, "2025-12-31").values())
    assert data["total_market_value"] == "819681.51"
    (accrued,) = [row for row in data["rows"] if row["role"] == "accrued_interest"]
    assert accrued["market_value"] == "270.03"
