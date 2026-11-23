"""A track record ends where nothing is held, and is never linked across. ADR 0045 §4.

`PORT-GIPS-H06`: when a portfolio holds nothing and later restarts, the record
before and the record after are two records. Linking them puts the days with
nothing invested into the return as though they had earned zero. These are
the three cases ADR 0045 found `pert twr` getting wrong, and the two moves a
period makes instead of linking across the gap.

The figures are worked by hand. B is funded with 500,000 on 01-02 (the base
template), buys 5,000 XYZ at 100 on 01-03, and sells them at 110 on 01-04: up
10%. It withdraws all 550,000 on 01-05 and holds nothing until 500,000 is
deposited and invested at 100 on 01-22. XYZ closes 01-31 at 90: down 10%.
"""

from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import (
    CliRunner,
    build_template,
    copy_template,
    stack_template,
)
from tests.unit.test_json_schemas import _validator

pytestmark = pytest.mark.integration

LEVELS = [None, "B"]


def _build_stints(run_pt: CliRunner, portfolio: Path) -> None:
    port = str(portfolio)
    run_pt("--port", port, "policy", "set", "--effective-from", "2024-01-01",
           "--large-flow-pct", "0.10")  # fmt: skip
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    for verb, qty, px, on in (
        ("buy", "5000", "100", "2024-01-03"),
        ("sell", "5000", "110", "2024-01-04"),
    ):
        run_pt("--port", port, verb, "XYZ", "--qty", qty, "--price", px, "--date", on,
               "-a", "B")  # fmt: skip
    run_pt("--port", port, "cash", "withdraw", "-a", "B", "--amount", "550000",
           "--date", "2024-01-05")  # fmt: skip
    run_pt("--port", port, "cash", "deposit", "-a", "B", "--amount", "500000",
           "--date", "2024-01-22")  # fmt: skip
    run_pt("--port", port, "buy", "XYZ", "--qty", "5000", "--price", "100", "--date",
           "2024-01-22", "-a", "B")  # fmt: skip
    prices = [("2024-01-03", "100"), ("2024-01-04", "110")]
    prices += [(f"2024-01-{day}", "100") for day in ("22", "23", "24", "25", "26", "29", "30")]
    prices += [("2024-01-31", "90")]
    for on, px in prices:
        run_pt("--port", port, "price", "set", "XYZ", "--price", px, "--date", on,
               "--valuation-level", "1")  # fmt: skip
    run_pt("--port", port, "value", "--from", "2024-01-02", "--to", "2024-01-31")


def _build_beside(run_pt: CliRunner, portfolio: Path) -> None:
    """The same, with an account C holding 1,000 cash all month."""
    port = str(portfolio)
    run_pt("--port", port, "account", "add", "--name", "C", "--type", "taxable",
           "--opened", "2024-01-02", "--relief-method", "fifo")  # fmt: skip
    run_pt("--port", port, "cash", "deposit", "-a", "C", "--amount", "1000",
           "--date", "2024-01-02")  # fmt: skip
    run_pt("--port", port, "value", "--from", "2024-01-02", "--to", "2024-01-31")


@pytest.fixture(scope="module")
def stints_template(tmp_path_factory: pytest.TempPathFactory, portfolio_template: Path) -> Path:
    return build_template(tmp_path_factory, _build_stints, start_from=portfolio_template)


@pytest.fixture(scope="module")
def beside_template(tmp_path_factory: pytest.TempPathFactory, stints_template: Path) -> Path:
    return build_template(tmp_path_factory, _build_beside, start_from=stints_template)


@pytest.fixture
def stints(stints_template: Path, portfolio: Path) -> str:
    return str(stack_template(stints_template, portfolio))


def _twr(
    run_pert: CliRunner, port: str, level: str | None, *window: str, expect: int = 0
) -> dict[str, Any]:
    scope = ("--account", level) if level is not None else ()
    payload = run_pert("--port", port, "twr", *window, *scope, expect=expect).json()
    if expect == 0:
        errors = sorted(_validator("twr-2.0.json").iter_errors(payload), key=str)
        assert not errors, [e.message for e in errors[:5]]
    return payload


def _net(payload: dict[str, Any]) -> D:
    [net] = [r for r in payload["data"]["returns"] if r["basis"] == "net_of_fees"]
    return D(net["value"])


@pytest.mark.gips
@pytest.mark.parametrize("level", LEVELS, ids=["portfolio", "account"])
def test_track_record_break_not_linked(
    run_pert: CliRunner, stints: str, level: str | None
) -> None:
    """PORT-GIPS-H06. Across the gap, 1.10 x 0.90 - 1 = -1.00% would be printed
    for two records, one up 10% and one down 10%. It is refused, and the remedy
    names each side, which is measured on its own."""
    for window in (("--from", "2024-01-02", "--to", "2024-01-31"), ("--to", "2024-01-31")):
        error = _twr(run_pert, stints, level, *window, expect=4)["error"]
        assert error["code"] == "PT-E-RETURN-BREAK"
        assert error["context"]["termination"] == "2024-01-05"
        assert error["context"]["restart"] == "2024-01-22"
        assert "`--to 2024-01-05`" in error["remedy"]
        assert "`--from 2024-01-22`" in error["remedy"]

    before = _twr(run_pert, stints, level, "--to", "2024-01-05")
    after = _twr(run_pert, stints, level, "--from", "2024-01-22", "--to", "2024-01-31")
    assert (_net(before), _net(after)) == (D("0.1"), D("-0.1"))


@pytest.mark.gips
@pytest.mark.parametrize("level", LEVELS, ids=["portfolio", "account"])
def test_port_gips_h06_nothing_held_is_refused_not_zero(
    run_pt: CliRunner, run_pert: CliRunner, stints: str, level: str | None
) -> None:
    """01-08 to 01-19 holds nothing. It has no sub-period, and the empty product
    used to print as 0.00% (invariant 10)."""
    error = _twr(run_pert, stints, level, "--from", "2024-01-08", "--to", "2024-01-19",
                 expect=4)["error"]  # fmt: skip
    assert error["code"] == "PT-E-RETURN-NOTHING-HELD"
    assert (error["context"]["first"], error["context"]["last"]) == ("2024-01-08", "2024-01-19")

    table = run_pert("--port", stints, "twr", "--from", "2024-01-08", "--to", "2024-01-19",
                     fmt="table", expect=4)  # fmt: skip
    assert "0.00%" not in table.stdout + table.stderr


@pytest.mark.gips
@pytest.mark.parametrize("level", LEVELS, ids=["portfolio", "account"])
def test_port_gips_h02_a_period_past_the_record_ends_where_it_ended(
    run_pert: CliRunner, stints: str, level: str | None
) -> None:
    """Asked for 01-02 to 01-19, the return is the record's: 01-02 to 01-05, up
    10%, labelled with the dates it covers, the requested end beside them."""
    payload = _twr(run_pert, stints, level, "--from", "2024-01-02", "--to", "2024-01-19")
    period = payload["data"]["period"]
    assert (period["start"], period["end"], period["requested_end"]) == (
        "2024-01-02",
        "2024-01-05",
        "2024-01-19",
    )
    assert period["days"] == 4
    assert period["nothing_held_before"] is None
    assert period["nothing_held_after"] == {"from": "2024-01-06", "to": "2024-01-19"}
    assert _net(payload) == D("0.1")
    # Every return is labelled with the period it covers, not the one asked for.
    assert {r["period_end"] for r in payload["data"]["returns"]} == {"2024-01-05"}
    assert payload["as_of"] == "2024-01-19"

    table = run_pert("--port", stints, "twr", "--from", "2024-01-02", "--to", "2024-01-19",
                     fmt="table").stdout  # fmt: skip
    assert "2024-01-02 to 2024-01-05 (4 days, cumulative)" in table
    assert "Ends on 2024-01-05, where the track record ended" in table


@pytest.mark.gips
@pytest.mark.parametrize("level", LEVELS, ids=["portfolio", "account"])
def test_a_period_starting_with_nothing_held_starts_where_the_record_begins_again(
    run_pert: CliRunner, stints: str, level: str | None
) -> None:
    """Asked for 01-10 to 01-31: nothing is held until 01-22, so the return is
    01-22 to 01-31, down 10%, the requested start beside it."""
    payload = _twr(run_pert, stints, level, "--from", "2024-01-10", "--to", "2024-01-31")
    period = payload["data"]["period"]
    assert (period["requested_start"], period["start"], period["end"]) == (
        "2024-01-10",
        "2024-01-22",
        "2024-01-31",
    )
    assert period["nothing_held_before"] == {"from": "2024-01-10", "to": "2024-01-21"}
    assert period["nothing_held_after"] is None
    assert _net(payload) == D("-0.1")


@pytest.mark.gips
def test_port_gips_h06_an_account_holding_value_beside_it_is_no_break(
    run_pert: CliRunner, beside_template: Path, tmp_path: Path
) -> None:
    """With C holding 1,000 all month, the portfolio never holds nothing, so its
    January is one record. B's still breaks: the rule is the same at both
    levels, and a portfolio day holds nothing only when every account does."""
    port = str(copy_template(beside_template, tmp_path / "beside.port"))
    payload = _twr(run_pert, port, None, "--from", "2024-01-02", "--to", "2024-01-31")
    period = payload["data"]["period"]
    assert (period["start"], period["end"]) == ("2024-01-02", "2024-01-31")
    assert period["nothing_held_before"] is None and period["nothing_held_after"] is None

    error = _twr(run_pert, port, "B", "--from", "2024-01-02", "--to", "2024-01-31",
                 expect=4)["error"]  # fmt: skip
    assert error["code"] == "PT-E-RETURN-BREAK"
