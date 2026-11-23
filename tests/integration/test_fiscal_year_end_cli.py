"""`pt init --fiscal-year-end` names a month end, or nothing is created. ADR 0045 §2.

`PORT-GIPS-A04`: annual valuation dates derive from the stored fiscal year
end, and quarters step back from it. A value that is no month end gives no
boundary to derive, so it is refused at the door rather than stored for a
report to trip over later.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner

pytestmark = pytest.mark.integration


@pytest.mark.gips
@pytest.mark.parametrize("value", ["06-15", "02-29", "04-31", "6-30", "banana"])
def test_port_gips_a04_a_fiscal_year_end_that_is_no_month_end_is_refused(
    run_pt: CliRunner, tmp_path: Path, value: str
) -> None:
    path = tmp_path / "f.port"
    error = run_pt("init", str(path), "--name", "F", "--inception", "2024-01-02",
                   "--fiscal-year-end", value, expect=2).json()["error"]  # fmt: skip
    assert error["code"] == "PT-E-FISCAL-YEAR-END"
    assert error["context"]["value"] == value
    assert not path.exists()


@pytest.mark.gips
@pytest.mark.parametrize("value", ["12-31", "06-30", "02-28"])
def test_port_gips_a04_a_month_end_is_stored_as_given(
    run_pt: CliRunner, tmp_path: Path, value: str
) -> None:
    path = tmp_path / "f.port"
    run_pt("init", str(path), "--name", "F", "--inception", "2024-01-02",
           "--fiscal-year-end", value)  # fmt: skip
    info = run_pt("--port", str(path), "info").data
    assert info["fiscal_year_end"] == value
