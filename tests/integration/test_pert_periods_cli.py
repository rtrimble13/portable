"""`pert periods` end to end: every period, from one walk. ADR 0045.

Two portfolios. The committed sample has five years, three accounts and
Modified Dietz bridges. Every row's figure is held to `pert twr` over the
same dates and to the chain of its own annual rows, and the table is held to
golden files in all four formats. The stints portfolio of
`test_track_record_breaks.py` holds nothing for two weeks, so its rows show
how a table is clipped to a track record and never linked across one.

Regenerate the golden files, after a change that is meant to move them, with
`PORTABLE_REGENERATE_GOLDEN=1 pytest tests/integration/test_pert_periods_cli.py`.
Each is compared as text read in universal-newline mode, so a checkout that
turns LF into CRLF, as Git for Windows can, does not fail it.
"""

from __future__ import annotations

import decimal
import json
import os
import shutil
import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from portable_core.domain.returns import BASES
from portable_core.errors import PortableError
from portable_core.persistence.connection import open_portfolio
from portable_core.persistence.repositories import Repositories
from portable_core.services.periods import period_table
from portable_core.services.returns import link, time_weighted_return
from tests.integration.conftest import ROOT, CliRunner, build_template, copy_template
from tests.integration.test_track_record_breaks import _build_stints
from tests.unit.test_json_schemas import _validator

pytestmark = pytest.mark.integration

SAMPLE = ROOT / "examples" / "sample.port"
GOLDEN = ROOT / "tests" / "golden" / "periods"
SUMMARY = [
    "Month to date",
    "Quarter to date",
    "Year to date",
    "1 year",
    "3 years",
    "5 years",
    "10 years",
    "Since inception",
]


@pytest.fixture
def sample(tmp_path: Path) -> str:
    copy = tmp_path / "sample.port"
    shutil.copyfile(SAMPLE, copy)
    return str(copy)


@pytest.fixture(scope="module")
def stints_template(tmp_path_factory: pytest.TempPathFactory, portfolio_template: Path) -> Path:
    return build_template(tmp_path_factory, _build_stints, start_from=portfolio_template)


@pytest.fixture
def stints(stints_template: Path, tmp_path: Path) -> str:
    return str(copy_template(stints_template, tmp_path / "stints.port"))


@pytest.fixture
def sample_repos(sample: str) -> Iterator[Repositories]:
    con = open_portfolio(sample)
    try:
        yield Repositories(con)
    finally:
        con.close()


def _set_fiscal_year_end(port: str, value: str) -> None:
    """Write the stored value directly, as a file made before `pt init` checked it holds.

    The connection is closed, not only committed: `with sqlite3.connect()`
    commits and leaves it open, for the garbage collector to report.
    """
    con = sqlite3.connect(port)
    try:
        with con:
            con.execute("UPDATE meta SET value = ? WHERE key = 'fiscal_year_end'", (value,))
    finally:
        con.close()


def _periods(run_pert: CliRunner, port: str, *args: str, expect: int = 0) -> dict[str, Any]:
    payload = run_pert("--port", port, "periods", *args, expect=expect).json()
    if expect == 0:
        errors = sorted(_validator("periods-1.0.json").iter_errors(payload), key=str)
        assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors[:5]]
    return payload


def _rows(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    data = payload["data"]
    return {row["label"]: row for row in data["summary"] + data["fiscal_years"]}


def _net(row: dict[str, Any], *, annualized: bool = False) -> D | None:
    [entry] = [r for r in row["returns"] if r["basis"] == "net_of_fees"]
    value = entry["annualized" if annualized else "cumulative"]
    return D(value["value"]) if value is not None else None


# ── the sample ───────────────────────────────────────────────────────────────


@pytest.mark.gips
def test_port_gips_h01_every_period_since_inception(run_pert: CliRunner, sample: str) -> None:
    """The summary rows in order, then every fiscal year since inception. The
    sample began on 2021-01-04, three days into its five years, so the 5-year
    row is refused rather than shortened, as is the 10-year one."""
    payload = _periods(run_pert, sample, "--to", "2025-12-31")
    data = payload["data"]
    assert [row["label"] for row in data["summary"]] == SUMMARY
    assert [row["label"] for row in data["fiscal_years"]] == [
        "2021",
        "2022",
        "2023",
        "2024",
        "2025",
    ]
    assert data["track_records"] == [{"inception": "2021-01-04", "termination": None}]
    rows = _rows(payload)
    for label in ("5 years", "10 years"):
        assert rows[label]["returns"] is None
        assert rows[label]["refusal"]["code"] == "PT-E-PERIOD-OUTSIDE-RECORD"
    assert len(payload["warnings"]) == 2


@pytest.mark.gips
def test_port_gips_b04_the_rows_link_to_one_another(run_pert: CliRunner, sample: str) -> None:
    """The 3-year row is its three years linked, and since inception is all
    five. Each is a window of the same walk, so they agree to the precision
    the chain carries, at every basis."""
    rows = _rows(_periods(run_pert, sample, "--to", "2025-12-31"))
    for basis in (str(b) for b in BASES):

        def cumulative(label: str, basis: str = basis) -> D:
            [entry] = [r for r in rows[label]["returns"] if r["basis"] == basis]
            return D(entry["cumulative"]["value"])

        three = link(cumulative(year) for year in ("2023", "2024", "2025"))
        assert abs(three - cumulative("3 years")) < D("1e-28")
        five = link(cumulative(year) for year in ("2021", "2022", "2023", "2024", "2025"))
        assert abs(five - cumulative("Since inception")) < D("1e-28")


@pytest.mark.gips
def test_a_row_is_pert_twr_over_its_window(sample_repos: Repositories) -> None:
    """ADR 0045 §1: every measured row is exactly `pert twr` over the dates it
    measured -- every sub-period, disclosure and figure -- though the table
    walked the history once."""
    table = period_table(sample_repos, through=date(2025, 12, 31), staleness_days=5)
    measured = [row for row in table.rows if row.result is not None]
    assert len(measured) == 11
    for row in measured:
        assert row.result is not None
        alone = time_weighted_return(
            sample_repos, start=row.result.start, end=row.result.end, staleness_days=5
        )
        assert repr(row.result) == repr(alone), row.window.label


@pytest.mark.gips
def test_fixed_length_row_never_shortened(run_pert: CliRunner, sample: str) -> None:
    """ADR 0045 §2. Through 2023-06-30 the sample's record is two and a half
    years old. `pert twr` over the 3-year dates moves the start to inception
    and prints that shorter figure. The table's 3-year row is null instead,
    because the shorter figure under a "3 years" label is a different number."""
    rows = _rows(_periods(run_pert, sample, "--to", "2023-06-30"))
    three = rows["3 years"]
    assert three["returns"] is None
    assert three["refusal"]["code"] == "PT-E-PERIOD-OUTSIDE-RECORD"
    assert three["requested"] == {"from": "2020-07-01", "to": "2023-06-30"}

    shorter = run_pert("--port", sample, "twr", "--from", "2020-07-01", "--to",
                       "2023-06-30").json()["data"]["period"]  # fmt: skip
    assert (shorter["requested_start"], shorter["start"]) == ("2020-07-01", "2021-01-04")
    assert rows["1 year"]["returns"] is not None  # what the record covers, it shows


@pytest.mark.gips
def test_annual_boundaries_are_stable(run_pert: CliRunner, sample: str) -> None:
    """PORT-GIPS-A04: a complete year's row is the same bytes whatever day
    the table ends on."""
    early = _rows(_periods(run_pert, sample, "--to", "2025-03-31"))
    late = _rows(_periods(run_pert, sample, "--to", "2025-12-31"))
    for year in ("2021", "2022", "2023", "2024"):
        assert json.dumps(early[year], sort_keys=True) == json.dumps(late[year], sort_keys=True)
    assert "2025" not in early  # the year holding --to is year to date, not a year


@pytest.mark.gips
def test_partial_first_year_labelled_and_unannualized(run_pert: CliRunner, sample: str) -> None:
    """PORT-GIPS-H02: 2021 runs from inception, 2021-01-04, is labelled so,
    and at 362 days has no annualized figure at any basis (PORT-GIPS-B07)."""
    row = _rows(_periods(run_pert, sample, "--to", "2025-12-31"))["2021"]
    assert (row["start"], row["end"], row["partial"]) == (
        "2021-01-04",
        "2021-12-31",
        ["inception"],
    )
    assert row["requested"] == {"from": "2021-01-01", "to": "2021-12-31"}
    for entry in row["returns"]:
        assert entry["annualized"] is None
        assert entry["not_annualized"]["code"] == "PT-E-GIPS-ANNUALIZE-SUB-YEAR"

    table = run_pert("--port", sample, "periods", "--to", "2025-12-31", fmt="table").stdout
    assert "2021 is partial: it starts at inception" in table


@pytest.mark.gips
def test_subyear_return_never_annualized(run_pert: CliRunner, sample: str) -> None:
    """PORT-GIPS-B07 across a table: to date, a month and a quarter have no
    annualized figure; a year's equals its cumulative exactly; three years'
    compounds back to the cumulative."""
    rows = _rows(_periods(run_pert, sample, "--to", "2025-12-31"))
    for label in ("Month to date", "Quarter to date"):
        assert _net(rows[label], annualized=True) is None
    assert _net(rows["1 year"], annualized=True) == _net(rows["1 year"])
    rate, cumulative = _net(rows["3 years"], annualized=True), _net(rows["3 years"])
    assert rate is not None and cumulative is not None
    with decimal.localcontext(decimal.Context(prec=50)):
        assert abs((1 + rate) ** 3 - (1 + cumulative)) < D("1e-30")
    assert rate != cumulative


@pytest.mark.gips
def test_every_annual_row_carries_its_assets(run_pert: CliRunner, sample: str) -> None:
    """PORT-GIPS-H03: the ending value and the accounts holding value, at each
    year's end. The last year's is the table's own, since --to is its end."""
    rows = _rows(_periods(run_pert, sample, "--to", "2025-12-31"))
    for year in ("2021", "2022", "2023", "2024", "2025"):
        assert D(rows[year]["ending_value"]) > 0
        assert rows[year]["accounts_holding_value"] == 3
    assert rows["2025"]["ending_value"] == rows["Since inception"]["ending_value"]


def test_a_null_row_is_never_zero(run_pert: CliRunner, sample: str) -> None:
    """Invariant 10, in every format: the refused 5-year row is null with its
    reason in JSON, an empty cell in CSV, and a dash with a footnote in table
    and markdown. Never 0.00%."""
    row = _rows(_periods(run_pert, sample, "--to", "2025-12-31"))["5 years"]
    assert row["returns"] is None and row["ending_value"] is None
    assert row["refusal"]["message"]

    csv = run_pert("--port", sample, "periods", "--to", "2025-12-31", fmt="csv").stdout
    [line] = [line for line in csv.splitlines() if line.startswith("5 years")]
    assert line.split(",")[3:] == ["", "", "", ""]

    for fmt in ("table", "markdown"):
        text = run_pert("--port", sample, "periods", "--to", "2025-12-31", fmt=fmt).stdout
        [line] = [line for line in text.splitlines() if "5 years [" in line]
        assert "—" in line and "0.00" not in line and "%" not in line
        assert "starts before the track record began, on 2021-01-04" in text


@pytest.mark.parametrize(("fmt", "name"), [
    ("json", "sample.json"),
    ("table", "sample.txt"),
    ("markdown", "sample.md"),
    ("csv", "sample.csv"),
])  # fmt: skip
def test_the_sample_table_matches_its_golden_file(
    run_pert: CliRunner, sample: str, fmt: str, name: str
) -> None:
    """Issue 5: the table in all four formats, byte for byte, but for the clock."""
    out = run_pert("--port", sample, "--as-of", "2025-12-31", "periods", fmt=fmt).stdout
    if fmt == "json":
        envelope = json.loads(out)
        for clock in ("generated_at", "portable_version"):
            envelope.pop(clock)
        out = json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path = GOLDEN / name
    if os.environ.get("PORTABLE_REGENERATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(out, encoding="utf-8", newline="\n")
    assert out == path.read_text(encoding="utf-8"), (
        f"{name} no longer matches. If the change is meant, regenerate with "
        "PORTABLE_REGENERATE_GOLDEN=1 and review the diff."
    )


def test_a_fiscal_year_ending_in_june(run_pert: CliRunner, sample: str) -> None:
    """PORT-GIPS-A04: the year and its quarters derive from the stored year end.
    Through 2025-12-31 with years ending 06-30: fiscal year to date from
    2025-07-01, the quarter from 2025-10-01, FY2021 from inception to
    2021-06-30, and FY2025 the last complete one."""
    _set_fiscal_year_end(sample, "06-30")
    payload = _periods(run_pert, sample, "--to", "2025-12-31")
    rows = _rows(payload)
    assert payload["data"]["fiscal_year_end"] == "06-30"
    assert rows["Fiscal year to date"]["requested"]["from"] == "2025-07-01"
    assert rows["Quarter to date"]["requested"]["from"] == "2025-10-01"
    assert [row["label"] for row in payload["data"]["fiscal_years"]] == [
        "FY2021", "FY2022", "FY2023", "FY2024", "FY2025",
    ]  # fmt: skip
    assert (rows["FY2021"]["start"], rows["FY2021"]["end"]) == ("2021-01-04", "2021-06-30")
    assert rows["FY2025"]["end"] == "2025-06-30"
    assert "fiscal year ending 06-30" in payload["data"]["title"]


def test_a_stored_fiscal_year_end_that_is_no_month_end_is_refused(
    run_pert: CliRunner, sample: str
) -> None:
    """ADR 0045 §2: a file made before `pt init` checked it is refused when read."""
    _set_fiscal_year_end(sample, "06-15")
    error = _periods(run_pert, sample, "--to", "2025-12-31", expect=4)["error"]
    assert error["code"] == "PT-E-FISCAL-YEAR-END"
    assert error["context"]["value"] == "06-15"


def test_another_basis_is_shown_on_request(run_pert: CliRunner, sample: str) -> None:
    table = run_pert("--port", sample, "periods", "--to", "2025-12-31", "--basis",
                     "gross_of_fees", fmt="table").stdout  # fmt: skip
    assert "Gross-of-fees, cumulative" in table
    error = run_pert("--port", sample, "periods", "--basis", "gross", expect=2).json()["error"]
    assert "--basis must be one of" in error["message"]


# ── track records ────────────────────────────────────────────────────────────


@pytest.mark.gips
def test_track_record_break_not_linked(run_pert: CliRunner, stints: str) -> None:
    """PORT-GIPS-H06 in a table. Up 10% to 01-05, nothing held, then down 10%
    from 01-22. The periods to date and since inception are the second
    record's, down 10%. The first record's year is its own row, up 10%.
    Nothing links the two into -1%."""
    payload = _periods(run_pert, stints, "--to", "2024-01-31")
    data, rows = payload["data"], _rows(payload)
    assert data["track_records"] == [
        {"inception": "2024-01-02", "termination": "2024-01-05"},
        {"inception": "2024-01-22", "termination": None},
    ]
    for label in ("Month to date", "Quarter to date", "Year to date", "Since inception"):
        assert _net(rows[label]) == D("-0.1"), label
        assert (rows[label]["start"], rows[label]["record"]) == ("2024-01-22", 1)
    assert rows["Month to date"]["partial"] == ["restart"]
    [year] = data["fiscal_years"]
    assert (year["label"], year["record"], year["start"], year["end"]) == (
        "2024", 0, "2024-01-02", "2024-01-05",
    )  # fmt: skip
    assert year["partial"] == ["inception", "termination"]
    assert _net(year) == D("0.1")
    assert all(_net(row) != D("-0.01") for row in rows.values() if row["returns"])

    table = run_pert("--port", stints, "periods", "--to", "2024-01-31", fmt="table").stdout
    assert "Nothing held" in table
    assert "the two are never linked (PORT-GIPS-H06)" in table


@pytest.mark.gips
def test_port_gips_h02_a_record_that_ended_is_measured_to_its_end(
    run_pert: CliRunner, stints: str
) -> None:
    """Through 01-19, the record ended on 01-05 and has not restarted. Since
    inception and the periods to date run to 01-05, labelled with the
    termination. Its 2024 row is the same bytes as it is through 01-31,
    after the restart (PORT-GIPS-A04)."""
    ended = _rows(_periods(run_pert, stints, "--to", "2024-01-19"))
    assert (ended["Since inception"]["end"], ended["Since inception"]["partial"]) == (
        "2024-01-05",
        ["termination"],
    )
    assert _net(ended["Year to date"]) == D("0.1")
    assert ended["1 year"]["refusal"]["code"] == "PT-E-PERIOD-OUTSIDE-RECORD"
    later = _rows(_periods(run_pert, stints, "--to", "2024-01-31"))
    assert json.dumps(ended["2024"], sort_keys=True) == json.dumps(
        later["2024"], sort_keys=True
    )


def test_a_table_with_no_measured_row_exits_with_the_first_refusal(
    run_pert: CliRunner, portfolio: Path
) -> None:
    """ADR 0045 §3. The base portfolio has no return policy, so no row can be
    measured, and the command fails as `pert twr` over the first row would."""
    error = _periods(run_pert, str(portfolio), "--to", "2024-01-31", expect=4)["error"]
    assert error["code"] == "PT-E-GIPS-NO-FLOW-POLICY"


def test_a_refused_row_carries_what_pert_twr_refuses(sample_repos: Repositories) -> None:
    """ADR 0045 §3. The sample has no price within five days of 2025-06-20, so
    no period can end there. Every row ending on it is refused exactly as
    `pert twr` over its dates is, and the complete years before it stand."""
    table = period_table(sample_repos, through=date(2025, 6, 20), staleness_days=5)
    refused = [
        row
        for row in table.summary
        if row.refusal is not None and row.refusal.code != "PT-E-PERIOD-OUTSIDE-RECORD"
    ]
    assert [row.window.label for row in refused] == [
        "Month to date", "Quarter to date", "Year to date", "1 year", "3 years",
        "Since inception",
    ]  # fmt: skip
    for row in refused:
        assert row.refusal is not None
        with pytest.raises(PortableError) as alone:
            time_weighted_return(
                sample_repos, start=row.window.first, end=row.window.through, staleness_days=5
            )
        assert alone.value.to_dict() == row.refusal.to_dict(), row.window.label
    assert [row.window.label for row in table.fiscal_years if row.result is not None] == [
        "2021", "2022", "2023", "2024",
    ]  # fmt: skip


def test_the_walk_is_refused_before_any_row_without_ledger_rows(
    run_pert: CliRunner, sample: str
) -> None:
    """ADR 0045 §3: a --to before inception refuses the table, not each row."""
    error = _periods(run_pert, sample, "--to", "2020-12-31", expect=4)["error"]
    assert "nothing to measure" in error["message"]
