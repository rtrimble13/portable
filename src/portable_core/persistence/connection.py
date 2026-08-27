"""Opening a `.port` file, and the one place SQLite is configured.

ADR 0002 and ADR 0005. Everything about how `portable` talks to SQLite is
decided here: pragmas, the Decimal adapter pair, row factory, and the
transaction discipline. No other module calls `sqlite3.connect`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from portable_core.decimals import from_text, to_text
from portable_core.errors import PortfolioFileError
from portable_core.errors.kinds import (
    E_PORTFOLIO_CORRUPT,
    E_PORTFOLIO_LOCKED,
    E_PORTFOLIO_NOT_FOUND,
)

#: The file extension a portfolio uses. Not enforced -- a user may name a file
#: anything -- but it is what `pt init` appends and what the docs assume.
PORT_SUFFIX: Final[str] = ".port"

_ADAPTERS_REGISTERED = False


def _register_adapters() -> None:
    """Register the Decimal adapter/converter pair. Idempotent, once per process.

    ADR 0005: this is the *only* module permitted to call
    ``sqlite3.register_adapter``. The adapter is one direction only -- Decimal
    out to canonical text. There is deliberately **no** automatic conversion
    coming back, and ``detect_types`` is not used: repositories convert
    explicitly with :func:`~portable_core.decimals.from_text`, so a forgotten
    conversion surfaces as a ``str`` where a ``Decimal`` was expected and fails
    loudly, rather than as a value that silently behaves like text.
    """
    global _ADAPTERS_REGISTERED
    if _ADAPTERS_REGISTERED:
        return
    sqlite3.register_adapter(Decimal, to_text)
    _ADAPTERS_REGISTERED = True


def _configure(con: sqlite3.Connection, *, read_only: bool) -> None:
    con.row_factory = sqlite3.Row
    # Referential integrity is off by default in SQLite and must be enabled
    # per connection. Without it the schema's REFERENCES clauses are decoration.
    con.execute("PRAGMA foreign_keys = ON")
    # WAL: concurrent readers alongside one writer, and a crash-safe journal.
    # Not available on a read-only handle to a fresh file, hence the guard.
    if not read_only:
        con.execute("PRAGMA journal_mode = WAL")
        # FULL, not NORMAL. This file is a tax record; an fsync per commit is
        # a price worth paying for it.
        con.execute("PRAGMA synchronous = FULL")
    con.execute("PRAGMA busy_timeout = 5000")
    # Reject a foreign key that points at nothing when the schema is checked.
    con.execute("PRAGMA legacy_alter_table = OFF")


def open_portfolio(
    path: Path | str,
    *,
    read_only: bool = False,
    must_exist: bool = True,
) -> sqlite3.Connection:
    """Open a `.port` file and return a configured connection.

    Args:
        path: the portfolio file.
        read_only: open with an immutable URI so no write can occur. Used by
            ``pt query --sql``, where the guard against writes should not
            depend on parsing the user's SQL correctly.
        must_exist: when True (the default), a missing file is an error rather
            than an empty database. SQLite's habit of creating a file on
            connect turns a typo into a silently empty portfolio.

    Raises:
        PortfolioFileError: missing, locked, or not a database.
    """
    _register_adapters()
    file_path = Path(path)

    if must_exist and not file_path.exists():
        raise PortfolioFileError(
            f"portfolio file not found: {file_path}",
            code=E_PORTFOLIO_NOT_FOUND,
            remedy=f"Create it with `pt init {file_path}` or check --port / PORTABLE_PORT.",
            path=str(file_path),
        )

    try:
        if read_only:
            uri = f"file:{file_path.as_posix()}?mode=ro"
            con = sqlite3.connect(uri, uri=True, isolation_level=None)
        else:
            con = sqlite3.connect(file_path, isolation_level=None)
        _configure(con, read_only=read_only)
    except sqlite3.OperationalError as exc:
        message = str(exc)
        if "locked" in message or "busy" in message:
            raise PortfolioFileError(
                f"portfolio is locked by another process: {file_path}",
                code=E_PORTFOLIO_LOCKED,
                remedy="Close the other `portable` process and retry.",
                path=str(file_path),
            ) from exc
        raise PortfolioFileError(
            f"cannot open portfolio: {file_path}: {message}",
            code=E_PORTFOLIO_CORRUPT,
            path=str(file_path),
        ) from exc

    return con


@contextmanager
def transaction(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one all-or-nothing database transaction.

    ``isolation_level=None`` puts the driver in autocommit, so transactions are
    explicit here rather than implicit and surprising. A command that writes a
    ledger row plus the derived rows it implies writes all of them or none:
    a ledger entry whose derived state did not land would break the replay
    invariant with no error to point at.
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def decimal_or_none(value: Any) -> Decimal | None:
    """Convert a nullable text column to a Decimal.

    The explicit counterpart to the write-side adapter. Repositories call this
    rather than relying on a converter, per ADR 0005.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return from_text(str(value))


def decimal_of(value: Any) -> Decimal:
    """Convert a NOT NULL text column to a Decimal."""
    result = decimal_or_none(value)
    if result is None:
        raise ValueError("expected a decimal, found NULL")
    return result
