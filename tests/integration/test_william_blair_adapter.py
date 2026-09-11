"""The William Blair adapter: the first real custodian, as data.

`src/portable_core/importers/william-blair/` holds the three mapping files
and a **synthetic** fixture -- fake rows, round numbers, one row per rule --
so that the adapter is exercised by the suite without a byte of the owner's
data in the repository. The real exports are read locally only.

Three claims:

- every rule in the activity map is reached by the fixture, so a rule cannot
  rot unnoticed (ADR 0012: the fixtures are fully covered by the map);
- the mapping files carry no account number, no amount, no position;
- the whole pipeline runs on the fixture: inspect, reconstruct at the ACAT
  cutover, and an extract whose seed prices come from the receipts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from portable_core.importers import TabularAdapter, load_activity_map
from portable_core.lint._common import repo_root
from tests.integration.conftest import CliRunner

pytestmark = [pytest.mark.integration]

ADAPTER = repo_root() / "src" / "portable_core" / "importers" / "william-blair"


def test_every_rule_in_the_map_is_reached_by_the_fixture() -> None:
    report = TabularAdapter.load(ADAPTER).read()
    reached = {
        re.sub(r" \((paired with|unpaired|\+ withholding|attached to).*$", "", m.rule)
        for m in report.mapped
    }
    rules = load_activity_map(ADAPTER / "activity_map.toml").rules
    unreached = sorted(rule.label for rule in rules if rule.label not in reached)
    assert unreached == [], f"rules no fixture row exercises: {unreached}"


def test_the_mapping_files_carry_nothing_identifying() -> None:
    """Account nicknames and two digits of an account number are the most the
    files say about the household. Amounts and positions live in the fixture,
    and the fixture is invented."""
    for name in ("source.toml", "activity_map.toml", "instruments.toml"):
        text = (ADAPTER / name).read_text(encoding="utf-8")
        runs = [
            m
            for m in re.findall(r"\d{5,}", text)
            # The one long digit run is a public security identifier inside a
            # description the custodian writes, not an account.
            if m != "003654100"
        ]
        assert runs == [], f"{name} carries a long digit run: {runs}"
        assert not re.search(r"\$\s*\d", text), f"{name} carries an amount"


def test_the_pipeline_runs_on_the_fixture(run_pt: CliRunner, tmp_path: Path) -> None:
    inspect = run_pt("import", "inspect", str(ADAPTER)).ok().data
    assert inspect["accounts"] == ["Brokerage", "IRA", "Roth IRA"]
    assert "settlement_date" not in inspect["capabilities"]["declared"]

    reconstruct = (
        run_pt("import", "reconstruct", str(ADAPTER), "--cutover", "2022-05-18").ok().data
    )
    by_source = {p["identifier"]: p["basis_source"] for p in reconstruct["positions"]}
    # The Roth's GQG block was received in kind and never sold: reconstructed
    # from the snapshot. The IRA's PIMCO block likewise.
    assert by_source["GSIYX"] == "reconstructed"
    assert by_source["PIMIX"] == "reconstructed"

    path = tmp_path / "wb.port"
    run_pt("init", str(path), "--name", "WB", "--inception", "2022-05-18").ok()
    for account, kind in (
        ("Brokerage", "taxable"),
        ("IRA", "tax-deferred"),
        ("Roth IRA", "tax-exempt"),
    ):
        run_pt(
            "--port",
            str(path),
            "account",
            "add",
            "--name",
            account,
            "--type",
            kind,
            "--opened",
            "2022-05-18",
            "--allows-fractional",
        ).ok()
    for symbol in (
        "PIMIX",
        "GSIYX",
        "GOOGL",
        "P",
        "LCGJX",
        "FDRXX",
        "FZAMX",
        "WSMDX",
        "WSMRX",
        "ASML",
        "003CVR016",
    ):
        run_pt("--port", str(path), "instrument", "add", symbol, "--type", "equity").ok()

    batch = tmp_path / "batch.json"
    data = (
        run_pt(
            "--port",
            str(path),
            "import",
            "broker",
            str(ADAPTER),
            "--cutover",
            "2022-05-18",
            "-o",
            str(batch),
        )
        .ok()
        .data
    )
    assert data["seeded"] == 6  # three blocks, three cash balances
    rows = json.loads(batch.read_text(encoding="utf-8"))["rows"]
    seed = next(r for r in rows if r.get("symbol") == "PIMIX" and "cutover" in r["rule"])
    # No price table: the price came from the custodian's receipt on the day.
    assert "Receipt" in seed["source_row"]["price_source"]
    assert seed["original_acquired_date"] == "2022-05-18"
    assert "capital_gain_lt" in {r.get("txn_type") for r in rows}
    transfers = [r for r in rows if r.get("txn_type") == "transfer"]
    assert {(t["account"], t["counter_account"]) for t in transfers} == {
        ("Brokerage", "IRA"),
    }
    # The realized document names the lot the IRA's one sale consumed, so the
    # sale relieves that lot by designation rather than by an assumed method.
    (sale,) = [r for r in rows if r.get("txn_type") == "sell"]
    assert sale["relief_method"] == "spec"
    assert sale["lots"] == [
        {"acquired": "2022-10-17", "quantity": "60", "cost_basis": "600"},
    ]


def test_the_history_commits_in_segments_and_the_gains_tie(
    run_pt: CliRunner, tmp_path: Path
) -> None:
    """The runbook's loop (docs/broker-import.md §11), end to end on the
    fixture: extract up to the day before a corporate action, commit, record
    the action with its typed command, extract `--incremental`, commit, then
    tie every realized gain to the custodian's own lot report.

    The FZAMX shares arrive by a share-class conversion the batch cannot
    carry. Committed in one go, the sale's designated lot would not exist and
    the commit would refuse -- which is the right answer, and why the loop
    runs in segments."""
    path = tmp_path / "wb.port"
    pt = ["--port", str(path)]
    run_pt("init", str(path), "--name", "WB", "--inception", "2022-05-18").ok()
    for account, kind in (
        ("Brokerage", "taxable"),
        ("IRA", "tax-deferred"),
        ("Roth IRA", "tax-exempt"),
    ):
        run_pt(
            *pt, "account", "add", "--name", account, "--type", kind,
            "--opened", "2022-05-18", "--allows-fractional",
        ).ok()  # fmt: skip
    for symbol in ("PIMIX", "GSIYX", "GOOGL", "P", "LCGJX", "FDRXX", "FZAMX", "WSMDX",
                   "WSMRX", "ASML", "003CVR016"):  # fmt: skip
        run_pt(*pt, "instrument", "add", symbol, "--type", "equity").ok()

    first = tmp_path / "segment-1.json"
    run_pt(
        *pt, "import", "broker", str(ADAPTER), "--cutover", "2022-05-18",
        "--until", "2022-10-16", "-o", str(first),
    ).ok()  # fmt: skip
    run_pt(*pt, "import", "batch", str(first)).ok()

    # The conversion in, recorded on its day with the typed command.
    run_pt(
        *pt, "--as-of", "2022-10-17", "transfer", "in", "FZAMX", "-a", "IRA",
        "--qty", "60", "--value", "600", "--basis", "600",
        "--basis-source", "custodian_asserted", "--acquired", "2022-10-17",
        "--note", "share-class conversion in",
    ).ok()  # fmt: skip

    second = tmp_path / "segment-2.json"
    extract = (
        run_pt(*pt, "import", "broker", str(ADAPTER), "--incremental", "-o", str(second))
        .ok()
        .data
    )
    assert extract["seeded"] == 0
    run_pt(*pt, "import", "batch", str(second)).ok()

    tie = run_pt(*pt, "reconcile", "--realized", str(ADAPTER)).ok().data["realized"]
    assert tie["sales"] == 1
    assert tie["tied"] == 1
    assert tie["break"] == 0
    (line,) = tie["lines"]
    assert (line["identifier"], line["status"], line["difference"]) == ("FZAMX", "tied", "0.00")
