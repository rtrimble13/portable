"""`pert twr` end to end: stored snapshots in, a labelled return out. ADR 0040.

Each test builds a small portfolio with `pt`, values it with `pt value`, and
asks `pert` for the return. The figures are worked by hand in the docstrings,
so a change to the engine that moves a number has to explain itself here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import ROOT, CliResult, CliRunner

pytestmark = pytest.mark.integration


@pytest.fixture
def run_pert(tmp_path: Path) -> CliRunner:
    """Invoke `pert`, JSON by default, isolated as `run_pt` is."""

    def run(*args: str, expect: int | None = 0, fmt: str = "json") -> CliResult:
        env = dict(os.environ)
        env |= {
            "PYTHONPATH": str(ROOT / "src"),
            "NO_COLOR": "1",
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
        }
        env.pop("PORTABLE_PORT", None)
        completed = subprocess.run(
            [sys.executable, "-m", "portable_pert", "--format", fmt, *args],
            capture_output=True,
            text=True,
            check=False,
            cwd=tmp_path,
            env=env,
        )
        result = CliResult(completed)
        if expect is not None:
            assert result.returncode == expect, (
                f"exit {result.returncode}, expected {expect}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result

    return run


def _policy(run_pt: CliRunner, port: str) -> None:
    run_pt("--port", port, "policy", "set", "--effective-from", "2024-01-01",
           "--large-flow-pct", "0.10")  # fmt: skip


def _price(run_pt: CliRunner, port: str, on: str, px: str, symbol: str = "XYZ") -> None:
    run_pt("--port", port, "price", "set", symbol, "--price", px, "--date", on,
           "--valuation-level", "1")  # fmt: skip


@pytest.fixture
def invested(run_pt: CliRunner, portfolio: Path) -> str:
    """500,000 deposited on 01-02; 1,000 XYZ bought at 100 on 01-03.

    XYZ closes at 100 on 01-03, 101 on 01-04, 99 on 01-05. Valued 01-02 to 01-05.
    """
    port = str(portfolio)
    _policy(run_pt, port)
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    run_pt("--port", port, "buy", "XYZ", "--qty", "1000", "--price", "100", "--date",
           "2024-01-03", "-a", "B")  # fmt: skip
    for on, px in (("2024-01-03", "100"), ("2024-01-04", "101"), ("2024-01-05", "99")):
        _price(run_pt, port, on, px)
    return port


def _value(run_pt: CliRunner, port: str, last: str = "2024-01-05") -> None:
    run_pt("--port", port, "value", "--from", "2024-01-02", "--to", last)


def _returns(payload: dict[str, Any]) -> dict[str, D]:
    return {entry["basis"]: D(entry["value"]) for entry in payload["data"]["returns"]}


def _twr(
    run_pert: CliRunner, port: str, *extra: str, last: str = "2024-01-05"
) -> dict[str, Any]:
    return run_pert("--port", port, "twr", "--to", last, *extra).json()


@pytest.mark.gips
def test_the_return_chains_the_days(
    run_pt: CliRunner, run_pert: CliRunner, invested: str
) -> None:
    """500,000 funded on 01-02 earns nothing that day. Then 1,000 shares move
    +1,000 and -2,000 on a 500,000 book: 1.002 x (499,000 / 501,000) - 1."""
    _value(run_pt, invested)
    payload = _twr(run_pert, invested)

    expected = D("1.002") * D(499000) / D(501000) - 1
    assert abs(_returns(payload)["net_of_fees"] - expected) < D("1e-28")
    assert payload["data"]["period"]["start"] == "2024-01-02"
    assert payload["data"]["subperiods"]["daily"] == 4
    assert payload["data"]["flow_timing"].startswith("inflows at the start")


@pytest.mark.gips
def test_tearsheet_leads_with_twr(
    run_pt: CliRunner, run_pert: CliRunner, invested: str
) -> None:
    """PORT-GIPS-B01, D01: TWR, net-of-fees first, the disclaimer an envelope field."""
    _value(run_pt, invested)
    payload = _twr(run_pert, invested)

    assert payload["data"]["returns"][0]["basis"] == "net_of_fees"
    assert all(e["method"] == "twr" for e in payload["data"]["returns"])
    assert payload["disclaimer"]
    table = run_pert("--port", invested, "twr", "--to", "2024-01-05", fmt="table").stdout
    assert table.index("Net-of-fees") < table.index("Gross-of-fees")


@pytest.mark.gips
def test_large_flow_threshold_required(
    run_pt: CliRunner, run_pert: CliRunner, portfolio: Path
) -> None:
    """PORT-GIPS-B03: no policy in force is an error, not a zero."""
    port = str(portfolio)
    run_pt("--port", port, "value", "--from", "2024-01-02", "--to", "2024-01-03")
    payload = run_pert("--port", port, "twr", "--to", "2024-01-03", expect=4).json()
    assert payload["error"]["code"] == "PT-E-GIPS-NO-FLOW-POLICY"


@pytest.mark.gips
def test_valuation_frequency_floor(
    run_pt: CliRunner, run_pert: CliRunner, invested: str
) -> None:
    """PORT-GIPS-A03: a day with no snapshot is refused, never interpolated."""
    run_pt("--port", invested, "value", "--from", "2024-01-02", "--to", "2024-01-03")
    run_pt("--port", invested, "value", "--date", "2024-01-05")
    payload = run_pert("--port", invested, "twr", "--to", "2024-01-05", expect=5).json()
    assert payload["error"]["code"] == "PT-E-SNAPSHOT-MISSING"
    assert "2024-01-04" in payload["error"]["remedy"]


@pytest.mark.gips
def test_internal_transfer_is_flow_neutral_at_portfolio_level(
    run_pt: CliRunner, run_pert: CliRunner, invested: str
) -> None:
    """PORT-GIPS-B02: a transfer between the owner's accounts leaves the
    portfolio's return bit-identical and changes both accounts'."""
    run_pt("--port", invested, "account", "add", "--name", "IRA", "--type", "tax_deferred",
           "--opened", "2024-01-02")  # fmt: skip
    _value(run_pt, invested)
    before = _twr(run_pert, invested)
    account_before = _returns(_twr(run_pert, invested, "-a", "B"))

    run_pt("--port", invested, "cash", "transfer", "--from", "B", "--to", "IRA",
           "--amount", "100000", "--date", "2024-01-04")  # fmt: skip
    _value(run_pt, invested)
    after = _twr(run_pert, invested)

    assert _returns(after) == _returns(before)
    assert _returns(_twr(run_pert, invested, "-a", "B")) != account_before
    ira = _returns(_twr(run_pert, invested, "-a", "IRA"))
    assert ira["net_of_fees"] == 0, "cash earning nothing returns nothing"


@pytest.mark.gips
def test_custody_fee_reduces_net_only(
    run_pt: CliRunner, run_pert: CliRunner, invested: str
) -> None:
    """PORT-GIPS-D01: custody is an internal management cost. Gross and
    net-of-external-costs-only are unchanged by it in the sub-period it is
    charged in; net-of-fees is reduced. Charged on the period's last day, so
    that sub-period is the whole of its effect (ADR 0040 §8)."""
    _value(run_pt, invested)
    without = _returns(_twr(run_pert, invested))
    run_pt("--port", invested, "cash", "fee", "-a", "B", "--amount", "250",
           "--fee-class", "internal_mgmt_cost", "--date", "2024-01-05")  # fmt: skip
    _value(run_pt, invested)
    with_fee = _returns(_twr(run_pert, invested))

    assert with_fee["gross_of_fees"] == without["gross_of_fees"]
    assert with_fee["net_of_external_costs_only"] == without["net_of_external_costs_only"]
    assert with_fee["net_of_fees"] < without["net_of_fees"]


@pytest.mark.gips
def test_other_admin_fee_reduces_no_gips_basis(
    run_pt: CliRunner, run_pert: CliRunner, invested: str
) -> None:
    """PORT-GIPS-D01: a wire fee changes value and none of the three bases in
    the sub-period it is charged in. Only the supplemental net-of-all-costs
    figure moves."""
    _value(run_pt, invested)
    without = _returns(_twr(run_pert, invested))
    run_pt("--port", invested, "cash", "fee", "-a", "B", "--amount", "30",
           "--fee-class", "other_admin", "--date", "2024-01-05")  # fmt: skip
    _value(run_pt, invested)
    with_fee = _returns(_twr(run_pert, invested))

    for basis in ("net_of_fees", "net_of_external_costs_only", "gross_of_fees"):
        assert with_fee[basis] == without[basis], basis
    assert with_fee["net_of_all_costs"] < without["net_of_all_costs"]


@pytest.mark.gips
def test_margin_interest_reduces_every_basis(
    run_pt: CliRunner, run_pert: CliRunner, invested: str
) -> None:
    _value(run_pt, invested)
    without = _returns(_twr(run_pert, invested))
    run_pt("--port", invested, "cash", "margin-interest", "-a", "B", "--amount", "40",
           "--date", "2024-01-04")  # fmt: skip
    _value(run_pt, invested)
    payload = _twr(run_pert, invested)

    assert all(_returns(payload)[b] < without[b] for b in without)
    assert payload["data"]["disclosures"]["margin_interest_deducted_at_every_basis"]


@pytest.mark.gips
def test_method_recorded_per_subperiod(
    run_pt: CliRunner, run_pert: CliRunner, portfolio: Path
) -> None:
    """PORT-GIPS-B05, B06: an unpriced day is bridged by Modified Dietz, and
    the result says which sub-period was which."""
    port = str(portfolio)
    _policy(run_pt, port)
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    run_pt("--port", port, "buy", "XYZ", "--qty", "1000", "--price", "100", "--date",
           "2024-01-03", "-a", "B")  # fmt: skip
    # Priced on 01-03, then not again until 01-10: 01-09 is beyond the 5-day
    # tolerance, so that day's snapshot is incomplete.
    _price(run_pt, port, "2024-01-03", "100")
    _price(run_pt, port, "2024-01-10", "102")
    run_pt("--port", port, "value", "--from", "2024-01-02", "--to", "2024-01-10")
    payload = run_pert("--port", port, "twr", "--to", "2024-01-10", "--daily").json()

    methods = [(s["after"], s["through"], s["method"]) for s in payload["data"]["series"]]
    assert ("2024-01-08", "2024-01-10", "modified_dietz") in methods
    assert payload["data"]["subperiods"]["modified_dietz"] == 1


@pytest.mark.gips
def test_a_gap_across_a_large_flow_is_refused(
    run_pt: CliRunner, run_pert: CliRunner, portfolio: Path
) -> None:
    """PORT-GIPS-B03: the valuation a large flow requires is missing."""
    port = str(portfolio)
    _policy(run_pt, port)
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    run_pt("--port", port, "buy", "XYZ", "--qty", "1000", "--price", "100", "--date",
           "2024-01-03", "-a", "B")  # fmt: skip
    _price(run_pt, port, "2024-01-03", "100")
    _price(run_pt, port, "2024-01-10", "102")
    run_pt("--port", port, "cash", "deposit", "-a", "B", "--amount", "200000",
           "--date", "2024-01-09")  # fmt: skip
    run_pt("--port", port, "value", "--from", "2024-01-02", "--to", "2024-01-10")
    payload = run_pert("--port", port, "twr", "--to", "2024-01-10", expect=4).json()

    assert payload["error"]["code"] == "PT-E-GIPS-VALUATION-GAP"
    assert "large flow" in payload["error"]["message"]


def test_a_stale_snapshot_is_refused(
    run_pt: CliRunner, run_pert: CliRunner, invested: str
) -> None:
    """ADR 0024: a price changed after the day was valued."""
    _value(run_pt, invested)
    _price(run_pt, invested, "2024-01-04", "105")
    payload = run_pert("--port", invested, "twr", "--to", "2024-01-05", expect=4).json()
    assert payload["error"]["code"] == "PT-E-SNAPSHOT-STALE"


def test_the_output_validates_against_its_schema(
    run_pt: CliRunner, run_pert: CliRunner, invested: str
) -> None:
    from tests.unit.test_json_schemas import _validator

    _value(run_pt, invested)
    payload = run_pert("--port", invested, "twr", "--to", "2024-01-05", "--daily").json()
    errors = sorted(_validator("twr-1.0.json").iter_errors(payload), key=str)
    assert not errors, [e.message for e in errors[:5]]
    assert json.dumps(payload)  # nothing in it a float could have produced
