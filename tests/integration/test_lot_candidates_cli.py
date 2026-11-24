"""`pt lot candidates`: each open lot as if sold alone on --as-of. ADR 0047.

One portfolio reaches every case, as of Monday 2025-03-03. Account B is
taxable at 37% short-term and 20% long-term federal, 5% state and 3.8% NIIT,
so 45.8% short and 28.8% long in all. IRA is tax-deferred. C is taxable with
no rate schedule, and D is taxable with average cost as its default.

Every instrument is priced from a file on the date, except STA, whose newest
price is 30 days old, and MIS, which has none. The figures below are worked
by hand from those prices, and `test_estimated_tax_matches_pt_tax_for_the_
same_sale` holds every row to the sale it describes, made for real.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from portable_core.domain.dates import is_long_term, long_term_from
from tests.integration.conftest import CliRunner, build_template, copy_template, stack_template

pytestmark = pytest.mark.integration

AS_OF = "2025-03-03"

#: symbol -> the price on AS_OF. LEAP is also priced on 2025-02-28.
PRICES = {
    "LTG": "60", "STG": "25", "STL": "30", "LTL": "70", "WSH": "40", "IRW": "40",
    "OND": "12", "LEAP": "15", "PRM": "40", "TWO": "40", "UNA": "30", "REC": "30",
    "NRT": "30", "AVG": "30",
}  # fmt: skip

#: (symbol, account, qty, price, date): opening purchases, in date order.
BUYS = [
    ("LTG", "B", "100", "50", "2024-01-02"),
    ("LTL", "B", "100", "80", "2024-01-02"),
    ("LEAP", "B", "100", "10", "2024-02-29"),
    ("OND", "B", "100", "10", "2024-03-03"),
    ("WSH", "B", "100", "50", "2024-06-03"),
    ("IRW", "B", "100", "50", "2024-06-03"),
    ("PRM", "B", "100", "50", "2024-06-03"),
    ("TWO", "B", "50", "50", "2024-06-03"),
    ("STA", "B", "100", "50", "2024-06-03"),
    ("MIS", "B", "100", "50", "2024-06-03"),
    ("NRT", "C", "100", "50", "2024-06-03"),
    ("AVG", "D", "100", "50", "2024-06-03"),
    ("PRM", "B", "100", "50", "2024-07-01"),
    ("TWO", "B", "50", "50", "2024-07-01"),
    ("STG", "B", "100", "20", "2024-10-01"),
    ("STL", "B", "100", "40", "2024-11-01"),
]


def _build(run_pt: CliRunner, portfolio: Path) -> str:
    port = str(portfolio)

    def pt(*args: str) -> None:
        run_pt("--port", port, *args)

    symbols = sorted({b[0] for b in BUYS} | {"UNA", "REC", "SHT"})
    for symbol in symbols:
        pt("instrument", "add", symbol, "--type", "equity")
    pt("account", "add", "--name", "IRA", "--type", "tax_deferred", "--opened", "2024-01-02",
       "--relief-method", "fifo")  # fmt: skip
    pt("account", "add", "--name", "C", "--type", "taxable", "--opened", "2024-01-02",
       "--relief-method", "fifo")  # fmt: skip
    pt("account", "add", "--name", "D", "--type", "taxable", "--opened", "2024-01-02",
       "--relief-method", "avg")  # fmt: skip
    pt("account", "tax-rates", "set", "-a", "D", "--short", "0.37", "--long", "0.20",
       "--state", "0.05", "--niit", "0.038", "--effective-from", "2024-01-01")  # fmt: skip
    for target in ("IRA", "C", "D"):
        pt("cash", "transfer", "--from", "B", "--to", target, "--amount", "20000",
           "--date", "2024-01-02")  # fmt: skip
    for symbol, account, qty, price, on in BUYS:
        pt("buy", symbol, "--qty", qty, "--price", price, "--date", on, "-a", account)
    # Lots that predate the ledger: one with no derivable basis, one rebuilt.
    pt("transfer", "in", "UNA", "-a", "B", "--qty", "100", "--value", "4000",
       "--basis-source", "unavailable", "--acquired", "2023-05-01",
       "--assumption", "no record of the purchase survives", "-d", "2024-06-03")  # fmt: skip
    pt("transfer", "in", "REC", "-a", "B", "--qty", "100", "--value", "4000",
       "--basis-source", "reconstructed", "--basis", "3500", "--acquired", "2023-05-01",
       "--assumption", "rebuilt from statements", "-d", "2024-06-03")  # fmt: skip
    # PRM: a loss whose replacement is already matched, so it washes nothing more.
    pt("sell", "PRM", "--qty", "100", "--price", "40", "--date", "2025-02-10", "-a", "B")
    pt("buy", "PRM", "--qty", "100", "--price", "41", "--date", "2025-02-14", "-a", "B")
    # Recent purchases inside the window of a sale on AS_OF.
    pt("buy", "WSH", "--qty", "30", "--price", "41", "--date", "2025-02-20", "-a", "B")
    pt("buy", "IRW", "--qty", "40", "--price", "42", "--date", "2025-02-25", "-a", "IRA")
    pt("buy", "TWO", "--qty", "10", "--price", "41", "--date", "2025-02-28", "-a", "B")
    pt("short", "SHT", "--qty", "10", "--price", "20", "--date", "2025-02-03", "-a", "B")

    lines = ["symbol,date,price,source,valuation_level"]
    lines += [f"{s},{AS_OF},{p},test,1" for s, p in sorted(PRICES.items())]
    lines += ["LEAP,2025-02-28,15,test,1", "STA,2025-02-01,45,test,1"]
    prices = portfolio.parent / "prices.csv"
    prices.write_text("\n".join(lines) + "\n", encoding="utf-8")
    pt("price", "load", "--file", str(prices))
    return port


@pytest.fixture(scope="module")
def candidates_template(
    tmp_path_factory: pytest.TempPathFactory, portfolio_template: Path
) -> Path:
    return build_template(tmp_path_factory, _build, start_from=portfolio_template)


@pytest.fixture
def port(candidates_template: Path, portfolio: Path) -> str:
    return str(stack_template(candidates_template, portfolio))


def _report(run_pt: CliRunner, port: str, *extra: str, on: str = AS_OF) -> dict[str, Any]:
    payload: dict[str, Any] = run_pt(
        "--port", port, "lot", "candidates", "--as-of", on, *extra
    ).json()
    return payload


def _rows(payload: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    return [c for c in payload["data"]["candidates"] if c["symbol"] == symbol]


def _one(payload: dict[str, Any], symbol: str) -> dict[str, Any]:
    (row,) = _rows(payload, symbol)
    return row


def _validate(payload: dict[str, Any]) -> None:
    from tests.unit.test_json_schemas import _validator

    errors = sorted(_validator("lot-candidates-1.0.json").iter_errors(payload), key=str)
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors[:5]]


# ── the holding period (§3) ──────────────────────────────────────────────────


def test_a_lot_one_day_short_of_long_term_says_so(run_pt: CliRunner, port: str) -> None:
    """Bought 2024-03-03 and sold on the anniversary: short-term, one day short,
    and long-term from the day after. The 29 February lot turns long-term on
    1 March, which is what `is_long_term` says, and is one day short on the
    28th."""
    payload = _report(run_pt, port)
    _validate(payload)
    row = _one(payload, "OND")
    assert (row["holding_period"], row["days_to_long_term"]) == ("short", 1)
    assert row["long_term_from"] == "2025-03-04"
    assert D(row["gain"]) == D("200.00")
    # The rate falls from 45.8% to 28.8% the next day: 34.00 saved by waiting.
    assert D(row["tax"]["estimated_tax"]) == D("91.60")
    assert D(row["waiting_saves"]) == D("34.00")

    leap = _one(payload, "LEAP")
    acquired = date(2024, 2, 29)
    first = long_term_from(acquired)
    assert leap["long_term_from"] == first.isoformat() == "2025-03-01"
    assert is_long_term(acquired, first) and not is_long_term(acquired, date(2025, 2, 28))
    assert (leap["holding_period"], leap["days_to_long_term"]) == ("long", 0)
    assert leap["waiting_saves"] is None
    before = _one(_report(run_pt, port, "LEAP", on="2025-02-28"), "LEAP")
    assert (before["holding_period"], before["days_to_long_term"]) == ("short", 1)


def test_a_short_term_gain_and_loss_show_what_waiting_changes(
    run_pt: CliRunner, port: str
) -> None:
    """A 500 gain saves 85.00 by waiting: 500 x (45.8% - 28.8%). A 1,000 loss
    offsets 170.00 less once long-term, so waiting costs: negative."""
    payload = _report(run_pt, port)
    gain, loss = _one(payload, "STG"), _one(payload, "STL")
    assert (D(gain["gain"]), D(gain["tax"]["estimated_tax"])) == (D("500.00"), D("229.00"))
    assert D(gain["waiting_saves"]) == D("85.00")
    assert D(gain["tax_per_dollar"]) == D("0.091600")
    assert (D(loss["gain"]), D(loss["tax"]["estimated_tax"])) == (D("-1000.00"), D("-458.00"))
    assert D(loss["waiting_saves"]) == D("-170.00")
    assert D(loss["tax_per_dollar"]) == D("-0.152667")


# ── the wash-sale check (§5) ─────────────────────────────────────────────────


def test_a_loss_with_a_recent_purchase_in_a_taxable_account_is_deferred(
    run_pt: CliRunner, port: str
) -> None:
    """100 bought at 50, worth 40: a 1,000 loss. 30 bought on 2025-02-20 wash
    300 of it, deferred into their basis; the window clears 31 days after."""
    row = next(r for r in _rows(_report(run_pt, port), "WSH") if r["quantity"] == "100")
    wash = row["wash"]
    assert [(r["account"], r["acquired"], r["quantity"], r["disallowed"], r["status"])
            for r in wash["replacements"]] == [
        ("B", "2025-02-20", "30", "300.00", "deferred")
    ]  # fmt: skip
    assert (D(wash["deferred"]), D(wash["permanently_disallowed"])) == (D("300.00"), D("0"))
    assert wash["window_clears"] == "2025-03-23"
    assert wash["buying_washes_through"] == "2025-04-02"
    assert D(row["allowed_gain"]) == D("-700.00")
    assert D(row["tax"]["estimated_tax"]) == D("-320.60")


def test_a_loss_with_a_recent_purchase_in_an_ira_is_flagged(
    run_pt: CliRunner, port: str
) -> None:
    """40 bought in the IRA wash 400 of B's 1,000 loss, and that 400 is lost
    for good (Rev. Rul. 2008-5): shown apart from a deferral, in the table too."""
    payload = _report(run_pt, port)
    wash = _one(payload, "IRW")["wash"]
    assert [(r["account"], r["quantity"], r["status"]) for r in wash["replacements"]] == [
        ("IRA", "40", "permanent")
    ]
    assert (D(wash["permanently_disallowed"]), D(wash["deferred"])) == (D("400.00"), D("0"))
    (table_row,) = [r for r in payload["data"]["rows"] if r["symbol"] == "IRW"]
    assert (D(table_row["wash_lost"]), D(table_row["wash_deferred"])) == (D("400.00"), D("0"))
    shown = run_pt("--port", port, "lot", "candidates", "IRW", "--as-of", AS_OF,
                   fmt="table").stdout  # fmt: skip
    assert "loses the loss for good" in shown


def test_replacement_shares_already_matched_wash_nothing(run_pt: CliRunner, port: str) -> None:
    """PRM's 100 bought on 2025-02-14 already washed the loss of 2025-02-10 and
    were split into their own lot. The other lot, sold at a loss today, has no
    replacement: counting that purchase again would call it washed."""
    rows = _rows(_report(run_pt, port), "PRM")
    july = next(r for r in rows if r["open_date"] == "2024-07-01")
    assert D(july["gain"]) == D("-1000.00")
    assert july["wash"]["replacements"] == []
    assert july["wash"]["window_clears"] is None
    assert D(july["allowed_gain"]) == D("-1000.00")


def test_two_loss_lots_and_one_replacement_are_not_additive(
    run_pt: CliRunner, port: str
) -> None:
    """Each 50-share lot, sold alone, is washed by the same 10 recent shares.
    Sold together only 10 would wash, so the report says the rows do not add."""
    payload = _report(run_pt, port)
    older = [r for r in _rows(payload, "TWO") if r["quantity"] == "50"]
    assert len(older) == 2
    for row in older:
        assert D(row["wash"]["washed_quantity"]) == D("10")
        assert D(row["wash"]["deferred"]) == D("100.00")
    assert payload["data"]["is_additive"] is False
    assert any("do not add up" in note for note in payload["data"]["footnotes"])


# ── nulls, never zeros ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("symbol", "reason"),
    [
        ("STA", "price_stale"),
        ("MIS", "price_missing"),
        ("UNA", "basis_unavailable"),
        ("NRT", "no_rate_schedule"),
    ],
)
def test_what_cannot_be_stated_is_null_with_its_reason(
    run_pt: CliRunner, port: str, symbol: str, reason: str
) -> None:
    row = _one(_report(run_pt, port), symbol)
    assert [u["reason"] for u in row["unstated"]] == [reason]
    assert row["tax"] is None and row["tax_per_dollar"] is None
    assert row["waiting_saves"] is None
    if reason == "no_rate_schedule":
        assert row["unstated"][0]["code"] == "PT-E-TAX-NO-RATE-SCHEDULE"
        # The rest of the row stands: only the tax is unknown.
        assert D(row["gain"]) == D("-2000.00")
    else:
        assert row["gain"] is None and row["wash"] is None
    if reason == "price_stale":
        assert row["price"] == {
            "price": "45", "price_date": "2025-02-01", "source": "file:prices.csv",
            "age_days": 30, "status": "stale", "valuation_level": 1, "is_estimate": False,
        }  # fmt: skip
        assert row["proceeds"] is None
    if reason == "price_missing":
        assert row["price"] is None and row["proceeds"] is None


def test_a_reconstructed_basis_is_computed_and_marked_approximate(
    run_pt: CliRunner, port: str
) -> None:
    payload = _report(run_pt, port)
    row = _one(payload, "REC")
    assert row["is_approximate_basis"] is True
    assert D(row["gain"]) == D("-500.00")
    assert any("approximate (ADR 0017)" in n for n in payload["data"]["footnotes"])


def test_an_averaged_holding_is_named_not_listed(run_pt: CliRunner, port: str) -> None:
    payload = _report(run_pt, port)
    assert _rows(payload, "AVG") == []
    assert payload["data"]["not_listed"]["averaged"] == [
        {
            "account": "D",
            "symbol": "AVG",
            "lots": 1,
            "quantity": "100",
            "reason": "the account's default relief method is average cost",
        }
    ]
    # The IRA's lot and the short lot are counted, not listed.
    assert payload["data"]["not_listed"]["sheltered_lots"] == 1
    assert payload["data"]["not_listed"]["short_lots"] == 1
    assert _rows(payload, "SHT") == []


def test_losses_and_sort_narrow_and_order_the_rows(run_pt: CliRunner, port: str) -> None:
    losses = _report(run_pt, port, "--losses", "--sort", "gain")
    gains = [D(r["gain"]) for r in losses["data"]["candidates"]]
    assert gains and all(g < 0 for g in gains)
    assert gains == sorted(gains)
    ordered = _report(run_pt, port, "--sort", "tax-per-dollar")["data"]["candidates"]
    known = [D(r["tax_per_dollar"]) for r in ordered if r["tax_per_dollar"] is not None]
    assert known == sorted(known)
    # Unknown figures last.
    tail = [r["tax_per_dollar"] for r in ordered[len(known) :]]
    assert tail and all(t is None for t in tail)


# ── the sale path it does not run ────────────────────────────────────────────

#: Every kind of row: a gain and a loss, short- and long-term, a wash deferred
#: in a taxable account and one lost to an IRA.
_SALES = ["LTG", "STG", "STL", "LTL", "WSH", "IRW", "OND", "TWO", "PRM", "REC"]


@pytest.mark.parametrize("symbol", _SALES)
def test_estimated_tax_matches_pt_tax_for_the_same_sale(
    run_pt: CliRunner, port: str, tmp_path: Path, symbol: str
) -> None:
    """ADR 0047's test obligation. Each row is made as the sale it describes:
    `pt --dry-run sell --lots` for what a dry run shows, then the same sale on a
    copy of the file, read back through `pt tax`. A dry run is that write,
    rolled back (ADR 0026), and shows no gain or tax."""
    for row in _rows(_report(run_pt, port), symbol):
        account, designation = row["account"], row["designation"]
        sale = ("sell", symbol, "--qty", row["quantity"], "--price", PRICES[symbol],
                "--date", AS_OF, "-a", account, "--lots", designation)  # fmt: skip
        rehearsed = run_pt("--port", port, "--dry-run", *sale).data
        assert D(rehearsed["gross_amount"]) == D(row["proceeds"])
        assert D(rehearsed["cost_basis_relieved"]) == D(row["basis"])
        assert [r["holding_period"] for r in rehearsed["rows"]] == [row["holding_period"]]

        copy = str(copy_template(Path(port), tmp_path / f"{row['lot_key']}.port"))
        before = run_pt("--port", copy, "tax", "--year", "2025").data["rows"]
        run_pt("--port", copy, *sale)
        after = run_pt("--port", copy, "tax", "--year", "2025").data["rows"]
        (made,) = [r for r in after if r not in before]
        assert (made["date"], made["account"], made["symbol"]) == (AS_OF, account, symbol)
        assert D(made["proceeds"]) == D(row["proceeds"])
        assert D(made["gain"]) == D(row["gain"])
        assert made["holding_period"] == row["holding_period"]
        assert D(made["estimated_tax"]) == D(row["tax"]["estimated_tax"])
        washed = row["wash"]
        disallowed = (
            D(washed["deferred"]) + D(washed["permanently_disallowed"]) if washed else D(0)
        )
        assert D(made["wash_sale_disallowed"]) == disallowed
        assert D(made["allowed_gain"]) == D(row["allowed_gain"])


# ── output ───────────────────────────────────────────────────────────────────


def test_the_sample_bond_is_priced_through_its_quote_basis(run_pt: CliRunner) -> None:
    """The sample's Treasury is held at 50,000 face and quoted at 98.275 percent
    of par: 49,137.50, accrued interest not included (ADR 0041)."""
    from portable_core.lint._common import repo_root

    sample = str(repo_root() / "examples" / "sample.port")
    payload = _report(run_pt, sample, on="2025-06-30")
    _validate(payload)
    bond = next(r for r in payload["data"]["candidates"] if r["instrument_type"] == "bond")
    assert (bond["quantity"], bond["price"]["price"]) == ("50000", "98.275")
    assert D(bond["proceeds"]) == D("49137.50")
    assert any("premium amortization" in n for n in bond["notes"])
    assert any("NOT computed" in n for n in payload["data"]["footnotes"])


def test_the_same_file_and_arguments_give_the_same_bytes(run_pt: CliRunner, port: str) -> None:
    def body() -> str:
        payload = _report(run_pt, port)
        payload.pop("generated_at")
        return json.dumps(payload, sort_keys=True)

    assert body() == body()
    table = ("--port", port, "lot", "candidates", "--as-of", AS_OF)
    assert run_pt(*table, fmt="table").stdout == run_pt(*table, fmt="table").stdout


def test_the_disclaimer_and_wash_sale_lists_travel_with_the_json(
    run_pt: CliRunner, port: str
) -> None:
    from tests.unit.test_json_schemas import _validator

    payload = _report(run_pt, port)
    _validate(payload)
    assert payload["disclaimer"].startswith("This is an estimate, not tax advice")
    assert payload["data"]["wash_sales"]["not_detected"]
    for drop in ("disclaimer",):
        stripped = {k: v for k, v in payload.items() if k != drop}
        assert list(_validator("lot-candidates-1.0.json").iter_errors(stripped))
    stripped = json.loads(json.dumps(payload))
    del stripped["data"]["wash_sales"]["not_detected"]
    assert list(_validator("lot-candidates-1.0.json").iter_errors(stripped))
