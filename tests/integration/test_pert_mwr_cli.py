"""`pert mwr` end to end: the money-weighted return beside the time-weighted. ADR 0046.

Account B is funded with 500,000 on 2024-01-02 by the base template, and here
buys 5,000 XYZ at 100 the same day. XYZ is priced every day from a file: 100
to 2024-06-30, then 110. So B is worth 500,000 to the end of June and
550,000 after, and every rate below can be worked by hand. Where one is
solved with flows inside, the test solves it again by plain bisection, written
here, and compares.
"""

from __future__ import annotations

import copy
import decimal
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from portable_core.domain.periods import years
from portable_core.services.returns import RETURN_CONTEXT
from tests.integration.conftest import CliRunner, build_template, stack_template

pytestmark = pytest.mark.integration

FIRST, LAST = date(2024, 1, 2), date(2025, 1, 10)
STEP = date(2024, 7, 1)
ANCHOR = date(2024, 1, 1)
#: A rate computed through exp and ln keeps 28 of RETURN_CONTEXT's 34 digits.
_EXACT = D("1e-28")


def _build_year(run_pt: CliRunner, portfolio: Path) -> str:
    port = str(portfolio)
    prices = portfolio.parent / "xyz.csv"
    lines = ["symbol,date,price,source,valuation_level"]
    day = FIRST
    while day <= LAST:
        lines.append(f"XYZ,{day.isoformat()},{'110' if day >= STEP else '100'},test,1")
        day += timedelta(days=1)
    prices.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run_pt("--port", port, "policy", "set", "--effective-from", "2024-01-01",
           "--large-flow-pct", "0.10")  # fmt: skip
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    run_pt("--port", port, "buy", "XYZ", "--qty", "5000", "--price", "100", "--date",
           FIRST.isoformat(), "-a", "B")  # fmt: skip
    run_pt("--port", port, "price", "load", "--file", str(prices))
    run_pt("--port", port, "value", "--from", FIRST.isoformat(), "--to", LAST.isoformat())
    return port


@pytest.fixture(scope="module")
def year_template(tmp_path_factory: pytest.TempPathFactory, portfolio_template: Path) -> Path:
    return build_template(tmp_path_factory, _build_year, start_from=portfolio_template)


@pytest.fixture
def year(year_template: Path, portfolio: Path) -> str:
    return str(stack_template(year_template, portfolio))


def _mwr(run_pert: CliRunner, port: str, *extra: str, expect: int = 0) -> dict[str, Any]:
    return run_pert("--port", port, "mwr", *extra, expect=expect).json()


def _net(payload: dict[str, Any]) -> D:
    return D(payload["data"]["returns"][0]["mwr"]["value"])


def _t(on: date) -> D:
    length = years(ANCHOR, on)
    with decimal.localcontext(RETURN_CONTEXT):
        return D(length.whole) + D(length.days) / D(length.of)


def _bisect(present_value: Callable[[D], D]) -> D:
    """The annual rate at which *present_value*, a function of 1 + R, is zero."""
    with decimal.localcontext(RETURN_CONTEXT):
        low, high = D("0.5"), D("3")
        for _ in range(200):
            mid = (low + high) / 2
            if (present_value(low) > 0) == (present_value(mid) > 0):
                low = mid
            else:
                high = mid
        return mid - 1


def _grows(rate: D, years_: D) -> D:
    with decimal.localcontext(RETURN_CONTEXT):
        return (years_ * rate.ln()).exp()


def _validate(payload: dict[str, Any]) -> None:
    from tests.unit.test_json_schemas import _validator

    errors = sorted(_validator("mwr-1.0.json").iter_errors(payload), key=str)
    assert not errors, [e.message for e in errors[:5]]


@pytest.mark.gips
def test_port_gips_c01_with_no_flow_over_a_year_mwr_is_the_annualized_twr(
    run_pert: CliRunner, year: str
) -> None:
    """500,000 to 550,000 over exactly a year, from the end of 2024-01-01 to the
    end of 2025-01-01. Both are 10%, and the MWR is shown beside the TWR."""
    payload = _mwr(run_pert, year, "--to", "2025-01-01")
    _validate(payload)
    data = payload["data"]
    assert data["period"]["is_annualized"] is True
    assert data["period"]["years"] == {"whole": 1, "days": 0, "of": 365}
    assert data["period"]["is_since_inception"] is True
    assert abs(_net(payload) - D("0.1")) < _EXACT
    assert D(data["twr"]["annualized"][0]["value"]) == D("0.1")
    assert data["twr"]["returns"][0]["method"] == "twr"
    shown = run_pert("--port", year, "mwr", "--to", "2025-01-01", fmt="table").stdout
    assert "(MWR, net-of-fees, annualized)" in shown
    assert "(TWR, net-of-fees, annualized)" in shown


@pytest.mark.gips
def test_port_gips_c03_si_mwr_under_one_year_not_annualized(
    run_pert: CliRunner, year: str
) -> None:
    """To 2024-12-31, a day short of a year: the money-weighted figure is the
    cumulative 10%, not annualized, and the TWR has no annualized figure."""
    payload = _mwr(run_pert, year, "--to", "2024-12-31")
    _validate(payload)
    data = payload["data"]
    assert data["period"]["is_annualized"] is False
    assert all(entry["mwr"]["is_annualized"] is False for entry in data["returns"])
    assert abs(_net(payload) - D("0.1")) < _EXACT
    assert data["twr"]["annualized"] == [None, None, None, None]
    shown = run_pert("--port", year, "mwr", "--to", "2024-12-31", fmt="table").stdout
    assert "annualized" not in shown.split("\n\n")[1]
    assert "Under a year, so neither is annualized" in shown


@pytest.mark.gips
def test_port_gips_c02_si_mwr_uses_daily_flows(
    run_pt: CliRunner, run_pert: CliRunner, year: str, tmp_path: Path
) -> None:
    """100,000 more paid into B as cash, on 06-10 or on 06-20. The TWR cannot
    tell the two apart, since cash earns nothing and the deposit adds nothing
    to any day's return. The MWR can: the earlier deposit sat idle ten days
    longer, so it is lower. Each matches a bisection of
    500,000 (1 + R) + 100,000 (1 + R)^(1 - t) = 650,000."""
    other = tmp_path / "other.port"
    other.write_bytes(Path(year).read_bytes())
    rates: list[D] = []
    for port, on in ((year, date(2024, 6, 10)), (str(other), date(2024, 6, 20))):
        run_pt("--port", port, "cash", "deposit", "-a", "B", "--amount", "100000",
               "--date", on.isoformat())  # fmt: skip
        run_pt("--port", port, "value", "--from", on.isoformat(), "--to", "2025-01-01")
        payload = _mwr(run_pert, port, "--to", "2025-01-01")
        _validate(payload)
        moment = _t(on - timedelta(days=1))

        def present(growth: D, moment: D = moment) -> D:
            return D(500000) * growth + D(100000) * _grows(growth, 1 - moment) - D(650000)

        expected = _bisect(present)
        assert abs(_net(payload) - expected) < D("1e-26"), on
        rates.append(_net(payload))
        assert payload["data"]["flows"] == {"count": 2, "net_paid_in": "600000.00"}
    assert rates[0] < rates[1]


@pytest.mark.gips
def test_port_gips_c02_stock_distribution_is_a_flow(
    run_pt: CliRunner, run_pert: CliRunner, year: str
) -> None:
    """1,000 XYZ leave B in kind on 09-03, recorded at 110,000: an outflow at
    the end of that day. 500,000 (1 + R) - 110,000 (1 + R)^(1 - t) = 440,000."""
    run_pt("--port", year, "transfer", "out", "XYZ", "--account", "B", "--qty", "1000",
           "--value", "110000", "-d", "2024-09-03", "--method", "fifo")  # fmt: skip
    run_pt("--port", year, "value", "--from", "2024-09-03", "--to", "2025-01-01")
    payload = _mwr(run_pert, year, "--to", "2025-01-01")
    moment = _t(date(2024, 9, 3))

    def present(growth: D) -> D:
        return D(500000) * growth - D(110000) * _grows(growth, 1 - moment) - D(440000)

    assert abs(_net(payload) - _bisect(present)) < D("1e-26")
    assert payload["data"]["flows"] == {"count": 2, "net_paid_in": "390000.00"}


@pytest.mark.gips
def test_port_gips_c05_portfolio_mwr_is_aggregated_not_averaged(
    run_pt: CliRunner, run_pert: CliRunner, year: str
) -> None:
    """Account C opens with 100,000 on 06-10 and buys 1,000 XYZ at 100, so it
    gains 10% in three weeks. B gains 10% over the year. Averaged by value,
    the two rates give something near 10%. Solved from the combined flows,
    500,000 (1 + R) + 100,000 (1 + R)^(1 - t) = 660,000, it is a little more,
    since C's money was in for half the year."""
    run_pt("--port", year, "account", "add", "--name", "C", "--type", "taxable",
           "--opened", "2024-06-10", "--relief-method", "fifo")  # fmt: skip
    run_pt("--port", year, "cash", "deposit", "-a", "C", "--amount", "100000",
           "--date", "2024-06-10")  # fmt: skip
    run_pt("--port", year, "buy", "XYZ", "--qty", "1000", "--price", "100", "--date",
           "2024-06-10", "-a", "C")  # fmt: skip
    run_pt("--port", year, "value", "--from", "2024-06-10", "--to", "2025-01-01")
    portfolio = _mwr(run_pert, year, "--to", "2025-01-01")
    _validate(portfolio)
    moment = _t(date(2024, 6, 9))

    def present(growth: D) -> D:
        return D(500000) * growth + D(100000) * _grows(growth, 1 - moment) - D(660000)

    combined = _bisect(present)
    assert abs(_net(portfolio) - combined) < D("1e-26")
    b = _net(_mwr(run_pert, year, "--to", "2025-01-01", "--account", "B"))
    c = _mwr(run_pert, year, "--to", "2025-01-01", "--account", "C")
    assert c["data"]["period"]["is_annualized"] is False, "C's own record is under a year"
    with decimal.localcontext(RETURN_CONTEXT):
        averaged = (D(550000) * b + D(110000) * _net(c)) / D(660000)
    assert abs(averaged - combined) > D("0.001")


@pytest.mark.gips
def test_port_gips_c01_mwr_never_presented_alone(run_pert: CliRunner, year: str) -> None:
    """Snapshots run to 2025-01-10, so the TWR to 2025-01-12 is refused, and so
    is the MWR, with the TWR's own refusal. The MWR would need only the two
    ends, but shown alone it would be the headline."""
    twr = run_pert("--port", year, "twr", "--to", "2025-01-12", expect=5).json()["error"]
    mwr = _mwr(run_pert, year, "--to", "2025-01-12", expect=5)["error"]
    assert (mwr["code"], mwr["message"]) == (twr["code"], twr["message"])
    assert mwr["code"] == "PT-E-SNAPSHOT-MISSING"


def test_a_period_mwr_says_it_is_not_the_since_inception_figure(
    run_pert: CliRunner, year: str
) -> None:
    payload = _mwr(run_pert, year, "--from", "2024-03-01", "--to", "2024-12-31")
    _validate(payload)
    assert payload["data"]["period"]["is_since_inception"] is False
    assert payload["data"]["beginning_value"] == "500000.00"
    shown = run_pert("--port", year, "mwr", "--from", "2024-03-01", "--to", "2024-12-31",
                     fmt="table").stdout  # fmt: skip
    assert "not the since-inception figure" in shown


def test_the_same_file_gives_the_same_bytes(run_pert: CliRunner, year: str) -> None:
    """Invariant 6. The solve is in RETURN_CONTEXT, through exp and ln."""
    first = _mwr(run_pert, year, "--to", "2025-01-01")
    again = _mwr(run_pert, year, "--to", "2025-01-01")
    assert first["data"] == again["data"]
    for fmt in ("table", "csv", "markdown"):
        one = run_pert("--port", year, "mwr", "--to", "2025-01-01", fmt=fmt).stdout
        two = run_pert("--port", year, "mwr", "--to", "2025-01-01", fmt=fmt).stdout
        assert one == two, fmt


def test_the_schema_will_not_take_a_dropped_mark(run_pert: CliRunner, year: str) -> None:
    from tests.unit.test_json_schemas import _validator

    payload = _mwr(run_pert, year, "--to", "2024-12-31")
    schema = _validator("mwr-1.0.json")
    annualized = copy.deepcopy(payload)
    annualized["data"]["returns"][0]["mwr"]["is_annualized"] = True
    alone = copy.deepcopy(payload)
    del alone["data"]["twr"]
    estimated = copy.deepcopy(payload)
    estimated["data"]["returns"][0]["mwr"]["is_estimated"] = True
    for broken in (annualized, alone, estimated):
        assert not schema.is_valid(broken)


# ── under --estimate ─────────────────────────────────────────────────────────


def _build_month_end(run_pt: CliRunner, portfolio: Path) -> str:
    """100 XYZ at 100 on 01-03 in B, priced 01-03, 01-24 and 02-05 only.

    01-30 to 02-04 are unpriced across the month end 01-31, which the TWR
    estimates under the flag (ADR 0044).
    """
    port = str(portfolio)
    run_pt("--port", port, "policy", "set", "--effective-from", "2024-01-01",
           "--large-flow-pct", "0.10")  # fmt: skip
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    run_pt("--port", port, "buy", "XYZ", "--qty", "100", "--price", "100", "--date",
           "2024-01-03", "-a", "B")  # fmt: skip
    for on, px in (("2024-01-03", "100"), ("2024-01-24", "102"), ("2024-02-05", "104")):
        run_pt("--port", port, "price", "set", "XYZ", "--price", px, "--date", on,
               "--valuation-level", "1")  # fmt: skip
    run_pt("--port", port, "value", "--from", "2024-01-02", "--to", "2024-02-05")
    return port


@pytest.fixture(scope="module")
def month_end_template(
    tmp_path_factory: pytest.TempPathFactory, portfolio_template: Path
) -> Path:
    return build_template(tmp_path_factory, _build_month_end, start_from=portfolio_template)


@pytest.fixture
def month_end(month_end_template: Path, portfolio: Path) -> str:
    return str(stack_template(month_end_template, portfolio))


@pytest.mark.gips
def test_an_mwr_is_estimated_only_when_an_end_is(run_pert: CliRunner, month_end: str) -> None:
    """ADR 0044 §3. To 02-05 the TWR is estimated, on 01-31, but the MWR reads
    only its ends, 01-01 and 02-05, both measured: it is not marked, and
    neither the cumulative 0.08% nor anything else depends on the carry. To
    02-03 its end is estimated, and it is marked. Without the flag both are
    refused, as the TWR is."""
    refused = _mwr(run_pert, month_end, "--to", "2024-02-05", expect=4)["error"]
    assert refused["code"] == "PT-E-GIPS-VALUATION-GAP"

    measured = _mwr(run_pert, month_end, "--to", "2024-02-05", "--estimate", "0.05")
    _validate(measured)
    data = measured["data"]
    assert data["is_estimated"] is False
    assert all(entry["mwr"]["is_estimated"] is False for entry in data["returns"])
    assert data["twr"]["returns"][0]["is_estimated"] is True
    assert [day["date"] for day in data["twr"]["estimate"]["days"]] == ["2024-01-31"]
    assert abs(_net(measured) - D("0.0008")) < _EXACT
    assert any(w.startswith("Time-weighted: ESTIMATED") for w in measured["warnings"])

    estimated = _mwr(run_pert, month_end, "--to", "2024-02-03", "--estimate", "0.05")
    _validate(estimated)
    assert estimated["data"]["is_estimated"] is True
    assert any(w.startswith("The money-weighted return is ESTIMATED") for w in
               estimated["warnings"])  # fmt: skip
    shown = run_pert("--port", month_end, "mwr", "--to", "2024-02-03", "--estimate", "0.05",
                     fmt="table").stdout  # fmt: skip
    assert shown.startswith("ESTIMATED Money-weighted and time-weighted returns")
    assert "(MWR, net-of-fees, ESTIMATED)" in shown
