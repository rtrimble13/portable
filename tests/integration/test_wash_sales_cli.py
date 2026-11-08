"""Wash sales, detected in replay across every account. ADR 0037.

Before: `pt tax` said on its face that it did not account for wash sales, and
reported every disallowed loss as deductible. Each scenario here checks the
figures, then that `pt validate` passes and `pt rebuild` leaves them
unchanged (invariant 3): every pairing is made when the later of its two rows
is applied, so a rebuild and a live append must agree.
"""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest

from tests.integration.conftest import CliResult, CliRunner

pytestmark = pytest.mark.integration

Lots = list[tuple[str, str, str, str, str]]


def _trade(run_pt: CliRunner, port: str, verb: str, qty: str, price: str, on: str,
           account: str = "B") -> CliResult:  # fmt: skip
    return run_pt("--port", port, verb, "XYZ", "--qty", qty, "--price", price,
                  "--date", on, "-a", account)  # fmt: skip


def _lots(run_pt: CliRunner, port: str, on: str) -> Lots:
    rows = run_pt("--port", port, "lot", "list", "XYZ", "--as-of", on).data["rows"]
    return [
        (r["account"], r["lot_key"], r["quantity"], r["basis"], r["holding_period"])
        for r in rows
    ]


def _gains(run_pt: CliRunner, port: str, year: str) -> tuple[D, D]:
    tax = run_pt("--port", port, "tax", "--year", year).data
    return D(tax["short_term_gain"]), D(tax["long_term_gain"])


def _stable(run_pt: CliRunner, port: str, on: str, *years: str) -> None:
    """Validate, rebuild, and assert nothing moved."""

    def book() -> tuple[object, ...]:
        return (_lots(run_pt, port, on), *(_gains(run_pt, port, y) for y in years))

    before = book()
    run_pt("--port", port, "validate")
    run_pt("--port", port, "rebuild", "--yes")
    assert book() == before, "a rebuild changed the book"


@pytest.fixture
def port(run_pt: CliRunner, portfolio: Path) -> str:
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    run_pt("--port", port, "account", "add", "--name", "IRA", "--type", "tax_deferred",
           "--opened", "2024-01-02", "--relief-method", "fifo")  # fmt: skip
    run_pt("--port", port, "cash", "transfer", "--from", "B", "--to", "IRA",
           "--amount", "50000", "--date", "2024-01-02")  # fmt: skip
    return port


def test_a_repurchase_after_a_loss_defers_it_into_the_new_basis(
    run_pt: CliRunner, port: str
) -> None:
    _trade(run_pt, port, "buy", "100", "50.00", "2024-01-02")
    sold = _trade(run_pt, port, "sell", "100", "40.00", "2024-03-01").data
    bought = _trade(run_pt, port, "buy", "100", "42.00", "2024-03-15")
    assert any("a wash sale" in w and "1000.00" in w for w in bought.json()["warnings"])

    # Held 59 days before the sale; the replacement's start moves back by
    # those days, so it is long-term a year after 2024-01-16, not 2024-03-15.
    key = f"{bought.data['txn_id']}~{sold['txn_id']}.{_first_key(run_pt, port)}"
    assert _lots(run_pt, port, "2024-03-16") == [("B", key, "100", "5200.00", "short")]
    assert _gains(run_pt, port, "2024") == (D("0.00"), D("0.00")), (
        "the 1,000.00 loss is disallowed: deferred into the new shares' basis"
    )

    _trade(run_pt, port, "sell", "100", "60.00", "2025-02-01")
    assert _gains(run_pt, port, "2025") == (D("0.00"), D("800.00")), (
        "6,000 against 5,200, long-term only because the holding period tacked"
    )
    _stable(run_pt, port, "2025-02-02", "2024", "2025")


def test_fewer_shares_bought_disallow_only_their_share_of_the_loss(
    run_pt: CliRunner, port: str
) -> None:
    _trade(run_pt, port, "buy", "100", "50.00", "2024-01-02")
    _trade(run_pt, port, "sell", "100", "40.00", "2024-03-01")
    _trade(run_pt, port, "buy", "40", "42.00", "2024-03-20")
    assert _gains(run_pt, port, "2024") == (D("-600.00"), D("0.00"))
    ((_account, _key, qty, basis, _period),) = _lots(run_pt, port, "2024-03-21")
    assert (qty, basis) == ("40", "2080.00"), "1,680 paid plus 400 of the loss"
    _stable(run_pt, port, "2024-03-21", "2024")


def test_a_purchase_before_the_sale_is_a_replacement_too(run_pt: CliRunner, port: str) -> None:
    _trade(run_pt, port, "buy", "100", "50.00", "2024-01-02")
    _trade(run_pt, port, "buy", "100", "41.00", "2024-02-20")
    run_pt("--port", port, "sell", "XYZ", "--qty", "100", "--price", "40.00",
           "--date", "2024-03-01", "-a", "B", "--method", "fifo")  # fmt: skip
    assert _gains(run_pt, port, "2024") == (D("0.00"), D("0.00"))
    ((_account, _key, qty, basis, _period),) = _lots(run_pt, port, "2024-03-02")
    assert (qty, basis) == ("100", "5100.00")
    _stable(run_pt, port, "2024-03-02", "2024")


def test_shares_bought_outside_the_window_are_not_replacements(
    run_pt: CliRunner, port: str
) -> None:
    _trade(run_pt, port, "buy", "100", "50.00", "2024-01-02")
    _trade(run_pt, port, "sell", "100", "40.00", "2024-03-01")
    _trade(run_pt, port, "buy", "100", "42.00", "2024-04-01")  # 31 days after
    assert _gains(run_pt, port, "2024") == (D("-1000.00"), D("0.00"))


def test_an_ira_replacement_disallows_the_loss_for_good(run_pt: CliRunner, port: str) -> None:
    _trade(run_pt, port, "buy", "100", "50.00", "2024-01-02")
    _trade(run_pt, port, "sell", "100", "40.00", "2024-03-01")
    bought = _trade(run_pt, port, "buy", "100", "42.00", "2024-03-15", account="IRA")
    assert any("permanently disallowed" in w for w in bought.json()["warnings"])
    assert _gains(run_pt, port, "2024") == (D("0.00"), D("0.00"))
    ((account, _key, qty, basis, _period),) = _lots(run_pt, port, "2024-03-16")
    assert (account, qty, basis) == ("IRA", "100", "4200.00"), (
        "Rev. Rul. 2008-5: the IRA's basis is NOT increased"
    )
    _stable(run_pt, port, "2024-03-16", "2024")


def test_a_january_purchase_restates_the_december_loss(run_pt: CliRunner, port: str) -> None:
    _trade(run_pt, port, "buy", "100", "50.00", "2024-06-03")
    _trade(run_pt, port, "sell", "100", "40.00", "2024-12-20")
    assert _gains(run_pt, port, "2024") == (D("-1000.00"), D("0.00"))
    bought = _trade(run_pt, port, "buy", "100", "42.00", "2025-01-10")
    assert any("restates tax year 2024" in w for w in bought.json()["warnings"])
    assert _gains(run_pt, port, "2024") == (D("0.00"), D("0.00"))
    _stable(run_pt, port, "2025-01-11", "2024")


def test_a_loss_in_a_sheltered_account_is_not_a_wash_sale(run_pt: CliRunner, port: str) -> None:
    _trade(run_pt, port, "buy", "100", "50.00", "2024-01-02", account="IRA")
    _trade(run_pt, port, "sell", "100", "40.00", "2024-03-01", account="IRA")
    _trade(run_pt, port, "buy", "100", "42.00", "2024-03-15", account="IRA")
    ((_account, key, _qty, basis, _period),) = _lots(run_pt, port, "2024-03-16")
    assert "~" not in key and basis == "4200.00"


def test_a_sale_from_one_purchase_is_not_washed_by_its_other_shares(
    run_pt: CliRunner, port: str
) -> None:
    """Shares bought together are one purchase, not each other's replacement."""
    _trade(run_pt, port, "buy", "100", "50.00", "2024-02-20")
    _trade(run_pt, port, "sell", "50", "40.00", "2024-03-01")
    assert _gains(run_pt, port, "2024") == (D("-500.00"), D("0.00"))


def _first_key(run_pt: CliRunner, port: str) -> str:
    """The lineage key of the first lot the fixture's XYZ buy opened."""
    rows = run_pt("--port", port, "trade", "list").data["rows"]
    first = next(r for r in rows if r.get("symbol") == "XYZ")
    return str(first["txn_id"])


def test_a_replacement_sold_before_the_loss_sale_is_reported_and_the_year_incomplete(
    run_pt: CliRunner, port: str
) -> None:
    """ADR 0037, answer 1: reported for manual handling, never guessed."""
    _trade(run_pt, port, "buy", "100", "50.00", "2024-01-02")
    _trade(run_pt, port, "buy", "100", "41.00", "2024-02-20")
    run_pt("--port", port, "sell", "XYZ", "--qty", "100", "--price", "45.00",
           "--date", "2024-02-25", "-a", "B", "--method", "lifo")  # fmt: skip
    sold = _trade(run_pt, port, "sell", "100", "40.00", "2024-03-01")
    assert any("manual handling" in w for w in sold.json()["warnings"])

    tax = run_pt("--port", port, "tax", "--year", "2024").data
    assert tax["is_complete"] is False
    (manual,) = tax["wash_sales"]["manual"]
    assert (manual["quantity"], manual["disallowed"]) == ("100", "1000.00")
    assert D(tax["short_term_gain"]) == D("-600.00"), (
        "400 gained on the replacement, 1,000 lost, and nothing applied"
    )
    _stable(run_pt, port, "2024-03-02", "2024")


def test_the_tax_report_states_the_adjustment_beside_the_gain(
    run_pt: CliRunner, port: str
) -> None:
    _trade(run_pt, port, "buy", "100", "50.00", "2024-01-02")
    _trade(run_pt, port, "sell", "100", "40.00", "2024-03-01")
    _trade(run_pt, port, "buy", "100", "42.00", "2024-03-15")
    tax = run_pt("--port", port, "tax", "--year", "2024").json()["data"]
    (row,) = tax["rows"]
    assert (row["gain"], row["wash_sale_disallowed"], row["allowed_gain"]) == (
        "-1000.00",
        "1000.00",
        "0.00",
    ), "Form 8949: the gain as computed, code W's adjustment, and the result"
    assert tax["wash_sales"]["disallowed"] == "1000.00"
    assert tax["wash_sales"]["permanently_disallowed"] == "0.00"
    assert tax["is_complete"] is True


def test_pnl_counts_a_deferred_loss_once(run_pt: CliRunner, port: str) -> None:
    """Realized, it would be counted again in the replacement's higher basis."""
    _trade(run_pt, port, "buy", "100", "50.00", "2024-01-02")
    _trade(run_pt, port, "sell", "100", "40.00", "2024-03-01")
    _trade(run_pt, port, "buy", "100", "42.00", "2024-03-15")
    run_pt("--port", port, "price", "set", "XYZ", "--price", "42.00",
           "--date", "2024-03-15")  # fmt: skip
    pnl = run_pt("--port", port, "pnl", "--year", "2024", "--as-of", "2024-03-15").data
    assert D(pnl["realized"]) + D(pnl["unrealized"]) == D("-1000.00"), (
        "paid 5,000 and 4,200, received 4,000, hold 4,200: down 1,000 in all"
    )


def test_a_different_instrument_is_a_candidate_until_declared_identical(
    run_pt: CliRunner, port: str
) -> None:
    """Decision 8: declared, never inferred; a declaration restating history is refused."""
    run_pt("--port", port, "instrument", "add", "ABC", "--type", "etf")
    _trade(run_pt, port, "buy", "100", "50.00", "2024-01-02")
    _trade(run_pt, port, "sell", "100", "40.00", "2024-03-01")
    run_pt("--port", port, "buy", "ABC", "--qty", "100", "--price", "42.00",
           "--date", "2024-03-10", "-a", "B")  # fmt: skip
    assert _gains(run_pt, port, "2024") == (D("-1000.00"), D("0.00")), "not inferred"

    listed = run_pt("--port", port, "tax", "wash-candidates", "--year", "2024").json()
    (row,) = listed["data"]["rows"]
    assert (row["candidate"], row["reason"]) == ("ABC", "different_instrument")
    narrowed = run_pt("--port", port, "tax", "wash-candidates", "--year", "2024",
                      "--type", "equity").json()  # fmt: skip
    assert narrowed["data"]["rows"] == [], "the filter is for reading"

    refused = run_pt("--port", port, "instrument", "equivalent", "XYZ", "ABC",
                     "--reason", "same index fund, two share classes", expect=4)  # fmt: skip
    assert "PT-E-RESTATES-HISTORY" in refused.stdout
    assert _gains(run_pt, port, "2024") == (D("-1000.00"), D("0.00")), "nothing recorded"

    run_pt("--port", port, "instrument", "equivalent", "XYZ", "ABC",
           "--reason", "same index fund, two share classes", "--restate")  # fmt: skip
    assert _gains(run_pt, port, "2024") == (D("0.00"), D("0.00"))
    rows = run_pt("--port", port, "lot", "list", "ABC", "--as-of", "2024-03-11").data["rows"]
    assert [r["basis"] for r in rows] == ["5200.00"], "4,200 paid plus 1,000 deferred"
    assert run_pt("--port", port, "instrument", "equivalents").data["pairs"] == 1
    _stable(run_pt, port, "2024-03-11", "2024")


def test_an_option_on_the_same_underlying_is_listed_not_applied(
    run_pt: CliRunner, port: str
) -> None:
    run_pt("--port", port, "instrument", "add", "XYZOPT", "--type", "option",
           "--underlier", "XYZ", "--right", "call", "--strike", "45",
           "--expiry", "2024-06-21", "--multiplier", "100")  # fmt: skip
    _trade(run_pt, port, "buy", "100", "50.00", "2024-01-02")
    _trade(run_pt, port, "sell", "100", "40.00", "2024-03-01")
    run_pt("--port", port, "buy", "XYZOPT", "--qty", "1", "--price", "2.00",
           "--date", "2024-03-05", "-a", "B")  # fmt: skip
    assert _gains(run_pt, port, "2024") == (D("-1000.00"), D("0.00")), "ADR 0037, answer 3"
    listed = run_pt("--port", port, "tax", "wash-candidates", "--year", "2024").json()
    (row,) = listed["data"]["rows"]
    assert (row["candidate"], row["reason"]) == ("XYZOPT", "option_on_underlying")


def _lot_id(run_pt: CliRunner, port: str, on: str, key: str) -> str:
    rows = run_pt("--port", port, "lot", "list", "XYZ", "--as-of", on).data["rows"]
    return str(next(r["lot_id"] for r in rows if r["lot_key"] == key))


def test_a_designation_a_later_split_divided_is_refused_until_re_entered(
    run_pt: CliRunner, port: str
) -> None:
    """ADR 0037, answer 2, by the reversal route the owner chose."""
    first = _trade(run_pt, port, "buy", "100", "50.00", "2024-01-02").data
    repl = _trade(run_pt, port, "buy", "100", "42.00", "2024-03-15").data
    whole = str(repl["txn_id"])
    designated = run_pt(
        "--port", port, "sell", "XYZ", "--qty", "10", "--price", "45.00",
        "--date", "2024-04-20", "-a", "B", "--method", "spec",
        "--lots", f"{_lot_id(run_pt, port, '2024-04-19', whole)}:10",
    ).data  # fmt: skip

    # Back-dated: a loss sale the replacement washes, which splits it.
    loss = run_pt(
        "--port", port, "sell", "XYZ", "--qty", "100", "--price", "40.00",
        "--date", "2024-03-01", "-a", "B", "--method", "spec",
        "--lots", f"{_lot_id(run_pt, port, '2024-02-28', str(first['txn_id']))}:100",
    )  # fmt: skip
    assert any("divided" in w for w in loss.json()["warnings"]), "said at once"
    refused = run_pt("--port", port, "validate", expect=4)
    assert "PT-E-LOT-DESIGNATION-DIVIDED" in refused.stdout or "divided" in refused.stdout

    run_pt("--port", port, "trade", "reverse", str(designated["txn_id"]),
           "--date", "2024-04-21")  # fmt: skip
    part = f"{whole}~{loss.data['txn_id']}.{first['txn_id']}"
    run_pt("--port", port, "sell", "XYZ", "--qty", "10", "--price", "45.00",
           "--date", "2024-04-22", "-a", "B", "--method", "spec",
           "--lots", f"{_lot_id(run_pt, port, '2024-04-21', part)}:10")  # fmt: skip
    run_pt("--port", port, "validate")


def test_a_reconstructed_block_beside_a_deferral_is_named(run_pt: CliRunner, port: str) -> None:
    """ADR 0037, decision 7: the custodian's figure may already include it."""
    run_pt("--port", port, "transfer", "in", "XYZ", "-a", "B", "--qty", "50",
           "--value", "2500.00", "--basis-source", "reconstructed",
           "--basis", "2400.00", "--acquired", "2023-06-01",
           "--assumption", "present basis less additions", "--date", "2024-01-02")  # fmt: skip
    _trade(run_pt, port, "buy", "100", "50.00", "2024-01-03")
    run_pt("--port", port, "sell", "XYZ", "--qty", "100", "--price", "40.00",
           "--date", "2024-03-01", "-a", "B", "--method", "lifo")  # fmt: skip
    _trade(run_pt, port, "buy", "100", "42.00", "2024-03-15")
    checked = run_pt("--port", port, "validate").json()
    assert any("reconstructed block" in w for w in checked["warnings"])
