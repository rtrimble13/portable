"""`pt account update` and `pt instrument update`: what may change, and what is refused.

Issue 38. Reference data is corrected in place, because it is not the ledger.
But a column that replay or valuation reads, with no effective date, would
restate history if it changed, and an account's name is part of every
imported row's identity (ADR 0033). Those are refused, each with its reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import CliRunner

pytestmark = pytest.mark.integration


def _account(run_pt: CliRunner, port: str) -> dict[str, Any]:
    return run_pt("--port", port, "account", "show", "B").data


def test_an_accounts_description_is_changed_and_cleared(
    run_pt: CliRunner, portfolio: Path
) -> None:
    port = str(portfolio)
    data = run_pt("--port", port, "account", "update", "B", "--custodian", "Acme",
                  "--alias", "acct-ending-01", "--note", "the brokerage").data  # fmt: skip
    assert data["changed"] == {
        "account_alias": {"was": None, "now": "acct-ending-01"},
        "custodian": {"was": None, "now": "Acme"},
        "note": {"was": None, "now": "the brokerage"},
    }
    shown = _account(run_pt, port)
    assert (shown["custodian"], shown["alias"], shown["note"]) == (
        "Acme",
        "acct-ending-01",
        "the brokerage",
    )

    cleared = run_pt("--port", port, "account", "update", "B", "--note", "").data
    assert cleared["changed"] == {"note": {"was": "the brokerage", "now": None}}
    assert _account(run_pt, port)["note"] is None


@pytest.mark.parametrize(
    ("flag", "code", "why"),
    [
        (["--name", "Brokerage"], "PT-E-VALIDATION", "ADR 0033"),
        (["--relief-method", "lifo"], "PT-E-RESTATES-HISTORY", "re-relieve"),
        (["--cash-treatment", "operating"], "PT-E-RESTATES-HISTORY", "PORT-GIPS-A07"),
        (["--allows-fractional"], "PT-E-RESTATES-HISTORY", "cash in lieu"),
    ],
)
def test_a_field_that_would_rewrite_history_is_refused(
    run_pt: CliRunner, portfolio: Path, flag: list[str], code: str, why: str
) -> None:
    port = str(portfolio)
    before = _account(run_pt, port)
    error = run_pt("--port", port, "account", "update", "B", *flag, "--note", "x",
                   expect=4).json()["error"]  # fmt: skip
    assert error["code"] == code
    assert why in error["message"]
    assert _account(run_pt, port) == before, "nothing is written, the note included"


def test_restating_a_refused_field_at_its_value_is_no_change(
    run_pt: CliRunner, portfolio: Path
) -> None:
    """Account B's default is FIFO: saying so again changes nothing, so refuses nothing."""
    data = run_pt("--port", str(portfolio), "account", "update", "B", "--relief-method",
                  "fifo", "--note", "x").data  # fmt: skip
    assert data["changed"] == {"note": {"was": None, "now": "x"}}


def test_a_dry_run_keeps_nothing(run_pt: CliRunner, portfolio: Path) -> None:
    port = str(portfolio)
    data = run_pt("--port", port, "--dry-run", "account", "update", "B", "--note", "x").data
    assert data["changed"] == {"note": {"was": None, "now": "x"}}
    assert _account(run_pt, port)["note"] is None


def test_an_update_naming_nothing_is_refused(run_pt: CliRunner, portfolio: Path) -> None:
    run_pt("--port", str(portfolio), "account", "update", "B", expect=4)
    run_pt("--port", str(portfolio), "instrument", "add", "XYZ")
    run_pt("--port", str(portfolio), "instrument", "update", "XYZ", expect=4)


def test_an_instrument_is_corrected_and_found_by_its_new_identifier(
    run_pt: CliRunner, portfolio: Path
) -> None:
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", "XYZ", "--name", "Xyz Crop")
    data = run_pt("--port", port, "instrument", "update", "XYZ", "--name", "Xyz Corp",
                  "--cusip", "123456789", "--asset-class", "us_equity").data  # fmt: skip
    assert data["changed"] == {
        "asset_class": {"was": None, "now": "us_equity"},
        "cusip": {"was": None, "now": "123456789"},
        "name": {"was": "Xyz Crop", "now": "Xyz Corp"},
    }
    found = run_pt("--port", port, "instrument", "show", "123456789").data
    assert (found["symbol"], found["name"]) == ("XYZ", "Xyz Corp")

    same = run_pt("--port", port, "instrument", "update", "XYZ", "--name", "Xyz Corp").json()
    assert same["data"]["changed"] == {}
    assert same["warnings"] == ["every value given is the one already recorded"]


def test_an_identifier_another_instrument_holds_is_refused(
    run_pt: CliRunner, portfolio: Path
) -> None:
    """Held twice, a CUSIP on a statement could resolve to either instrument."""
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", "ABC", "--cusip", "111111111")
    run_pt("--port", port, "instrument", "add", "XYZ")
    error = run_pt("--port", port, "instrument", "update", "XYZ", "--cusip", "111111111",
                   expect=4).json()["error"]  # fmt: skip
    assert "already identifies ABC" in error["message"]
    assert run_pt("--port", port, "instrument", "show", "XYZ").data["cusip"] is None
