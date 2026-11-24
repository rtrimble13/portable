"""`pert twr --estimate` and `pert periods --estimate`, end to end. ADR 0044.

A carried price values a day only where the measured return is refused for
want of a price, and only the days the bridge needs. Each test builds a
portfolio whose gaps are known, and works the figures by hand in its
docstring. Account B is funded with 500,000 on 01-02 by the base template.
"""

from __future__ import annotations

import copy
import decimal
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from portable_core.services.returns import RETURN_CONTEXT
from tests.integration.conftest import CliRunner, build_template, stack_template

pytestmark = pytest.mark.integration


def _setup(run_pt: CliRunner, port: str, *prices: tuple[str, str]) -> None:
    """100 XYZ bought at 100 on 01-03, priced on *prices*: 10,000 of 500,000."""
    run_pt("--port", port, "policy", "set", "--effective-from", "2024-01-01",
           "--large-flow-pct", "0.10")  # fmt: skip
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    run_pt("--port", port, "buy", "XYZ", "--qty", "100", "--price", "100", "--date",
           "2024-01-03", "-a", "B")  # fmt: skip
    for on, px in prices:
        run_pt("--port", port, "price", "set", "XYZ", "--price", px, "--date", on,
               "--valuation-level", "1")  # fmt: skip


def _build_month_end(run_pt: CliRunner, portfolio: Path) -> str:
    """XYZ priced 01-03 at 100, 01-24 at 102 and 02-05 at 104; valued to 02-05.

    At the five-day tolerance 01-09 to 01-23 are unpriced, a gap inside
    January that Modified Dietz bridges, measured. 01-30 to 02-04 are
    unpriced too, and that gap spans the month end 01-31, which the bridge
    may not cross.
    """
    port = str(portfolio)
    _setup(run_pt, port, ("2024-01-03", "100"), ("2024-01-24", "102"), ("2024-02-05", "104"))
    return port


def _build_large_inflow(run_pt: CliRunner, portfolio: Path) -> str:
    """XYZ unpriced 01-09 to 01-14, with 200,000 deposited on 01-12; priced after.

    Prices: 01-03 at 100, 01-15 at 101, then 01-19, 01-24 and 01-29 at 101,
    101 and 102, 02-02 at 103 and 02-05 at 104, so every day from 01-15 on
    is priced within five days, 01-31 included. The deposit is 40% of the
    value, large under the 10% policy, and it falls inside the one gap.
    """
    port = str(portfolio)
    _setup(run_pt, port, ("2024-01-03", "100"), ("2024-01-15", "101"), ("2024-01-19", "101"),
           ("2024-01-24", "102"), ("2024-01-29", "102"), ("2024-02-02", "103"),
           ("2024-02-05", "104"))  # fmt: skip
    run_pt("--port", port, "cash", "deposit", "-a", "B", "--amount", "200000",
           "--date", "2024-01-12")  # fmt: skip
    return port


@pytest.fixture(scope="module")
def month_end_template(
    tmp_path_factory: pytest.TempPathFactory, portfolio_template: Path
) -> Path:
    return build_template(tmp_path_factory, _build_month_end, start_from=portfolio_template)


@pytest.fixture
def month_end(month_end_template: Path, portfolio: Path) -> str:
    return str(stack_template(month_end_template, portfolio))


@pytest.fixture(scope="module")
def large_inflow_template(
    tmp_path_factory: pytest.TempPathFactory, portfolio_template: Path
) -> Path:
    return build_template(tmp_path_factory, _build_large_inflow, start_from=portfolio_template)


@pytest.fixture
def large_inflow(large_inflow_template: Path, portfolio: Path) -> str:
    return str(stack_template(large_inflow_template, portfolio))


def _value(run_pt: CliRunner, port: str, last: str) -> None:
    run_pt("--port", port, "value", "--from", "2024-01-02", "--to", last)


def _net(payload: dict[str, Any]) -> D:
    return D(payload["data"]["returns"][0]["value"])


def _over(numerator: int, denominator: int) -> D:
    """A quotient as the engine computes it, in RETURN_CONTEXT."""
    with decimal.localcontext(RETURN_CONTEXT):
        return D(numerator) / D(denominator)


def _days(estimate: dict[str, Any]) -> list[str]:
    return [day["date"] for day in estimate["days"]]


@pytest.mark.gips
def test_only_the_month_end_the_bridge_needs_is_estimated(
    run_pt: CliRunner, run_pert: CliRunner, month_end: str
) -> None:
    """ADR 0044 §1. Refused without the flag, for the month end. With it, only
    01-31 is valued, at 102 carried from 01-24, seven days old:
    490,000 + 100 x 102 = 500,200. Each side of it is bridged as before.

    01-08 to 01-24 is 200 on 500,000; 01-29 to 01-31 is nothing; 01-31 to
    02-05 is 200 on 500,200. Linked: 1.0004 x 500,400 / 500,200 - 1 = 0.0008.
    """
    _value(run_pt, month_end, "2024-02-05")
    refused = run_pert("--port", month_end, "twr", "--to", "2024-02-05", expect=4).json()
    assert refused["error"]["code"] == "PT-E-GIPS-VALUATION-GAP"
    assert "month end 2024-01-31" in refused["error"]["message"]

    payload = run_pert("--port", month_end, "twr", "--to", "2024-02-05", "--estimate", "0.05",
                       "--daily").json()  # fmt: skip
    data = payload["data"]
    assert _net(payload) == D("0.0008")
    assert all(entry["is_estimated"] is True for entry in data["returns"])
    estimate = data["estimate"]
    assert _days(estimate) == ["2024-01-31"]
    (day,) = estimate["days"]
    assert day["value"] == "500200.00"
    assert D(day["share"]) == _over(10200, 500200)
    (holding,) = day["holdings"]
    assert (holding["symbol"], holding["price"], holding["price_date"], holding["age_days"],
            holding["source"], holding["value"]) == (
        "XYZ", "102", "2024-01-24", 7, "price", "10200.00",
    )  # fmt: skip
    assert (estimate["limit"], estimate["oldest_age_days"], estimate["peak"]["date"]) == (
        "0.05",
        7,
        "2024-01-31",
    )

    bridged = [(s["after"], s["through"], s["is_estimated"])
               for s in data["series"] if s["method"] == "modified_dietz"]  # fmt: skip
    assert bridged == [
        ("2024-01-08", "2024-01-24", False),
        ("2024-01-29", "2024-01-31", True),
        ("2024-01-31", "2024-02-05", True),
    ]
    assert not any(s["is_estimated"] for s in data["series"] if s["method"] == "daily")


@pytest.mark.gips
def test_an_estimate_is_marked_in_the_title_the_footnote_and_on_stderr(
    run_pt: CliRunner, run_pert: CliRunner, month_end: str
) -> None:
    """ADR 0044 §2: the title begins ESTIMATED, every figure says so, and the
    sentence that says how much was carried, against what limit and how old,
    is a footnote and a warning."""
    _value(run_pt, month_end, "2024-02-05")
    shown = run_pert("--port", month_end, "twr", "--to", "2024-02-05", "--estimate", "0.05",
                     fmt="table")  # fmt: skip
    sentence = (
        "ESTIMATED, not measured, and not part of the track record: 1 day(s) valued with 1 "
        "carried holding(s), at most 2.04% of a day's value, on 2024-01-31, against a limit "
        "of 5.00%. The oldest price carried was 7 days old (ADR 0044)."
    )
    assert shown.stdout.startswith("ESTIMATED time-weighted return, portfolio,")
    assert "8.0 bps (TWR, net-of-fees, ESTIMATED)" in shown.stdout
    assert sentence in shown.stdout
    assert f"warning: {sentence}" in shown.stderr

    from tests.unit.test_json_schemas import _validator

    payload = run_pert("--port", month_end, "twr", "--to", "2024-02-05", "--estimate", "0.05",
                       "--daily").json()  # fmt: skip
    assert payload["warnings"] == [sentence]
    schema = _validator("twr-2.0.json")
    errors = sorted(schema.iter_errors(payload), key=str)
    assert not errors, [e.message for e in errors[:5]]

    # The schema will not take an estimate with a mark dropped.
    unmarked = copy.deepcopy(payload)
    unmarked["data"]["returns"][0]["is_estimated"] = False
    dropped = copy.deepcopy(payload)
    del dropped["data"]["estimate"]
    hidden = copy.deepcopy(payload)
    hidden["data"]["estimate"] = None
    for broken in (unmarked, dropped, hidden):
        assert not schema.is_valid(broken)


@pytest.mark.gips
def test_the_flag_alone_changes_nothing(
    run_pt: CliRunner, run_pert: CliRunner, month_end: str
) -> None:
    """ADR 0044 §2. Through 01-29 the return is measurable, a Modified Dietz
    bridge included, and the flag leaves it byte for byte as it was. So does
    a limit the period could not meet if anything were estimated."""
    _value(run_pt, month_end, "2024-02-05")
    for fmt in ("table", "markdown", "csv"):
        plain = run_pert("--port", month_end, "twr", "--to", "2024-01-29", fmt=fmt)
        flagged = run_pert("--port", month_end, "twr", "--to", "2024-01-29", "--estimate",
                           "0.0001", fmt=fmt)  # fmt: skip
        assert (flagged.stdout, flagged.stderr) == (plain.stdout, plain.stderr), fmt
    plain_json = run_pert("--port", month_end, "twr", "--to", "2024-01-29", "--daily").json()
    flagged_json = run_pert("--port", month_end, "twr", "--to", "2024-01-29", "--daily",
                            "--estimate", "0.0001").json()  # fmt: skip
    assert flagged_json["data"] == plain_json["data"]
    assert flagged_json["warnings"] == plain_json["warnings"] == []
    assert plain_json["data"]["estimate"] is None
    assert plain_json["data"]["subperiods"]["modified_dietz"] == 1


@pytest.mark.gips
def test_a_day_over_the_limit_is_refused(
    run_pt: CliRunner, run_pert: CliRunner, month_end: str
) -> None:
    """ADR 0044 §4: 10,200 of 500,200 is 2.04%, over a limit of 1%."""
    _value(run_pt, month_end, "2024-02-05")
    error = run_pert("--port", month_end, "twr", "--to", "2024-02-05", "--estimate", "0.01",
                     expect=4).json()["error"]  # fmt: skip
    assert error["code"] == "PT-E-ESTIMATE-LIMIT"
    assert (error["context"]["day"], error["context"]["limit"]) == ("2024-01-31", "0.01")
    assert D(error["context"]["share"]) == _over(10200, 500200)
    assert error["context"]["holdings"] == ["XYZ"]


@pytest.mark.gips
def test_a_large_inflow_inside_a_gap_is_valued_around(
    run_pt: CliRunner, run_pert: CliRunner, large_inflow: str
) -> None:
    """ADR 0044 §1, ADR 0040 §6. The deposit of 01-12 counts from the start
    of its day, so it needs 01-11 valued: 490,000 + 100 x 100 = 500,000. The
    stretch from 01-11 then has the deposit on its first day, which ADR 0040
    §6 still calls inside it, so 01-12 is valued too: 700,000. 01-08 to
    01-11 and 01-11 to 01-12 return nothing, and 01-12 to 01-15 is 100 on
    700,000.
    """
    _value(run_pt, large_inflow, "2024-01-15")
    refused = run_pert("--port", large_inflow, "twr", "--to", "2024-01-15", expect=4).json()
    assert "large flow" in refused["error"]["message"]

    payload = run_pert("--port", large_inflow, "twr", "--to", "2024-01-15", "--estimate",
                       "0.05", "--daily").json()  # fmt: skip
    # Linked through growths, a quotient keeps 30 of its 34 digits.
    assert abs(_net(payload) - _over(100, 700000)) < D("1e-30")
    estimate = payload["data"]["estimate"]
    assert _days(estimate) == ["2024-01-11", "2024-01-12"]
    assert [day["value"] for day in estimate["days"]] == ["500000.00", "700000.00"]
    assert [h["age_days"] for day in estimate["days"] for h in day["holdings"]] == [8, 9]
    cuts = [(s["after"], s["through"], s["method"]) for s in payload["data"]["series"]
            if s["after"] >= "2024-01-08"]  # fmt: skip
    assert cuts == [
        ("2024-01-08", "2024-01-11", "modified_dietz"),
        ("2024-01-11", "2024-01-12", "daily"),
        ("2024-01-12", "2024-01-15", "modified_dietz"),
    ]


@pytest.mark.gips
def test_a_dividend_inside_the_carry_is_counted_once(
    run_pt: CliRunner, run_pert: CliRunner, month_end: str
) -> None:
    """ADR 0044 §1. 50.00 on the 100 units, ex 01-30 and paid 02-02. On 01-31
    the snapshot holds it as a receivable, so the carry is 102 - 0.50:
    490,000 + 50 + 10,150 = 500,200, as without the dividend. 02-05 is
    490,050 + 10,400. Carried at 102, 01-31 would read 500,250 and the
    dividend would be counted twice."""
    run_pt("--port", month_end, "income", "dividend", "XYZ", "-a", "B", "--amount", "50.00",
           "--ex-date", "2024-01-30", "--pay-date", "2024-02-02")  # fmt: skip
    _value(run_pt, month_end, "2024-02-05")
    payload = run_pert("--port", month_end, "twr", "--to", "2024-02-05", "--estimate",
                       "0.05").json()  # fmt: skip
    (day,) = payload["data"]["estimate"]["days"]
    (holding,) = day["holdings"]
    assert (day["value"], holding["price"], holding["last_known_price"],
            holding["lowered_by"]) == ("500200.00", "101.50", "102", "0.50")  # fmt: skip
    with decimal.localcontext(RETURN_CONTEXT):
        assert _net(payload) == D("1.0004") * (1 + D(250) / D(500200)) - 1


@pytest.mark.gips
def test_a_price_older_than_five_days_is_estimated_on_request(
    run_pt: CliRunner, run_pert: CliRunner, month_end: str
) -> None:
    """ADR 0044 §1. With staleness_tolerance_days at 10, `pt value` calls
    02-01 complete on XYZ's 01-24 price, eight days old, which `pert` will
    not measure with. The estimate carries the same price, and says so."""
    ten = {"PORTABLE_STALENESS_TOLERANCE_DAYS": "10"}
    run_pt("--port", month_end, "value", "--from", "2024-01-02", "--to", "2024-02-01",
           extra_env=ten)  # fmt: skip
    error = run_pert("--port", month_end, "twr", "--to", "2024-02-01", expect=5,
                     extra_env=ten).json()["error"]  # fmt: skip
    assert error["code"] == "PT-E-PRICE-STALE"
    payload = run_pert("--port", month_end, "twr", "--to", "2024-02-01", "--estimate", "0.05",
                       extra_env=ten).json()  # fmt: skip
    estimate = payload["data"]["estimate"]
    assert _days(estimate) == ["2024-01-31", "2024-02-01"]
    assert {h["age_days"] for day in estimate["days"] for h in day["holdings"]} == {7, 8}
    assert _net(payload) == D("0.0004")


@pytest.mark.gips
def test_an_estimate_supplies_a_price_and_nothing_else(
    run_pt: CliRunner, run_pert: CliRunner, month_end: str
) -> None:
    """ADR 0044 §1. A missing snapshot is refused with the flag, as without
    it: `pt value` makes it, and nothing is estimated in its place."""
    _value(run_pt, month_end, "2024-02-03")
    for extra in ((), ("--estimate", "0.05")):
        error = run_pert("--port", month_end, "twr", "--to", "2024-02-05", *extra,
                         expect=5).json()["error"]  # fmt: skip
        assert error["code"] == "PT-E-SNAPSHOT-MISSING"


@pytest.mark.gips
def test_an_unrecorded_coupon_is_not_estimated(
    run_pt: CliRunner, run_pert: CliRunner, portfolio: Path
) -> None:
    """ADR 0044 §1, ADR 0041 §3. The 11-15 coupon is not in the ledger, so
    every day from 11-15 is incomplete. That is a missing row, not a missing
    price, and the flag does not estimate it."""
    port = str(portfolio)
    run_pt("--port", port, "policy", "set", "--effective-from", "2024-01-01",
           "--large-flow-pct", "0.10")  # fmt: skip
    run_pt("--port", port, "instrument", "add", "T-4.25-2030", "--type", "bond",
           "--issuer", "US Treasury", "--coupon", "0.0425", "--coupon-frequency", "2",
           "--maturity", "2030-05-15", "--face", "1000")  # fmt: skip
    run_pt("--port", port, "buy", "T-4.25-2030", "--qty", "10000", "--price", "100",
           "--date", "2024-05-15", "--accrued-interest", "0", "-a", "B")  # fmt: skip
    run_pt("--port", port, "price", "set", "T-4.25-2030", "--price", "100", "--date",
           "2024-11-14", "--valuation-level", "1")  # fmt: skip
    run_pt("--port", port, "value", "--from", "2024-11-14", "--to", "2024-11-18")
    error = run_pert("--port", port, "twr", "--from", "2024-11-15", "--to", "2024-11-18",
                     "--estimate", "0.5", expect=4).json()["error"]  # fmt: skip
    assert error["code"] == "PT-E-GIPS-VALUATION-GAP"
    assert "is not estimated" in error["message"]
    assert "coupon" in error["message"]


@pytest.mark.parametrize("given", ["0", "-0.05", "1.5", "5%", "NaN", "Infinity"])
def test_the_limit_is_a_share_above_zero_and_at_most_one(
    run_pert: CliRunner, month_end: str, given: str
) -> None:
    """ADR 0044 §4: required with the flag, and no default."""
    error = run_pert("--port", month_end, "twr", "--to", "2024-02-05", "--estimate", given,
                     expect=2).json()["error"]  # fmt: skip
    assert "--estimate must be a share" in error["message"]


# ── pert periods ─────────────────────────────────────────────────────────────


@pytest.mark.gips
def test_each_period_is_estimated_or_not_on_its_own(
    run_pt: CliRunner, run_pert: CliRunner, large_inflow: str
) -> None:
    """ADR 0044 §3. Through 02-05, month to date is measured: 700,200 on
    01-31 to 700,400, and nothing it needs is unpriced. It reads the same
    with the flag, unmarked. Year to date and since inception cross the
    deposit's gap, so without the flag they are null with the refusal, and
    with it they are estimated, from 01-11 and 01-12."""
    from tests.unit.test_json_schemas import _validator

    _value(run_pt, large_inflow, "2024-02-05")
    plain = run_pert("--port", large_inflow, "periods", "--to", "2024-02-05").json()
    payload = run_pert("--port", large_inflow, "periods", "--to", "2024-02-05", "--estimate",
                       "0.05").json()  # fmt: skip
    schema = _validator("periods-1.0.json")
    errors = sorted(schema.iter_errors(payload), key=str)
    assert not errors, [e.message for e in errors[:5]]
    index = next(n for n, r in enumerate(payload["data"]["summary"]) if r["estimate"])
    unmarked = copy.deepcopy(payload)
    unmarked["data"]["summary"][index]["returns"][0]["cumulative"]["is_estimated"] = False
    assert not schema.is_valid(unmarked)

    rows = {row["label"]: row for row in payload["data"]["summary"]}
    before = {row["label"]: row for row in plain["data"]["summary"]}
    month = rows["Month to date"]
    assert month == before["Month to date"]
    assert month["estimate"] is None
    # 100 on 700,200 on 02-02 and 100 on 700,300 on 02-05, linked.
    assert abs(D(month["returns"][0]["cumulative"]["value"]) - _over(200, 700200)) < D("1e-30")

    for label in ("Year to date", "Since inception"):
        assert before[label]["refusal"]["code"] == "PT-E-GIPS-VALUATION-GAP"
        row = rows[label]
        assert _days(row["estimate"]) == ["2024-01-11", "2024-01-12"]
        assert row["returns"][0]["cumulative"]["is_estimated"] is True
    assert any(w.startswith("Year to date: ESTIMATED") for w in payload["warnings"])

    shown = run_pert("--port", large_inflow, "periods", "--to", "2024-02-05", "--estimate",
                     "0.05", fmt="table").stdout  # fmt: skip
    assert shown.startswith("ESTIMATED where marked: time-weighted returns, portfolio")
    assert "(TWR, net-of-fees, ESTIMATED)" in shown


@pytest.mark.gips
def test_a_period_ending_on_an_estimated_day_ends_at_its_estimated_value(
    run_pt: CliRunner, run_pert: CliRunner, large_inflow: str
) -> None:
    """PORT-GIPS-H03 under ADR 0044: through 01-12, the day is valued with its
    carried price, 690,000 + 10,000, and that is the ending value shown."""
    _value(run_pt, large_inflow, "2024-01-15")
    payload = run_pert("--port", large_inflow, "periods", "--to", "2024-01-12", "--estimate",
                       "0.05").json()  # fmt: skip
    since = next(r for r in payload["data"]["summary"] if r["label"] == "Since inception")
    assert (since["ending_value"], since["accounts_holding_value"]) == ("700000.00", 1)
    assert _days(since["estimate"]) == ["2024-01-11", "2024-01-12"]


def test_periods_with_nothing_to_estimate_are_unchanged_by_the_flag(
    run_pt: CliRunner, run_pert: CliRunner, month_end: str
) -> None:
    _value(run_pt, month_end, "2024-02-05")
    for fmt in ("table", "json"):
        plain = run_pert("--port", month_end, "periods", "--to", "2024-01-29", fmt=fmt)
        flagged = run_pert("--port", month_end, "periods", "--to", "2024-01-29", "--estimate",
                           "0.0001", fmt=fmt)  # fmt: skip
        if fmt == "json":
            assert flagged.json()["data"] == plain.json()["data"]
        else:
            assert (flagged.stdout, flagged.stderr) == (plain.stdout, plain.stderr)
