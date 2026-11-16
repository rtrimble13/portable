"""`pert twr` on the committed `examples/sample.port`, checked against hand arithmetic.

The sample ships with month-end prices and stored snapshots (issue 59), so the
headline command runs on it with no step before it. Running is not enough: a
return that comes out is only worth something if it is the right one. So two
periods are worked here from first principles -- quantities and cash from the
generator's ledger, prices from `examples/sample_prices.csv` -- without
reading a snapshot, and the engine must agree with them to the last digit.

`pert` never writes a portfolio, but opening one can leave a `-shm` beside it,
so every test reads a copy of the committed file.
"""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
from decimal import Decimal as D
from decimal import localcontext
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import ROOT, CliResult

pytestmark = pytest.mark.integration

SAMPLE = ROOT / "examples" / "sample.port"
PRICES = ROOT / "examples" / "sample_prices.csv"

#: Every level the sample can be asked for.
LEVELS = [None, "Taxable Brokerage", "Rollover IRA", "Roth IRA"]


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    copy = tmp_path / "sample.port"
    shutil.copyfile(SAMPLE, copy)
    return copy


def _run(cli: str, tmp_path: Path, *args: str) -> CliResult:
    env = dict(os.environ)
    env |= {
        "PYTHONPATH": str(ROOT / "src"),
        "NO_COLOR": "1",
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
    }
    env.pop("PORTABLE_PORT", None)
    completed = subprocess.run(
        [sys.executable, "-m", cli, *args],
        capture_output=True,
        encoding="utf-8",
        check=False,
        cwd=tmp_path,
        env=env,
    )
    return CliResult(completed)


def _twr(sample: Path, tmp_path: Path, *args: str) -> dict[str, Any]:
    result = _run("portable_pert", tmp_path, "--port", str(sample), "--format", "json", *args)
    return result.ok().data


def _net_of_fees(data: dict[str, Any]) -> D:
    (row,) = [r for r in data["returns"] if r["basis"] == "net_of_fees"]
    return D(row["value"])


def _price(symbol: str, on: str) -> D:
    with PRICES.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if (row["symbol"], row["date"]) == (symbol, on):
                return D(row["price"])
    raise AssertionError(f"examples/sample_prices.csv has no {symbol} on {on}")


@pytest.mark.parametrize("as_of", ["2025-06-30", "2025-12-31"])
@pytest.mark.parametrize("account", LEVELS, ids=lambda a: a or "portfolio")
def test_pert_twr_runs_on_the_committed_sample(
    sample: Path, tmp_path: Path, account: str | None, as_of: str
) -> None:
    """Issue 59: from inception, at every level, with nothing run before it.

    Both methods are exercised: daily sub-periods where the sample is priced,
    and Modified Dietz across the unpriced days within each month.
    """
    args = ["--as-of", as_of, "twr"] + (["--account", account] if account else [])
    data = _twr(sample, tmp_path, *args)
    assert data["period"]["start"] == "2021-01-04"
    assert data["period"]["end"] == as_of
    assert data["subperiods"]["daily"] > 0
    assert data["subperiods"]["modified_dietz"] > 0


def test_a_month_without_flows_is_the_change_in_value(sample: Path, tmp_path: Path) -> None:
    """Roth IRA, March 2022: no flow, so the return is V_E / V_B - 1 (PORT-GIPS-B04).

    The Roth was funded with 40,000.00 on 2021-01-04 and bought 150 AAPL at
    128.10 with a 1.00 commission, leaving 20,784.00 of cash; nothing else
    touched it before 2023. So its value on a day is 150 x AAPL + 20,784.00:

        2022-02-28   150 x 169.85 + 20,784.00 = 46,261.50
        2022-03-31   150 x 165.79 + 20,784.00 = 45,652.50
        return       45,652.50 / 46,261.50 - 1 = -1.3164%

    The month is linked from daily sub-periods and one Modified Dietz bridge.
    With no flow, every one telescopes, so the chain must equal the direct
    ratio exactly.
    """
    shares, cash = D("150"), D("20784.00")
    begin = shares * _price("AAPL", "2022-02-28") + cash
    end = shares * _price("AAPL", "2022-03-31") + cash
    with localcontext() as ctx:
        ctx.prec = 34
        expected = end / begin - 1

    data = _twr(
        sample, tmp_path, "--as-of", "2022-03-31", "twr", "--from", "2022-03-01",
        "--account", "Roth IRA",
    )  # fmt: skip
    assert _net_of_fees(data) == expected


def test_a_valued_flow_day_takes_the_flow_out_of_the_return(
    sample: Path, tmp_path: Path
) -> None:
    """Rollover IRA, January 2022: a 7,000.00 deposit on Saturday 01-15.

    The sample is priced on the trading day before every external flow, so the
    deposit falls between two values and the month is a true time-weighted
    return (PORT-GIPS-B01, B03). The IRA held 600 VTI and, at 2021-12-31,
    63,407.00 of cash: 180,000.00 rolled in, less 118,267.00 for the VTI,
    plus 1,674.00 of 2021 distributions.

        2021-12-31   600 x 241.05 + 63,407.00 = 208,037.00
        2022-01-14   600 x 239.33 + 63,407.00 = 207,005.00
        2022-01-15   600 x 239.33 + 70,407.00 = 214,005.00   (deposit in)
        2022-01-31   600 x 237.24 + 70,407.00 = 212,751.00
        return       (207,005 / 208,037) x (212,751 / 214,005) - 1 = -1.0791%

    Had 01-14 been left unpriced, one Modified Dietz bridge would have spanned
    the deposit and given -1.0752%: close, and wrong.
    """
    shares = D("600")
    before, after = D("63407.00"), D("70407.00")
    with localcontext() as ctx:
        ctx.prec = 34
        v_start = shares * _price("VTI", "2021-12-31") + before
        v_eve = shares * _price("VTI", "2022-01-14") + before
        v_flow = shares * _price("VTI", "2022-01-14") + after
        v_end = shares * _price("VTI", "2022-01-31") + after
        expected = (v_eve / v_start) * (v_end / v_flow) - 1

    data = _twr(
        sample, tmp_path, "--as-of", "2022-01-31", "twr", "--from", "2022-01-01",
        "--account", "Rollover IRA",
    )  # fmt: skip
    assert _net_of_fees(data) == expected


def test_the_committed_sample_validates(sample: Path, tmp_path: Path) -> None:
    """Its stored snapshots re-derive from the ledger and their recorded prices (ADR 0024)."""
    args = ["--port", str(sample), "--format", "json", "validate"]
    result = _run("portable_pt", tmp_path, *args)
    data = result.ok().data
    assert data["problems"] == 0, data
