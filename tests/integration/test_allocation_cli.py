"""`pt allocation`: declared classes, an effective-dated policy, and drift. ADR 0048.

Account B is funded with 500,000 on 2024-01-02 by the base template. Here it
buys, on 2024-01-03:
- 1,000 VTI at 200, classified `equity`: 200,000;
- 2,000 BND at 75, classified `bonds`: 150,000;
- 1,000 BAL at 100, a balanced fund classified 60% `equity` and 40% `bonds`:
  60,000 and 40,000.

That leaves 50,000 of cash. Priced unchanged on 2024-06-28, the portfolio is
500,000:
- `equity` 260,000, a weight of 0.52;
- `bonds` 190,000, 0.38;
- `cash` 50,000, 0.10.

The policy from 2024-01-02 is `equity` 0.55 ± 0.03, `bonds` 0.40 ± 0.05 and
`cash` 0.05 ± 0.02. So `equity` is 3 points under, exactly on the edge of
its band and inside it; `bonds` 2 points under, inside; and `cash` 5 points
over, above. An IRA, opened 2024-01-02, holds nothing.
"""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from tests.integration import test_holdings_valuation_cli as valuation
from tests.integration.conftest import CliRunner, build_template, stack_template

pytestmark = pytest.mark.integration

PRICED = "2024-06-28"


def _build(run_pt: CliRunner, portfolio: Path) -> str:
    port = str(portfolio)

    def pt(*args: str) -> None:
        run_pt("--port", port, *args)

    pt("account", "add", "--name", "IRA", "--type", "tax_deferred", "--opened", "2024-01-02",
       "--relief-method", "fifo")  # fmt: skip
    pt("allocation", "class", "add", "equity", "--description", "Stocks")
    pt("allocation", "class", "add", "bonds", "--description", "Bonds")
    for symbol, price in (("VTI", "200"), ("BND", "75"), ("BAL", "100")):
        pt("instrument", "add", symbol, "--type", "etf")
        pt("price", "set", symbol, "--price", price, "--date", PRICED)
    pt("--as-of", "2024-01-02", "allocation", "classify", "VTI", "equity")
    pt("--as-of", "2024-01-02", "allocation", "classify", "BND", "bonds")
    pt("--as-of", "2024-01-02", "allocation", "classify", "BAL", "equity=0.6", "bonds=0.4")
    pt("buy", "VTI", "--qty", "1000", "--price", "200", "--date", "2024-01-03", "-a", "B")
    pt("buy", "BND", "--qty", "2000", "--price", "75", "--date", "2024-01-03", "-a", "B")
    pt("buy", "BAL", "--qty", "1000", "--price", "100", "--date", "2024-01-03", "-a", "B")
    pt("allocation", "target", "set", "equity=0.55:0.03", "bonds=0.40:0.05",
       "cash=0.05:0.02", "--effective-from", "2024-01-02")  # fmt: skip
    return port


@pytest.fixture(scope="module")
def allocated_template(
    tmp_path_factory: pytest.TempPathFactory, portfolio_template: Path
) -> Path:
    return build_template(tmp_path_factory, _build, start_from=portfolio_template)


@pytest.fixture
def allocated(allocated_template: Path, portfolio: Path) -> str:
    return str(stack_template(allocated_template, portfolio))


def _drift(run_pt: CliRunner, port: str, on: str, *extra: str) -> dict[str, Any]:
    payload: dict[str, Any] = run_pt(
        "--port", port, "--as-of", on, "allocation", "drift", *extra
    ).json()
    from tests.unit.test_json_schemas import _validator

    errors = sorted(_validator("allocation-drift-1.0.json").iter_errors(payload), key=str)
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors[:5]]
    data: dict[str, Any] = payload["data"]
    return data


def _classes(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["asset_class"]: row for row in data["rows"]}


def _values(data: dict[str, Any]) -> dict[str, D]:
    return {name: D(row["value"]) for name, row in _classes(data).items()}


def test_drift_against_the_policy_by_hand(run_pt: CliRunner, allocated: str) -> None:
    data = _drift(run_pt, allocated, PRICED)
    assert D(data["total_market_value"]) == D("500000.00")
    assert _values(data) == {"cash": D(50000), "bonds": D(190000), "equity": D(260000)}
    rows = _classes(data)
    assert {n: D(r["weight"]) for n, r in rows.items()} == {
        "cash": D("0.10"),
        "bonds": D("0.38"),
        "equity": D("0.52"),
    }
    assert {n: (D(r["difference"]), r["status"]) for n, r in rows.items()} == {
        "cash": (D("0.05"), "above"),
        "bonds": (D("-0.02"), "inside"),
        "equity": (D("-0.03"), "inside"),
    }
    assert data["outside_band"] == ["cash"]
    assert (data["is_complete"], data["is_estimated"], data["estimate"]) == (True, False, None)
    assert data["policy"]["effective_from"] == "2024-01-02"
    assert data["classified"] == [
        {"symbol": symbol, "effective_from": "2024-01-02"} for symbol in ("BAL", "BND", "VTI")
    ]


def test_a_split_classification_puts_each_class_its_weight(
    run_pt: CliRunner, allocated: str
) -> None:
    """BAL is 60/40: 60,000 of its 100,000 is equity, 40,000 bonds."""
    holdings = run_pt("--port", allocated, "--as-of", PRICED, "holdings", "--by",
                      "asset-class").data  # fmt: skip
    grouped = {row["symbol"]: D(row["market_value"]) for row in holdings["rows"]}
    assert grouped == {"cash": D(50000), "bonds": D(190000), "equity": D(260000)}
    shown = run_pt("--port", allocated, "--as-of", PRICED, "instrument", "show", "BAL").data
    assert shown["classification"] == {
        "effective_from": "2024-01-02",
        "shares": [
            {"asset_class": "bonds", "weight": "0.4"},
            {"asset_class": "equity", "weight": "0.6"},
        ],
    }


def test_the_band_edge_is_inside_and_a_cent_beyond_it_is_outside(
    run_pt: CliRunner, allocated: str
) -> None:
    assert _classes(_drift(run_pt, allocated, PRICED))["equity"]["status"] == "inside"
    run_pt("--port", allocated, "cash", "deposit", "-a", "B", "--amount", "0.01", "--date",
           PRICED)  # fmt: skip
    equity = _classes(_drift(run_pt, allocated, PRICED))["equity"]
    # 260,000 of 500,000.01: a hair under 0.52, so more than 3 points under.
    assert D(equity["weight"]) < D("0.52")
    assert equity["status"] == "below"


def test_policy_allocation_is_effective_dated(run_pt: CliRunner, allocated: str) -> None:
    """A policy from 2024-07-01 does not change drift as of 2024-06-30."""
    before = _drift(run_pt, allocated, "2024-06-30")
    run_pt("--port", allocated, "allocation", "target", "set", "equity=0.50:0.05",
           "bonds=0.45:0.05", "cash=0.05:0.02", "--effective-from", "2024-07-01")  # fmt: skip
    assert _drift(run_pt, allocated, "2024-06-30") == before
    after = _classes(_drift(run_pt, allocated, "2024-07-01"))
    assert (after["equity"]["target"], after["equity"]["status"]) == ("0.50", "inside")
    assert (after["bonds"]["target"], after["bonds"]["status"]) == ("0.45", "below")


def test_drift_as_of_a_past_date_uses_the_holdings_and_policy_of_that_date(
    run_pt: CliRunner, allocated: str
) -> None:
    """On 2024-08-01 B sells half its VTI, BAL's glide path moves it to 50/50,
    and a new policy starts. None of it reaches back to June."""
    june = _drift(run_pt, allocated, PRICED)
    port = allocated

    def pt(*args: str) -> None:
        run_pt("--port", port, *args)

    pt("sell", "VTI", "--qty", "500", "--price", "200", "--date", "2024-08-01", "-a", "B")
    for symbol, price in (("VTI", "200"), ("BND", "75"), ("BAL", "100")):
        pt("price", "set", symbol, "--price", price, "--date", "2024-08-01")
    pt("--as-of", "2024-08-01", "allocation", "classify", "BAL", "equity=0.5", "bonds=0.5")
    pt("allocation", "target", "set", "equity=0.30:0.05", "bonds=0.40:0.05",
       "cash=0.30:0.05", "--effective-from", "2024-08-01")  # fmt: skip

    assert _drift(run_pt, port, PRICED) == june
    august = _drift(run_pt, port, "2024-08-01")
    # 100,000 of VTI and 150,000 of cash; BAL is 50,000 of each class.
    assert _values(august) == {"cash": D(150000), "bonds": D(200000), "equity": D(150000)}
    assert {r["asset_class"]: r["status"] for r in august["rows"]} == {
        "cash": "inside",
        "bonds": "inside",
        "equity": "inside",
    }
    assert august["policy"]["effective_from"] == "2024-08-01"
    assert {"symbol": "BAL", "effective_from": "2024-08-01"} in august["classified"]


def test_unclassified_holdings_are_named_not_bucketed(
    run_pt: CliRunner, allocated: str
) -> None:
    """5,000 of NEWCO, classified as nothing: in the total, in no class."""
    run_pt("--port", allocated, "instrument", "add", "NEWCO")
    run_pt("--port", allocated, "buy", "NEWCO", "--qty", "100", "--price", "50", "--date",
           "2024-06-27", "-a", "B")  # fmt: skip
    run_pt("--port", allocated, "price", "set", "NEWCO", "--price", "50", "--date", PRICED)
    data = _drift(run_pt, allocated, PRICED)
    assert D(data["total_market_value"]) == D("500000.00")
    assert _values(data) == {"cash": D(45000), "bonds": D(190000), "equity": D(260000)}
    assert data["unclassified"] == [
        {"account": "B", "symbol": "NEWCO", "value": "5000.00", "weight": "0.01"}
    ]
    assert data["is_complete"] is False
    (b,) = data["accounts"]
    assert D(b["unclassified"]) == D(5000)
    table = run_pt("--port", allocated, "--as-of", PRICED, "allocation", "drift",
                   fmt="table").stdout  # fmt: skip
    assert "Not classified, so in no class but in the total: NEWCO in B (5,000.00)" in table


def test_a_class_held_with_no_target_is_shown_and_leaves_the_report_incomplete(
    run_pt: CliRunner, allocated: str
) -> None:
    run_pt("--port", allocated, "allocation", "class", "add", "gold", "--description", "Gold")
    run_pt("--port", allocated, "--as-of", "2024-01-02", "allocation", "classify", "BND",
           "gold", "--corrects")  # fmt: skip
    data = _drift(run_pt, allocated, PRICED)
    gold = _classes(data)["gold"]
    assert (gold["value"], gold["target"], gold["status"]) == ("150000.00", None, "no_target")
    assert data["no_target"] == ["gold"]
    assert data["is_complete"] is False
    # The class the policy names and nothing holds is still a row, at zero.
    assert D(_classes(data)["bonds"]["value"]) == D(40000)


def test_an_internal_transfer_moves_no_portfolio_weight(
    run_pt: CliRunner, allocated: str
) -> None:
    """40,000 from B to the IRA: B's contribution to cash falls by 0.08 and
    the IRA's rises by as much; no portfolio weight moves."""
    before = _drift(run_pt, allocated, PRICED)
    run_pt("--port", allocated, "cash", "transfer", "--from", "B", "--to", "IRA", "--amount",
           "40000", "--date", "2024-06-27")  # fmt: skip
    after = _drift(run_pt, allocated, PRICED)
    assert after["rows"] == before["rows"]
    assert after["total_market_value"] == before["total_market_value"]
    contribution = {
        (a["account"], c["asset_class"]): D(c["contribution"])
        for a in after["accounts"]
        for c in a["classes"]
    }
    assert contribution[("IRA", "cash")] == D("0.08")
    assert contribution[("B", "cash")] == D("0.02")
    for row in after["rows"]:
        assert sum(
            (v for (_, name), v in contribution.items() if name == row["asset_class"]), D(0)
        ) == D(row["weight"])
    ira = run_pt("--port", allocated, "--as-of", PRICED, "allocation", "drift", "-a",
                 "IRA").data  # fmt: skip
    assert [a["account"] for a in ira["accounts"]] == ["IRA"]
    assert ira["rows"] == after["rows"]


def test_a_missing_policy_is_an_error_not_a_zero(run_pt: CliRunner, allocated: str) -> None:
    error = run_pt("--port", allocated, "--as-of", "2024-01-01", "allocation", "drift",
                   expect=4).json()["error"]  # fmt: skip
    assert error["code"] == "PT-E-ALLOCATION-NO-POLICY"
    run_pt("--port", allocated, "--as-of", "2024-01-01", "allocation", "target", "show",
           expect=4)  # fmt: skip


@pytest.mark.parametrize(
    "args",
    [
        ("classify", "VTI", "equity=0.6", "bonds=0.3", "--effective-from", "2024-02-01"),
        ("classify", "VTI", "equity=0.6", "--effective-from", "2024-02-01"),
        ("classify", "VTI", "equity=1.5", "bonds=-0.5", "--effective-from", "2024-02-01"),
        ("target", "set", "equity=0.55:0.03", "bonds=0.40:0.05", "--effective-from",
         "2024-02-01"),
        ("target", "set", "equity=0.60:0.03", "bonds=0.40:0.05", "cash=0.05:0.02",
         "--effective-from", "2024-02-01"),
    ],
)  # fmt: skip
def test_weights_that_do_not_sum_to_one_are_refused(
    run_pt: CliRunner, allocated: str, args: tuple[str, ...]
) -> None:
    error = run_pt("--port", allocated, "allocation", *args, expect=4).json()["error"]
    assert error["code"] == "PT-E-ALLOCATION-WEIGHTS"


def test_a_class_is_declared_before_it_is_used(run_pt: CliRunner, allocated: str) -> None:
    error = run_pt("--port", allocated, "allocation", "classify", "VTI", "stocks",
                   expect=4).json()["error"]  # fmt: skip
    assert error["code"] == "PT-E-ALLOCATION-UNKNOWN-CLASS"
    assert error["context"]["known"] == ["bonds", "cash", "equity"]


def test_a_row_entered_wrongly_is_corrected_on_its_date_and_kept(
    run_pt: CliRunner, allocated: str
) -> None:
    """ADR 0025: one row per date, never edited; --corrects replaces it."""
    port = allocated
    run_pt("--port", port, "--as-of", "2024-01-02", "allocation", "classify", "VTI", "bonds",
           expect=4)  # fmt: skip
    run_pt("--port", port, "--as-of", "2024-01-02", "allocation", "classify", "VTI", "bonds",
           "--corrects")  # fmt: skip
    assert _values(_drift(run_pt, port, PRICED))["bonds"] == D(390000)
    run_pt("--port", port, "allocation", "target", "set", "equity=0.5:0.1", "bonds=0.5:0.1",
           "--effective-from", "2024-03-01", "--corrects", expect=4)  # fmt: skip
    run_pt("--port", port, "allocation", "target", "set", "equity=0.25:0.05",
           "bonds=0.70:0.10", "cash=0.05:0.02", "--effective-from", "2024-01-02",
           "--corrects")  # fmt: skip
    shown = run_pt("--port", port, "--as-of", PRICED, "allocation", "target", "show").data
    assert [(h["policy_id"], h["corrects_policy_id"]) for h in shown["history"]] == [
        (1, None),
        (2, 1),
    ]
    assert shown["policy_id"] == 2
    # VTI is now bonds: 390,000 of 500,000, 8 points over 0.70 and inside 0.10.
    rows = _classes(_drift(run_pt, port, PRICED))
    assert (rows["bonds"]["target"], rows["bonds"]["status"]) == ("0.70", "inside")


def test_a_dry_run_writes_nothing(run_pt: CliRunner, allocated: str) -> None:
    run_pt("--port", allocated, "--dry-run", "allocation", "class", "add", "gold",
           "--description", "Gold")  # fmt: skip
    run_pt("--port", allocated, "--dry-run", "allocation", "target", "set", "equity=1:0",
           "--effective-from", "2024-03-01")  # fmt: skip
    classes = run_pt("--port", allocated, "allocation", "class", "list").data["rows"]
    assert [c["name"] for c in classes] == ["cash", "bonds", "equity"]
    shown = run_pt("--port", allocated, "--as-of", "2024-03-01", "allocation", "target",
                   "show").data  # fmt: skip
    assert shown["effective_from"] == "2024-01-02"


def test_an_unpriced_holding_is_refused_unless_estimated(
    run_pt: CliRunner, allocated: str
) -> None:
    """On 2024-07-10 BAL's newest price is 12 days old, beyond the five-day
    tolerance: every weight is unknown, so drift is refused, unless its price
    is carried (ADR 0044), within the share of the total --estimate allows."""
    on = "2024-07-10"
    for symbol, price in (("VTI", "200"), ("BND", "75")):
        run_pt("--port", allocated, "price", "set", symbol, "--price", price, "--date", on)
    error = run_pt("--port", allocated, "--as-of", on, "allocation", "drift",
                   expect=5).json()["error"]  # fmt: skip
    assert error["code"] == "PT-E-ALLOCATION-UNPRICED"
    assert "BAL in B" in error["message"]

    # BAL carried at 100 is 100,000 of 500,000: a fifth.
    error = run_pt("--port", allocated, "--as-of", on, "allocation", "drift", "--estimate",
                   "0.1", expect=5).json()["error"]  # fmt: skip
    assert error["code"] == "PT-E-ALLOCATION-UNPRICED"
    assert D(error["context"]["carried_share"]) == D("0.2")

    data = _drift(run_pt, allocated, on, "--estimate", "0.2")
    assert data["is_estimated"] is True
    assert data["estimate"]["carried"] == [
        {
            "account": "B",
            "symbol": "BAL",
            "quantity": "1000",
            "price": "100",
            "price_date": PRICED,
            "age_days": 12,
            "value": "100000.00",
        }
    ]
    assert _values(data) == {"cash": D(50000), "bonds": D(190000), "equity": D(260000)}
    table = run_pt("--port", allocated, "--as-of", on, "allocation", "drift", "--estimate",
                   "0.2", fmt="table").stdout  # fmt: skip
    assert "ESTIMATED: valued at carried prices" in table


def test_drift_is_deterministic(run_pt: CliRunner, allocated: str) -> None:
    def run(fmt: str) -> str:
        out = run_pt("--port", allocated, "--as-of", PRICED, "allocation", "drift",
                     fmt=fmt).stdout  # fmt: skip
        return "\n".join(line for line in out.splitlines() if "generated_at" not in line)

    for fmt in ("json", "table", "csv", "markdown"):
        assert run(fmt) == run(fmt)


# ── valued as `pt mark` values it ────────────────────────────────────────────


@pytest.fixture(scope="module")
def accrued_template(
    tmp_path_factory: pytest.TempPathFactory, portfolio_template: Path
) -> Path:
    def build(run_pt: CliRunner, portfolio: Path) -> str:
        port = valuation._build(run_pt, portfolio)
        run_pt("--port", port, "allocation", "target", "set", "equity=0.10:0.05",
               "fixed_income=0.10:0.05", "cash=0.80:0.10",
               "--effective-from", "2024-01-02")  # fmt: skip
        return port

    return build_template(tmp_path_factory, build, start_from=portfolio_template)


@pytest.fixture
def accrued(accrued_template: Path, portfolio: Path) -> str:
    return str(stack_template(accrued_template, portfolio))


def test_the_total_is_the_snapshot_market_value(run_pt: CliRunner, accrued: str) -> None:
    """Each account's total is the ending market value `pt mark` gives, a
    bond's accrued interest in the bond's class and a declared dividend in
    cash (ADR 0048 §4). See test_holdings_valuation_cli for the figures."""
    data = _drift(run_pt, accrued, valuation.ON)
    marked = valuation._marked(run_pt, accrued)
    assert {a["account"]: D(a["total"]) for a in data["accounts"]} == {"B": marked["B"]}
    assert D(data["total_market_value"]) == sum(marked.values()) == D("499572.94")
    assert _values(data) == {
        "cash": D("483833.27") + D("25.00"),
        "equity": D("5500.00"),
        "fixed_income": D("10150.00") + D("64.67"),
    }
    # OP's operating cash is left out, as `pt mark` leaves it out (PORT-GIPS-A07),
    assert data["excluded_operating_cash"] == [{"account": "OP", "amount": "1000.00"}]
    # and CVR, which nothing quotes, is held at zero and classified as nothing.
    assert data["unclassified"] == [
        {"account": "B", "symbol": "CVR", "value": "0.00", "weight": "0"}
    ]


def test_accrued_interest_cannot_be_carried(run_pt: CliRunner, accrued: str) -> None:
    """After a coupon the ledger does not record, the bond's accrued interest
    is unknown, and a carried price cannot stand in for it."""
    for extra in ((), ("--estimate", "1")):
        error = run_pt("--port", accrued, "--as-of", valuation.AFTER_COUPON, "allocation",
                       "drift", *extra, expect=5).json()["error"]  # fmt: skip
        assert error["code"] == "PT-E-ALLOCATION-UNPRICED"
        assert "pt income coupon" in error["remedy"]
