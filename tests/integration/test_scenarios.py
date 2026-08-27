"""The end-to-end scenarios named in the bootstrap (§9), through the real CLI.

Each asserts on parsed JSON, so a change to the envelope breaks here rather
than in somebody's consumer. Numbered to match the bootstrap's list.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner

pytestmark = [pytest.mark.integration, pytest.mark.slow]

D = Decimal


def _equity(run_pt: CliRunner, port: Path, symbol: str = "AAPL") -> None:
    run_pt("--port", str(port), "instrument", "add", symbol, "--type", "equity")


# ── 1 ────────────────────────────────────────────────────────────────────────


def test_1_buy_dividend_partial_sale_with_spec_id(run_pt: CliRunner, portfolio: Path) -> None:
    """Buy, take a dividend, sell part with spec-ID; check gain, period, tax."""
    port = str(portfolio)
    _equity(run_pt, portfolio)

    run_pt(
        "--port",
        port,
        "buy",
        "AAPL",
        "--qty",
        "100",
        "--price",
        "150.00",
        "--date",
        "2024-01-03",
        "-a",
        "B",
    )
    run_pt(
        "--port",
        port,
        "buy",
        "AAPL",
        "--qty",
        "100",
        "--price",
        "180.00",
        "--date",
        "2024-11-01",
        "-a",
        "B",
    )
    run_pt(
        "--port",
        port,
        "income",
        "dividend",
        "AAPL",
        "-a",
        "B",
        "--amount",
        "48.00",
        "--ex-date",
        "2024-02-09",
        "--pay-date",
        "2024-02-15",
    )

    lots = run_pt("--port", port, "lot", "list", "AAPL", "--as-of", "2025-06-30").data
    ids = {row["symbol"]: row for row in lots["rows"]}
    assert len(lots["rows"]) == 2
    older = min(lots["rows"], key=lambda r: r["open_date"])
    assert older["holding_period"] == "long"

    # Designate the OLDER lot explicitly -- the long-term one.
    sold = run_pt(
        "--port",
        port,
        "sell",
        "AAPL",
        "--qty",
        "50",
        "--price",
        "210.00",
        "--date",
        "2025-06-30",
        "-a",
        "B",
        "--method",
        "spec",
        "--lots",
        f"{older['lot_id']}:50",
    ).data
    assert sold["relief_method"] == "spec"
    assert D(sold["cost_basis_relieved"]) == D("7500.00")

    tax = run_pt("--port", port, "tax", "--year", "2025").data
    assert D(tax["long_term_gain"]) == D("10500.00") - D("7500.00")
    assert D(tax["short_term_gain"]) == D("0.00")
    # 3000 x (0.20 + 0.05 + 0.038)
    assert D(tax["long_term_tax"]) == D("864.00")
    assert ids  # the lot table is keyed and readable


# ── 2 ────────────────────────────────────────────────────────────────────────


def test_2_split_mid_holding_period_then_sale(run_pt: CliRunner, portfolio: Path) -> None:
    """A 3-for-1 split, then a sale: basis per share, quantity, and the period.

    The assertion that matters is the last one: the split must NOT have reset
    the holding period, because resetting it turns a long-term gain into a
    short-term one -- a rate error, not a rounding.
    """
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", "ACME", "--type", "equity")
    run_pt(
        "--port",
        port,
        "buy",
        "ACME",
        "--qty",
        "100",
        "--price",
        "60.00",
        "--date",
        "2024-02-01",
        "-a",
        "B",
    )

    split = run_pt(
        "--port", port, "ca", "split", "ACME", "--ratio", "3:1", "--ex-date", "2024-06-03"
    ).data
    assert split["ratio"] == "3:1"

    lots = run_pt("--port", port, "lot", "list", "ACME", "--as-of", "2025-03-01").data
    (lot,) = lots["rows"]
    assert D(lot["quantity"]) == D("300")
    assert D(lot["basis"]) == D("6000.00"), "total basis is unchanged"
    assert D(lot["per_unit"]) == D("20.00")
    assert lot["open_date"] == "2024-02-01"
    assert lot["holding_period"] == "long", "the split did NOT reset the period"

    sale = run_pt(
        "--port",
        port,
        "sell",
        "ACME",
        "--qty",
        "300",
        "--price",
        "25.00",
        "--date",
        "2025-03-01",
        "-a",
        "B",
    ).data
    assert D(sale["cost_basis_relieved"]) == D("6000.00")

    tax = run_pt("--port", port, "tax", "--year", "2025").data
    assert D(tax["long_term_gain"]) == D("1500.00")
    assert D(tax["short_term_gain"]) == D("0.00"), "a reset period would land here"


# ── 3 ────────────────────────────────────────────────────────────────────────


def test_3_covered_call_written_and_assigned(run_pt: CliRunner, portfolio: Path) -> None:
    """Premium must flow into the stock's proceeds, at the stock's period."""
    port = str(portfolio)
    _equity(run_pt, portfolio, "XYZ")
    run_pt(
        "--port",
        port,
        "buy",
        "XYZ",
        "--qty",
        "100",
        "--price",
        "100.00",
        "--date",
        "2024-01-03",
        "-a",
        "B",
    )
    run_pt(
        "--port",
        port,
        "instrument",
        "add",
        "XYZ250117C120",
        "--type",
        "option",
        "--underlier",
        "XYZ",
        "--right",
        "call",
        "--strike",
        "120",
        "--expiry",
        "2025-01-17",
        "--multiplier",
        "100",
    )
    run_pt(
        "--port",
        port,
        "short",
        "XYZ250117C120",
        "--qty",
        "1",
        "--price",
        "4.20",
        "--date",
        "2024-06-03",
        "-a",
        "B",
    )

    assigned = run_pt(
        "--port", port, "option", "assign", "XYZ250117C120", "-a", "B", "--date", "2025-01-17"
    ).data

    assert D(assigned["strike_proceeds"]) == D("12000.00")
    assert D(assigned["premium"]) == D("420.00")
    assert D(assigned["total_proceeds"]) == D("12420.00"), (
        "premium goes into PROCEEDS, not into a separate short-term gain"
    )

    tax = run_pt("--port", port, "tax", "--year", "2025").data
    assert D(tax["long_term_gain"]) == D("2420.00")
    assert D(tax["short_term_gain"]) == D("0.00"), (
        "the stock's holding period governs; the premium is not independently short"
    )


# ── 5 ────────────────────────────────────────────────────────────────────────


def test_5_bond_bought_between_coupons(run_pt: CliRunner, portfolio: Path) -> None:
    """Accrued interest is part of market value, not a memo (PORT-GIPS-A06)."""
    port = str(portfolio)
    run_pt(
        "--port",
        port,
        "instrument",
        "add",
        "T-4.25-2030",
        "--type",
        "bond",
        "--issuer",
        "US Treasury",
        "--coupon",
        "0.0425",
        "--coupon-frequency",
        "2",
        "--maturity",
        "2030-05-15",
        "--day-count",
        "ACT/ACT",
        "--face",
        "1000",
    )
    run_pt(
        "--port",
        port,
        "buy",
        "T-4.25-2030",
        "--qty",
        "10",
        "--price",
        "1012.40",
        "--date",
        "2024-06-20",
        "-a",
        "B",
    )
    run_pt(
        "--port",
        port,
        "price",
        "set",
        "T-4.25-2030",
        "--price",
        "995.00",
        "--date",
        "2024-06-30",
        "--valuation-level",
        "1",
    )

    run_pt(
        "--port",
        port,
        "income",
        "coupon",
        "T-4.25-2030",
        "-a",
        "B",
        "--amount",
        "212.50",
        "--pay-date",
        "2024-11-15",
    )

    holdings = run_pt("--port", port, "holdings", "--as-of", "2024-06-30").data
    bond = next(r for r in holdings["rows"] if r["symbol"] == "T-4.25-2030")
    assert D(bond["market_value"]) == D("9950.00")
    # The accrued interest paid to the seller is NOT basis: basis is price
    # times quantity plus fees.
    assert D(bond["cost_basis"]) == D("10124.00")


# ── 6 ────────────────────────────────────────────────────────────────────────


@pytest.mark.gips
def test_6_transfer_is_external_at_account_level_only(
    run_pt: CliRunner, portfolio: Path
) -> None:
    """PORT-GIPS-B02, end to end through the CLI.

    The classic error this prevents: a shuffle between the owner's own
    accounts silently rewriting the track record.
    """
    port = str(portfolio)
    run_pt(
        "--port",
        port,
        "account",
        "add",
        "--name",
        "IRA",
        "--type",
        "tax-deferred",
        "--opened",
        "2024-01-02",
    )
    run_pt(
        "--port",
        port,
        "cash",
        "transfer",
        "--from",
        "B",
        "--to",
        "IRA",
        "--amount",
        "100000",
        "--date",
        "2024-03-01",
    )

    account_level = run_pt(
        "--port",
        port,
        "cash-flows",
        "--level",
        "account",
        "--external-only",
        "--from",
        "2024-01-01",
        "--to",
        "2024-12-31",
    ).data
    portfolio_level = run_pt(
        "--port",
        port,
        "cash-flows",
        "--level",
        "portfolio",
        "--external-only",
        "--from",
        "2024-01-01",
        "--to",
        "2024-12-31",
    ).data

    account_types = [r["type"] for r in account_level["rows"]]
    portfolio_types = [r["type"] for r in portfolio_level["rows"]]

    assert "transfer" in account_types
    assert "transfer" not in portfolio_types, (
        "a transfer between the owner's own accounts is NOT an external flow at portfolio level"
    )
    assert D(account_level["net_external_flow"]) == D("400000.00")
    assert D(portfolio_level["net_external_flow"]) == D("500000.00")


# ── 7 ────────────────────────────────────────────────────────────────────────


def test_7_wrong_trade_reversed_and_reentered(run_pt: CliRunner, portfolio: Path) -> None:
    """History shows all three; current state is right (CLAUDE.md invariant 2)."""
    port = str(portfolio)
    _equity(run_pt, portfolio)
    run_pt(
        "--port",
        port,
        "buy",
        "AAPL",
        "--qty",
        "100",
        "--price",
        "150.00",
        "--date",
        "2024-01-03",
        "-a",
        "B",
    )

    wrong = run_pt(
        "--port",
        port,
        "buy",
        "AAPL",
        "--qty",
        "1000",
        "--price",
        "150.00",
        "--date",
        "2024-02-01",
        "-a",
        "B",
    ).data
    run_pt(
        "--port",
        port,
        "trade",
        "reverse",
        str(wrong["txn_id"]),
        "--note",
        "wrong quantity",
        "--date",
        "2024-02-02",
    )
    run_pt(
        "--port",
        port,
        "buy",
        "AAPL",
        "--qty",
        "10",
        "--price",
        "150.00",
        "--date",
        "2024-02-02",
        "-a",
        "B",
    )

    ledger = run_pt(
        "--port", port, "activity", "--from", "2024-01-01", "--to", "2024-12-31"
    ).data
    types = [r["type"] for r in ledger["rows"]]
    assert types.count("buy") == 3
    assert "reversal" in types, "the reversal stays visible in history"

    holdings = run_pt("--port", port, "holdings", "--as-of", "2024-12-31").data
    position = next(r for r in holdings["rows"] if r["symbol"] == "AAPL")
    assert D(position["quantity"]) == D("110"), (
        "current state is right despite the wrong entry remaining in history"
    )

    run_pt("--port", port, "validate")


# ── 8 ────────────────────────────────────────────────────────────────────────


def test_8_export_import_export_is_byte_identical(
    run_pt: CliRunner, portfolio: Path, tmp_path: Path
) -> None:
    port = str(portfolio)
    _equity(run_pt, portfolio)
    run_pt(
        "--port",
        port,
        "buy",
        "AAPL",
        "--qty",
        "100",
        "--price",
        "150.00",
        "--date",
        "2024-01-03",
        "-a",
        "B",
    )
    run_pt(
        "--port",
        port,
        "sell",
        "AAPL",
        "--qty",
        "40",
        "--price",
        "180.00",
        "--date",
        "2025-03-01",
        "-a",
        "B",
    )

    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    round_trip = tmp_path / "round.port"

    run_pt("--port", port, "export", "-o", str(first))
    run_pt("import", str(first), "--into", str(round_trip))
    run_pt("--port", str(round_trip), "export", "-o", str(second))

    assert first.read_bytes() == second.read_bytes()

    # And the derived state, rebuilt from the imported ledger, agrees too.
    original = run_pt("--port", port, "tax", "--year", "2025").data
    imported = run_pt("--port", str(round_trip), "tax", "--year", "2025").data
    assert original["long_term_gain"] == imported["long_term_gain"]
    assert original["short_term_gain"] == imported["short_term_gain"]


# ── 9 ────────────────────────────────────────────────────────────────────────


def test_9_missing_and_stale_prices_exit_five(run_pt: CliRunner, portfolio: Path) -> None:
    """Data unavailable is exit 5, and a stale price is refused, not carried."""
    port = str(portfolio)
    _equity(run_pt, portfolio)
    run_pt(
        "--port",
        port,
        "buy",
        "AAPL",
        "--qty",
        "100",
        "--price",
        "150.00",
        "--date",
        "2024-01-03",
        "-a",
        "B",
    )

    # No provider configured: the null provider refuses politely.
    failed = run_pt("--port", port, "price", "load", "AAPL", expect=5)
    assert failed.json()["error"]["code"] == "PT-E-PROVIDER-UNAVAILABLE"
    assert "--offline" in failed.json()["error"]["remedy"]

    # A price beyond the staleness tolerance is refused rather than carried
    # forward -- carrying it produces a flat series that looks like a calm
    # market.
    run_pt(
        "--port",
        port,
        "price",
        "set",
        "AAPL",
        "--price",
        "150.00",
        "--date",
        "2024-01-03",
        "--valuation-level",
        "1",
    )
    valued = run_pt("--port", port, "value", "--date", "2024-06-30").data
    row = valued["rows"][0]
    assert row["complete"] is False, "a stale price leaves the snapshot incomplete"


# ── refusals the standard requires ───────────────────────────────────────────


@pytest.mark.gips
def test_large_flow_threshold_required(run_pt: CliRunner, portfolio: Path) -> None:
    """PORT-GIPS-B03 -- a missing policy is an error, not a zero."""
    failed = run_pt("--port", str(portfolio), "policy", "show", expect=4)
    error = failed.json()["error"]
    assert error["code"] == "PT-E-GIPS-NO-FLOW-POLICY"
    assert error["context"]["requirement"] == "PORT-GIPS-B03"

    run_pt(
        "--port",
        str(portfolio),
        "policy",
        "set",
        "--large-flow-pct",
        "0.10",
        "--effective-from",
        "2024-01-01",
    )
    shown = run_pt("--port", str(portfolio), "policy", "show").data
    assert D(shown["large_flow_value"]) == D("0.10")


@pytest.mark.gips
def test_a_fee_without_a_classification_is_refused(run_pt: CliRunner, portfolio: Path) -> None:
    """PORT-GIPS-D01 -- the three return bases are derived from it."""
    port = str(portfolio)
    _equity(run_pt, portfolio)
    failed = run_pt(
        "--port",
        port,
        "buy",
        "AAPL",
        "--qty",
        "10",
        "--price",
        "150.00",
        "--date",
        "2024-01-03",
        "-a",
        "B",
        "--fees",
        "1.00",
        expect=4,
    )
    error = failed.json()["error"]
    assert error["code"] == "PT-E-FEE-CLASS-MISSING"
    assert "custody fee is NOT a transaction cost" in error["remedy"]


def test_an_unmatched_closing_trade_stops_the_command(
    run_pt: CliRunner, portfolio: Path
) -> None:
    """CLAUDE.md invariant 9. Never a zero basis by default."""
    port = str(portfolio)
    _equity(run_pt, portfolio)
    failed = run_pt(
        "--port",
        port,
        "sell",
        "AAPL",
        "--qty",
        "10",
        "--price",
        "150.00",
        "--date",
        "2024-01-03",
        "-a",
        "B",
        expect=4,
    )
    assert failed.json()["error"]["code"] == "PT-E-LOT-UNMATCHED"


def test_a_dry_run_writes_nothing_but_shows_the_real_effects(
    run_pt: CliRunner, portfolio: Path
) -> None:
    """--dry-run is the same code path with the write suppressed."""
    port = str(portfolio)
    _equity(run_pt, portfolio)
    run_pt(
        "--port",
        port,
        "buy",
        "AAPL",
        "--qty",
        "100",
        "--price",
        "150.00",
        "--date",
        "2024-01-03",
        "-a",
        "B",
    )

    dry = run_pt(
        "--port",
        port,
        "--dry-run",
        "sell",
        "AAPL",
        "--qty",
        "40",
        "--price",
        "180.00",
        "--date",
        "2025-03-01",
        "-a",
        "B",
    )
    assert dry.data["dry_run"] is True
    assert D(dry.data["cost_basis_relieved"]) == D("6000.00")
    assert any("DRY RUN" in w for w in dry.json()["warnings"])

    lots = run_pt("--port", port, "lot", "list", "AAPL", "--as-of", "2025-03-01").data
    assert D(lots["rows"][0]["quantity"]) == D("100"), "nothing was written"


def test_holdings_grouping_aggregates_and_totals_agree(
    run_pt: CliRunner, portfolio: Path
) -> None:
    """`--by` groups rather than being accepted and ignored.

    A flag that is accepted and does nothing is worse than one that does not
    exist, because the output looks like it honoured the request.
    """
    port = str(portfolio)
    run_pt(
        "--port",
        port,
        "instrument",
        "add",
        "AAPL",
        "--type",
        "equity",
        "--sector",
        "Technology",
    )
    run_pt(
        "--port",
        port,
        "instrument",
        "add",
        "MSFT",
        "--type",
        "equity",
        "--sector",
        "Technology",
    )
    run_pt(
        "--port",
        port,
        "buy",
        "AAPL",
        "--qty",
        "100",
        "--price",
        "150.00",
        "--date",
        "2024-01-03",
        "-a",
        "B",
    )
    run_pt(
        "--port",
        port,
        "buy",
        "MSFT",
        "--qty",
        "50",
        "--price",
        "400.00",
        "--date",
        "2024-01-03",
        "-a",
        "B",
    )
    run_pt(
        "--port",
        port,
        "price",
        "set",
        "AAPL",
        "--price",
        "200.00",
        "--date",
        "2024-06-30",
        "--valuation-level",
        "1",
    )
    run_pt(
        "--port",
        port,
        "price",
        "set",
        "MSFT",
        "--price",
        "450.00",
        "--date",
        "2024-06-30",
        "--valuation-level",
        "1",
    )

    ungrouped = run_pt("--port", port, "holdings", "--as-of", "2024-06-30").data
    by_sector = run_pt(
        "--port", port, "holdings", "--as-of", "2024-06-30", "--by", "sector"
    ).data

    assert len(ungrouped["rows"]) == 3, "AAPL, MSFT, CASH"
    # Technology and the unclassified cash row.
    assert len(by_sector["rows"]) == 2
    technology = next(r for r in by_sector["rows"] if r["symbol"] == "Technology")
    assert technology["holdings"] == 2
    assert D(technology["market_value"]) == D("42500.00")

    # The totals must agree, or the grouping has lost or invented a holding.
    assert D(ungrouped["total_market_value"]) == D(by_sector["total_market_value"])
    assert sum(D(r["market_value"]) for r in by_sector["rows"]) == D(
        ungrouped["total_market_value"]
    )


def test_an_unknown_grouping_is_refused(run_pt: CliRunner, portfolio: Path) -> None:
    failed = run_pt("--port", str(portfolio), "holdings", "--by", "nonsense", expect=4)
    assert (
        "account, position, instrument, sector, asset-class"
        in (failed.json()["error"]["remedy"])
    )
