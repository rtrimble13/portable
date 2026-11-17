"""A copied template is the portfolio its builder makes in place.

`tests/integration/conftest.py` builds each portfolio a fixture needs once, as
a template, and copies it into every test. Every integration test then runs on
a copy, so these tests hold the premise: a copy holds what the same calls to
`pt` write in the test's own directory, but for the wall-clock stamps, which
nothing orders, replays or values by (ADR 0035). They also hold the two checks
that refuse a template to the shapes they refuse.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.integration.conftest import (
    CliRunner,
    build_portfolio,
    build_template,
    check_template,
    copy_template,
    stack_template,
)

pytestmark = pytest.mark.integration

#: The stamps read from the wall clock (CLAUDE.md invariant 6, ADR 0035): the
#: columns of these names, and `meta`'s rows under these keys, where `pt init`
#: records when the portfolio was created. Every other stored value must be
#: the same in a copy and an in-place build.
STAMPS = frozenset({"created_at", "updated_at", "applied_at"})


def _rows(path: Path) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table in *path*, less the stamps, in a stable order."""
    con = sqlite3.connect(f"{path.as_uri()}?immutable=1", uri=True)
    try:
        tables = [
            str(row[0])
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        contents: dict[str, list[tuple[Any, ...]]] = {}
        for table in tables:
            columns = [
                str(row[1])
                for row in con.execute(f'PRAGMA table_info("{table}")')
                if str(row[1]) not in STAMPS
            ]
            select = ", ".join(f'"{c}"' for c in columns)
            rows = con.execute(f'SELECT {select} FROM "{table}"').fetchall()
            if table == "meta":
                rows = [r for r in rows if r[columns.index("key")] not in STAMPS]
            contents[table] = sorted((tuple(r) for r in rows), key=repr)
        return contents
    finally:
        con.close()


def _build_held(run_pt: CliRunner, portfolio: Path) -> None:
    """AAPL bought into `portfolio`'s taxable account: a template stacked on another."""
    port = str(portfolio)
    run_pt("--port", port, "instrument", "add", "AAPL", "--type", "equity")
    run_pt("--port", port, "buy", "AAPL", "--qty", "10", "--price", "190.00",
           "--date", "2024-01-03", "-a", "B")  # fmt: skip


@pytest.fixture(scope="module")
def held_template(tmp_path_factory: pytest.TempPathFactory, portfolio_template: Path) -> Path:
    return build_template(tmp_path_factory, _build_held, start_from=portfolio_template)


@pytest.fixture
def held(held_template: Path, portfolio: Path) -> Path:
    return stack_template(held_template, portfolio)


def _in_place(tmp_path: Path) -> Path:
    folder = tmp_path / "in-place"
    folder.mkdir()
    return folder / "t.port"


def test_a_copy_of_the_portfolio_template_is_what_building_in_place_writes(
    run_pt: CliRunner, portfolio: Path, tmp_path: Path
) -> None:
    built = _in_place(tmp_path)
    build_portfolio(run_pt, built)

    copied, fresh = _rows(portfolio), _rows(built)
    assert fresh["account"] and fresh["transaction"], "nothing to compare"
    assert copied == fresh


def test_a_copy_of_a_stacked_template_is_what_building_in_place_writes(
    run_pt: CliRunner, held: Path, tmp_path: Path
) -> None:
    built = _in_place(tmp_path)
    build_portfolio(run_pt, built)
    _build_held(run_pt, built)

    copied, fresh = _rows(held), _rows(built)
    assert fresh["lot"], "nothing stacked to compare"
    assert copied == fresh


def test_a_stacked_copy_is_the_file_portfolio_names(portfolio: Path, held: Path) -> None:
    """A test asking for both sees one file, holding the stacked state.

    Before templates, a stacked fixture wrote into `portfolio`'s file. It now
    copies its template over that path, after `portfolio` has made it.
    """
    assert held == portfolio
    assert _rows(portfolio)["lot"], "the stacked copy, not the base, is on that path"


def test_stacking_onto_a_portfolio_a_fixture_already_wrote_to_is_refused(
    held_template: Path, held: Path
) -> None:
    """Built in place, both fixtures' writes would be in the file; a copy keeps one.

    So a second stacked copy onto the same `portfolio`, as a test asking for two
    stacked fixtures would make, is refused instead.
    """
    with pytest.raises(AssertionError, match="another fixture wrote to it"):
        stack_template(held_template, held)


def test_a_template_with_a_sidecar_beside_it_is_refused(
    portfolio: Path, tmp_path: Path
) -> None:
    """A copy of the main file alone would lose what a `-wal` holds."""
    folder = tmp_path / "template-probe"
    folder.mkdir()
    template = copy_template(portfolio, folder / "t.port")
    (folder / "t.port-wal").write_bytes(b"")

    with pytest.raises(AssertionError, match=r"t\.port-wal left beside the template"):
        check_template(template)


def test_a_template_that_names_its_own_directory_is_refused(
    run_pt: CliRunner, portfolio: Path, tmp_path: Path
) -> None:
    """A copy would point another test at this one's files."""
    folder = tmp_path / "template-probe"
    folder.mkdir()
    template = copy_template(portfolio, folder / "t.port")
    run_pt("--port", str(template), "instrument", "add", "XYZ", "--type", "equity",
           "--name", str(folder / "notes.txt"))  # fmt: skip

    with pytest.raises(
        AssertionError, match=r"names the directory it was built in.*instrument"
    ):
        check_template(template)


def test_the_checks_pass_a_template_that_holds_neither(portfolio: Path, tmp_path: Path) -> None:
    """So the refusals above are the checks biting, not the checks failing."""
    folder = tmp_path / "template-probe"
    folder.mkdir()
    check_template(copy_template(portfolio, folder / "t.port"))
    assert not list(folder.glob("t.port-*")), "the check left a sidecar of its own"


def test_shutil_copies_the_bytes_the_template_holds(
    portfolio_template: Path, tmp_path: Path
) -> None:
    """`copy_template` copies the file, not a re-serialised database."""
    to = tmp_path / "copy.port"
    copy_template(portfolio_template, to)
    assert to.read_bytes() == portfolio_template.read_bytes()
