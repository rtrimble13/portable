"""The `.bmk` file's schema: its migrations, read by the `.port`'s runner. ADR 0050.

A `.bmk` file is one index. Its DDL is versioned on its own, in this package,
because it is a different file with a different schema: a `.bmk` at version 1
and a `.port` at version 13 are both current. The runner, its checksums, its
atomicity and its clock are the `.port`'s (`schema/migrations.py`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Final

from portable_core.clock import SYSTEM_CLOCK, Clock
from portable_core.errors import PortfolioFileError
from portable_core.errors.kinds import E_PORTFOLIO_CORRUPT, E_SCHEMA_TOO_NEW, E_SCHEMA_TOO_OLD
from portable_core.schema import migrations as M

__all__ = [
    "BMK_SCHEMA_DIR",
    "CURRENT_BMK_SCHEMA_VERSION",
    "available_migrations",
    "check_openable",
    "initialise",
    "verify_checksums",
]

BMK_SCHEMA_DIR: Final[Path] = Path(__file__).parent

#: The `.bmk` schema version this build writes and understands.
CURRENT_BMK_SCHEMA_VERSION: Final[int] = 1


def available_migrations() -> list[M.Migration]:
    """Every `.bmk` migration shipped with this build, in version order."""
    return M.available_migrations(BMK_SCHEMA_DIR)


def initialise(
    con: sqlite3.Connection, *, clock: Clock = SYSTEM_CLOCK, applied_by: str | None = None
) -> int:
    """Apply every `.bmk` migration to a fresh file. Returns the version."""
    for migration in available_migrations():
        M._apply(con, migration, clock, applied_by)
    return M.schema_version(con)


def verify_checksums(con: sqlite3.Connection) -> list[str]:
    """Migrations whose text no longer matches what was applied to this file."""
    on_disk = {m.version: m for m in available_migrations()}
    problems: list[str] = []
    for row in con.execute("SELECT version, name, checksum FROM schema_migration ORDER BY 1"):
        migration = on_disk.get(int(row["version"]))
        if migration is None:
            problems.append(
                f"migration {int(row['version']):04d} was applied to this file but is "
                "not shipped with this build of portable"
            )
        elif migration.checksum != row["checksum"]:
            problems.append(
                f"migration {int(row['version']):04d}_{row['name']} has been edited "
                "since it was applied: the DDL on disk no longer describes this file"
            )
    return problems


def check_openable(con: sqlite3.Connection, path: Path | None = None) -> None:
    """Refuse a `.bmk` file this build does not understand, as a `.port` is refused."""
    version = M.schema_version(con)
    where = str(path) if path else None
    tables = {
        str(r[0]) for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if version == 0 or "methodology" not in tables:
        raise PortfolioFileError(
            "file has no .bmk schema: it is empty, or not a .bmk file",
            code=E_PORTFOLIO_CORRUPT,
            remedy="Create an index with `bmk init FILE`.",
            path=where,
        )
    if version > CURRENT_BMK_SCHEMA_VERSION:
        raise PortfolioFileError(
            f"index file uses .bmk schema version {version}, but this build of portable "
            f"understands version {CURRENT_BMK_SCHEMA_VERSION}",
            code=E_SCHEMA_TOO_NEW,
            remedy="Upgrade portable. This build will not guess at what new columns mean.",
            file_version=version,
            supported_version=CURRENT_BMK_SCHEMA_VERSION,
            path=where,
        )
    if version < CURRENT_BMK_SCHEMA_VERSION:  # pragma: no cover -- one version so far
        raise PortfolioFileError(
            f"index file uses .bmk schema version {version}; this build reads "
            f"{CURRENT_BMK_SCHEMA_VERSION}",
            code=E_SCHEMA_TOO_OLD,
            remedy="There is no `bmk migrate` yet: it arrives with the second .bmk schema.",
            file_version=version,
            supported_version=CURRENT_BMK_SCHEMA_VERSION,
            path=where,
        )
