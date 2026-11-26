"""`pt allocation drift` on the committed `examples/sample.port`, by hand. ADR 0048.

The sample declares two classes beside `cash`, classifies every instrument
from inception, and sets one policy: `us_equity` 0.55 ± 0.05, `us_treasury`
0.10 ± 0.05, `cash` 0.35 ± 0.03. Two class values on 2025-12-31 are worked
here from quantities in the generator's ledger and prices in
`examples/sample_prices.csv`, without reading a snapshot. The total they are
weighed against is what `pt mark` values the accounts at that day.
"""

from __future__ import annotations

import shutil
from decimal import Decimal as D
from decimal import localcontext
from pathlib import Path

import pytest

from tests.integration.test_sample_returns import SAMPLE, _price, _run
from tests.unit.test_json_schemas import _validator

pytestmark = pytest.mark.integration

ON = "2025-12-31"
CENT = D("0.01")


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    """A copy: opening the committed file can leave a `-shm` beside it."""
    copy = tmp_path / "sample.port"
    shutil.copyfile(SAMPLE, copy)
    return copy


def test_two_class_weights_on_the_sample_by_hand(sample: Path, tmp_path: Path) -> None:
    """On 2025-12-31.

    `us_treasury` is the Taxable Brokerage's 50,000 face of T-4.25-2030 and
    the interest accrued on it: 46 of the 181 days from the 15 November coupon
    to the next, ACT/ACT.

        50,000 x 99.14 / 100                = 49,570.00
        50,000 x 0.0425 / 2 x 46 / 181      =    270.03
                                              49,840.03

    `us_equity` is every share of AAPL, VTI, ACME and NEWCO, each line valued
    to the cent: the Roth's 30.95 VTI, reinvested, is 9,657.9475, so 9,657.95.

        AAPL   100 + 150 + 300 = 550 x 228.60        = 125,730.00
        VTI    140,422.50 + 9,657.95 + 156,025.00     = 306,105.45
        ACME   600 x 31.20                            =  18,720.00
        NEWCO  150 x 15.40                            =   2,310.00
                                                       452,865.45
    """
    args = ["--port", str(sample), "--format", "json", "--as-of", ON]
    payload = _run("portable_pt", tmp_path, *args, "allocation", "drift").ok().json()
    errors = sorted(_validator("allocation-drift-1.0.json").iter_errors(payload), key=str)
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors[:5]]
    data = payload["data"]
    rows = {row["asset_class"]: row for row in data["rows"]}

    with localcontext() as ctx:
        ctx.prec = 34
        face = D("50000")
        clean = (face * _price("T-4.25-2030", ON) / 100).quantize(CENT)
        accrued = (face * D("0.0425") / 2 * 46 / 181).quantize(CENT)
        treasury = clean + accrued
        vti = _price("VTI", ON)
        equity = (
            D(550) * _price("AAPL", ON)
            + sum(((q * vti).quantize(CENT) for q in (D(450), D("30.95"), D(500))), D(0))
            + D(600) * _price("ACME", ON)
            + D(150) * _price("NEWCO", ON)
        )
        marked = _run("portable_pt", tmp_path, *args, "mark").ok().data["rows"]
        total = sum((D(row["ending"]) for row in marked), D(0))
        expected = {"us_treasury": treasury / total, "us_equity": equity / total}

    assert (treasury, equity) == (D("49840.03"), D("452865.45"))
    assert D(data["total_market_value"]) == total
    assert D(rows["us_treasury"]["value"]) == treasury
    assert D(rows["us_equity"]["value"]) == equity
    for name, weight in expected.items():
        assert D(rows[name]["weight"]) == weight, name
    # 6.08% against 0.10 ± 0.05, and 55.25% against 0.55 ± 0.05: both inside.
    assert (rows["us_treasury"]["status"], rows["us_equity"]["status"]) == ("inside", "inside")
    assert data["is_complete"] is True
