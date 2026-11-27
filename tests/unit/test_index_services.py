"""Defining, refreshing and building a `.bmk` file through its services (ADR 0050).

Files stand in for fafnir: a calendar, closes, actions and caps. Two
securities, AAA and BBB, equal-weighted and never rebalanced from 2024-01-02:

    day        AAA   BBB
    01-02       10    20
    01-03       11    20     BBB goes ex 0.40
    01-04       12    22

AAA's units are 50/10 = 5, BBB's 50/20 = 2.5:

    01-03  price 5 x 11 + 2.5 x 20 = 105; points 2.5 x 0.40 = 1; tr 106
    01-04  price 5 x 12 + 2.5 x 22 = 115; tr 106 x 115 / 105
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal as D
from decimal import localcontext
from pathlib import Path

import pytest

import portable_core.schema.bmk as bmk
from portable_core.clock import FixedClock
from portable_core.domain.index import (
    IndexMeta,
    Member,
    RebalanceRule,
    Schedule,
    SecurityType,
    SyntheticSecurity,
    Weighting,
)
from portable_core.errors import PortableError
from portable_core.persistence.connection import open_index, transaction
from portable_core.persistence.index_repositories import IndexRepositories
from portable_core.providers import FileProvider
from portable_core.services import index_construction, index_definition, index_inputs

pytestmark = pytest.mark.unit

CLOCK = FixedClock(datetime(2026, 11, 27, tzinfo=UTC))
BASE = date(2024, 1, 2)
END = date(2024, 1, 4)


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    written = {
        "calendar": "exchange,date\nXNYS,2024-01-02\nXNYS,2024-01-03\nXNYS,2024-01-04\n",
        "prices": (
            "symbol,date,price\nAAA,2024-01-02,10\nAAA,2024-01-03,11\nAAA,2024-01-04,12\n"
            "BBB,2024-01-02,20\nBBB,2024-01-03,20\nBBB,2024-01-04,22\n"
        ),
        "actions": (
            "symbol,action_type,ex_date,cash_amount\nBBB,cash_dividend,2023-10-03,0.40\n"
            "BBB,cash_dividend,2024-01-03,0.40\n"
        ),
        "caps": "symbol,date,market_cap\nAAA,2024-01-02,1000\nBBB,2024-01-02,3000\n",
    }
    paths = {}
    for name, text in written.items():
        path = tmp_path / f"{name}.csv"
        path.write_text(text, encoding="utf-8")
        paths[name] = path
    return paths


def _provider(files: dict[str, Path], prices: Path | None = None) -> FileProvider:
    return FileProvider(
        price_file=prices or files["prices"],
        action_file=files["actions"],
        calendar_file=files["calendar"],
        cap_file=files["caps"],
    )


@pytest.fixture
def repos(tmp_path: Path) -> Iterator[IndexRepositories]:
    con = open_index(tmp_path / "i.bmk", must_exist=False)
    bmk.initialise(con, clock=CLOCK, applied_by="portable test")
    found = IndexRepositories(con, clock=CLOCK)
    with transaction(con):
        index_definition.create(
            found, IndexMeta("Two-stock equal weight", "test", BASE, D(100), "USD", "xnys")
        )
    yield found
    con.close()


def _define(repos: IndexRepositories, weighting: Weighting = Weighting.EQUAL) -> None:
    with transaction(repos.con):
        for symbol in ("AAA", "BBB"):
            sid = index_definition.add_security(
                repos, SyntheticSecurity(0, symbol, SecurityType.EQUITY, "manual")
            )
            repos.add_symbol(sid, symbol, BASE, None, "manual")
        index_definition.set_methodology(repos, BASE, weighting, RebalanceRule.NEVER)
        index_definition.reconstitute(repos, BASE, [Member(1), Member(2)])


def _refresh(repos: IndexRepositories, provider: FileProvider, **kw: str) -> object:
    with transaction(repos.con):
        return index_inputs.refresh(repos, provider, through=END, **kw)


def _build(repos: IndexRepositories) -> None:
    with transaction(repos.con):
        index_construction.build(repos, through=None, built_by="test")


def test_a_file_defined_refreshed_and_built_by_hand(
    repos: IndexRepositories, files: dict[str, Path]
) -> None:
    _define(repos)
    report = _refresh(repos, _provider(files))
    assert (report.trading_days, report.closes, report.dividends) == (3, 6, 2)  # type: ignore[attr-defined]

    # A schedule is declared before it is built: proposed from history, accepted.
    with pytest.raises(PortableError) as caught:
        _build(repos)
    assert caught.value.code == "PT-E-INDEX-NO-SCHEDULE"
    proposals = index_definition.propose_schedules(repos)
    assert [(p.symbol, p.schedule) for p in proposals] == [
        ("AAA", Schedule.NONE),
        ("BBB", Schedule.IRREGULAR),
    ]
    with transaction(repos.con):
        index_definition.accept_proposals(repos, proposals, source="file")
    (bbb,) = [s for s in repos.schedules() if s.security_id == 2]
    assert bbb.source == "proposed from file history (2 dividends), accepted"

    _build(repos)
    levels = repos.levels()
    assert [lv.price_level for lv in levels] == [D(100), D(105), D(115)]
    assert levels[1].total_return_level == D(106)
    with localcontext() as ctx:
        ctx.prec = 34
        assert levels[2].total_return_level == D(106) * D(115) / D(105)
    (reinvested,) = repos.reinvestments()
    assert (reinvested.units, reinvested.index_points) == (D("2.5"), D("1.00"))
    build = repos.build()
    assert build is not None and build.inputs_hash == repos.inputs_hash()


def test_a_rebuild_reproduces_every_built_row(
    repos: IndexRepositories, files: dict[str, Path]
) -> None:
    """ADR 0050 §2: derived state, rebuilt from the definition and the inputs."""
    _define(repos)
    _refresh(repos, _provider(files))
    with transaction(repos.con):
        index_definition.accept_proposals(
            repos, index_definition.propose_schedules(repos), source="file"
        )
    _build(repos)
    first = repos.built_digest()
    _build(repos)
    assert repos.built_digest() == first


def test_a_refresh_adds_and_never_silently_changes(
    repos: IndexRepositories, files: dict[str, Path], tmp_path: Path
) -> None:
    _define(repos)
    _refresh(repos, _provider(files))
    again = _refresh(repos, _provider(files))
    assert (again.closes, again.dividends, again.restated) == (0, 0, 0)  # type: ignore[attr-defined]
    before = repos.inputs_hash()

    changed = tmp_path / "corrected.csv"
    changed.write_text(
        files["prices"]
        .read_text(encoding="utf-8")
        .replace("AAA,2024-01-03,11", "AAA,2024-01-03,11.5"),
        encoding="utf-8",
    )
    with pytest.raises(PortableError) as caught:
        _refresh(repos, _provider(files, changed))
    assert caught.value.code == "PT-E-INDEX-RESTATEMENT"
    assert caught.value.context["changes"] == [
        "close security_id=1;trade_date=2024-01-03: 11 -> 11.5"
    ]
    assert repos.inputs_hash() == before  # nothing written

    restated = _refresh(repos, _provider(files, changed), restate_reason="vendor correction")
    assert restated.restated == 1  # type: ignore[attr-defined]
    assert repos.closes()[(1, date(2024, 1, 3))].close == D("11.5")
    (logged,) = repos.restatements()
    assert (logged["old_value"], logged["new_value"], logged["reason"]) == (
        "11",
        "11.5",
        "vendor correction",
    )
    assert repos.inputs_hash() != before


def test_a_cap_weighted_index_reads_caps_on_the_dates_it_rebalances(
    repos: IndexRepositories, files: dict[str, Path]
) -> None:
    _define(repos, Weighting.CAP)
    report = _refresh(repos, _provider(files))
    assert report.market_caps == 2  # type: ignore[attr-defined]
    assert set(repos.market_caps()) == {(1, BASE), (2, BASE)}


def test_a_definition_is_refused_before_it_is_written(repos: IndexRepositories) -> None:
    with transaction(repos.con):
        sid = index_definition.add_security(
            repos, SyntheticSecurity(0, "AAA", SecurityType.EQUITY, "manual")
        )
        index_definition.set_methodology(repos, BASE, Weighting.EQUAL, RebalanceRule.NEVER)
    for members in ([Member(sid, D("1"))], [Member(sid), Member(sid)]):
        with pytest.raises(PortableError) as caught, transaction(repos.con):
            index_definition.reconstitute(repos, BASE, members)
        assert caught.value.code == "PT-E-INDEX-DEFINITION"
    with pytest.raises(PortableError, match="already has a row"), transaction(repos.con):
        index_definition.set_methodology(repos, BASE, Weighting.CAP, RebalanceRule.MONTHLY)
    with transaction(repos.con):
        index_definition.set_methodology(
            repos, BASE, Weighting.CAP, RebalanceRule.MONTHLY, corrects=True
        )
    assert [m.weighting for m in repos.methodologies()] == [Weighting.CAP]
    assert len(repos.methodologies(include_corrected=True)) == 2


def test_the_file_s_meta_is_complete(repos: IndexRepositories) -> None:
    assert repos.missing_meta() == []
    assert repos.index_meta().calendar == "XNYS"
