"""Every connection a command opens is closed, whether it succeeds or not.

Commands opened the portfolio and never closed it. A process exiting closes
it anyway, which is why nothing failed on Python 3.11 and 3.12. Python 3.13
reports an unclosed connection as a ResourceWarning, which this suite treats as
an error, and on Windows an open handle keeps the file locked for as long as
the process lives. These run the CLI in-process, where a leak outlives the
command.

Checked by using the connection afterwards, not by catching the warning, so
the test means the same thing on every Python version.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from portable_core.cli import context
from portable_core.errors import PortfolioFileError
from portable_core.persistence.connection import open_portfolio
from portable_pt.app import app

pytestmark = pytest.mark.unit


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[sqlite3.Connection]:
    """Every connection a command opens through the context, in order."""
    seen: list[sqlite3.Connection] = []
    real: Callable[..., sqlite3.Connection] = open_portfolio

    def recording(*args: object, **kwargs: object) -> sqlite3.Connection:
        con = real(*args, **kwargs)
        seen.append(con)
        return con

    monkeypatch.setattr(context, "open_portfolio", recording)
    return seen


def _closed(con: sqlite3.Connection) -> bool:
    try:
        con.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


@pytest.fixture
def port(tmp_path: Path) -> str:
    path = tmp_path / "c.port"
    result = CliRunner().invoke(
        app, ["init", str(path), "--name", "C", "--inception", "2024-01-02"]
    )
    assert result.exit_code == 0, result.output
    return str(path)


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (["info"], 0),
        (["validate"], 0),
        (["rebuild"], 0),
        (["account", "add", "--name", "B", "--type", "taxable", "--opened", "2024-01-02"], 0),
        (["cash", "deposit", "-a", "NOPE", "--amount", "1", "--date", "2024-01-02"], 4),
    ],
)
def test_a_command_closes_what_it_opened(
    port: str, opened: list[sqlite3.Connection], args: list[str], code: int
) -> None:
    result = CliRunner().invoke(app, ["--format", "json", "--port", port, *args])

    assert result.exit_code == code, result.output
    assert opened, "the command opened nothing through the context"
    assert all(_closed(con) for con in opened)


def test_a_file_that_is_not_a_database_is_refused_by_name(tmp_path: Path) -> None:
    """It reached the user as an unexpected DatabaseError, with the handle open."""
    path = tmp_path / "notes.port"
    path.write_text("not a database, just some notes\n" * 20, encoding="utf-8")

    with pytest.raises(PortfolioFileError) as raised:
        open_portfolio(path)

    assert raised.value.code == "PT-E-PORTFOLIO-CORRUPT"
