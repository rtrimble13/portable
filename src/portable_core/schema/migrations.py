"""Numbered, checksummed, forward-compatible migrations.

Rules, from the bootstrap (§5) and `CLAUDE.md`:

* Migrations are numbered and **never renumbered or edited once applied**. The
  checksum is what makes that enforceable rather than merely asked for.
* They are **idempotent** -- every statement uses ``IF NOT EXISTS`` or is
  guarded -- so a half-applied migration can be re-run.
* `pt` **refuses to open a file with a newer schema version than it
  understands**, and says exactly what to upgrade. Opening it read-anyway would
  mean interpreting columns whose meaning we do not know.
* `pt migrate` upgrades **with an automatic backup**, because a migration is
  the one operation that can lose a ledger.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from portable_core import __version__
from portable_core.errors import PortfolioFileError
from portable_core.errors.kinds import (
    E_MIGRATION_BLOCKED,
    E_MIGRATION_FAILED,
    E_PORTFOLIO_CORRUPT,
    E_SCHEMA_TOO_NEW,
)

_SCHEMA_DIR: Final[Path] = Path(__file__).parent
_FILENAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")

#: Marks a migration that alters a table SQLite can only alter by rebuilding it
#: -- a CHECK constraint, or a NOT NULL column with no default. ADR 0019.
REBUILD_MARKER: Final = "-- portable:rebuild"

#: The schema version this build of `portable` writes and understands. Bumped
#: by every migration, in the same commit as the migration and its CHANGELOG
#: entry.
CURRENT_SCHEMA_VERSION: Final[int] = 4


@dataclass(frozen=True, slots=True)
class Migration:
    """One numbered migration file."""

    version: int
    name: str
    path: Path
    sql: str

    @property
    def rebuilds(self) -> bool:
        """Whether this migration rebuilds a table. ADR 0019 §1.

        Declared by a marker comment in the file rather than in a registry, so
        the fact travels with the thing it describes. It is inside the
        checksummed text, so a migration cannot be quietly promoted to rebuild
        mode after it has been applied.
        """
        return any(line.strip() == REBUILD_MARKER for line in self.sql.splitlines()[:5])

    @property
    def checksum(self) -> str:
        """SHA-256 of the migration text, over normalised line endings.

        Normalised because the owner works on Windows and CI runs both: a
        checkout that converted LF to CRLF must not read as a tampered
        migration.
        """
        normalised = self.sql.replace("\r\n", "\n").encode("utf-8")
        return hashlib.sha256(normalised).hexdigest()


def available_migrations() -> list[Migration]:
    """Every migration shipped with this build, in version order."""
    found: list[Migration] = []
    for path in sorted(_SCHEMA_DIR.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if match is None:
            raise PortfolioFileError(
                f"migration filename does not match NNNN_name.sql: {path.name}",
                code=E_MIGRATION_FAILED,
                path=str(path),
            )
        found.append(
            Migration(
                version=int(match.group("version")),
                name=match.group("name"),
                path=path,
                sql=path.read_text(encoding="utf-8"),
            )
        )

    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise PortfolioFileError(
            f"duplicate migration versions: {versions}",
            code=E_MIGRATION_FAILED,
        )
    return found


def _has_migration_table(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migration'"
    ).fetchone()
    return row is not None


def applied_versions(con: sqlite3.Connection) -> list[int]:
    """Versions already applied to this file, in order."""
    if not _has_migration_table(con):
        return []
    rows = con.execute("SELECT version FROM schema_migration ORDER BY version").fetchall()
    return [int(r["version"]) for r in rows]


def schema_version(con: sqlite3.Connection) -> int:
    """The file's schema version: the highest applied migration, or 0."""
    applied = applied_versions(con)
    return applied[-1] if applied else 0


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def split_statements(script: str) -> list[str]:
    """Split a migration into individual statements.

    ``sqlite3.Connection.executescript`` cannot be used: it issues an implicit
    COMMIT before running, which would make a migration non-atomic exactly when
    atomicity matters most.

    Splitting on ``;`` naively would break every ``CREATE TRIGGER``, whose body
    contains semicolons. :func:`sqlite3.complete_statement` wraps SQLite's own
    ``sqlite3_complete()``, which knows that a trigger is incomplete until its
    ``END;`` -- so accumulating lines until it reports completeness is both
    correct and not our own parser to maintain.
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        stripped = line.lstrip()
        if not buffer and (not stripped.strip() or stripped.startswith("--")):
            continue  # leading comments and blank lines between statements
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise PortfolioFileError(
            f"migration ends with an incomplete statement: {buffer.strip()[:80]!r}",
            code=E_MIGRATION_FAILED,
        )
    return statements


def _duplicate_external_refs(con: sqlite3.Connection) -> list[str]:
    """Ledger rows that would collide under 0002's unique index.

    Reported before the migration runs, because the alternative is
    ``UNIQUE constraint failed: transaction.account_id, transaction.external_ref``
    from half way through, carrying a remedy -- restore the backup -- that
    reproduces the same duplicates and the same failure.
    """
    rows = con.execute(
        "SELECT account_id, external_ref, COUNT(*) AS n, "
        "GROUP_CONCAT(txn_id) AS ids "
        'FROM "transaction" WHERE external_ref IS NOT NULL '
        "GROUP BY account_id, external_ref HAVING COUNT(*) > 1 "
        "ORDER BY account_id, external_ref"
    ).fetchall()
    return [
        f"account {r['account_id']} has {r['n']} rows carrying external_ref "
        f"{r['external_ref']!r} (transactions {r['ids']})"
        for r in rows
    ]


#: Data conditions a migration needs before it can apply, by version.
#:
#: A migration is pure SQL and cannot branch, so a constraint added over
#: existing data either applies or fails with whatever SQLite says. That is a
#: poor answer for the one operation that can lose a ledger, so the condition is
#: checked first and reported in terms of the rows at fault.
_PRECONDITIONS: Final[dict[int, Callable[[sqlite3.Connection], list[str]]]] = {
    2: _duplicate_external_refs,
}

#: What to do about each, since "the file is unchanged" is not a remedy.
_PRECONDITION_REMEDY: Final[dict[int, str]] = {
    2: (
        "Two ledger rows in one account cannot claim the same external reference. "
        "Correct the duplicate with a reversing entry (`pt trade reverse`), or "
        "re-record it without `--ref`. The ledger is append-only, so the reference "
        "cannot simply be edited off the existing row."
    ),
}


def check_preconditions(con: sqlite3.Connection, migration: Migration) -> None:
    """Refuse a migration whose data would make it fail. Reads only.

    Raises:
        PortfolioFileError: with every offending row named, and a remedy that
            addresses the data rather than the migration.
    """
    check = _PRECONDITIONS.get(migration.version)
    if check is None:
        return
    problems = check(con)
    if not problems:
        return
    raise PortfolioFileError(
        f"migration {migration.version:04d}_{migration.name} cannot be applied to this "
        f"file: {len(problems)} precondition failure(s)",
        code=E_MIGRATION_BLOCKED,
        remedy=_PRECONDITION_REMEDY.get(migration.version, "Correct the data and retry."),
        version=migration.version,
        problems=problems,
    )


def _triggers(con: sqlite3.Connection) -> set[str]:
    """Every trigger in the file, by name.

    Compared across a rebuild because a rebuild drops and recreates them, and a
    file that lost `trg_transaction_no_update` looks entirely fine while no
    longer being append-only. No test of the migration's *data* would catch it
    (`CLAUDE.md` invariant 2).
    """
    return {
        str(row[0])
        for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }


def _check_foreign_keys(con: sqlite3.Connection, migration: Migration) -> None:
    """The enforcement a rebuild switches off, reinstated once at the end.

    ADR 0019 §1. `PRAGMA foreign_keys = OFF` is required by SQLite's own
    rebuild procedure and is a no-op inside a transaction, so it is issued
    before one opens. This is what stops that being a hole: the check is not
    skipped, it moves from per-statement to once-before-commit.
    """
    violations = list(con.execute("PRAGMA foreign_key_check"))
    if not violations:
        return
    named = [f"{row[0]} rowid {row[1]} -> {row[2]}" for row in violations[:20]]
    raise PortfolioFileError(
        f"migration {migration.version:04d}_{migration.name} left "
        f"{len(violations)} foreign key violation(s); the file is unchanged",
        code=E_MIGRATION_FAILED,
        remedy="This is a bug in the migration, not in the file. Report it.",
        version=migration.version,
        violations=named,
    )


def _apply(con: sqlite3.Connection, migration: Migration) -> None:
    """Apply one migration and record it. All or nothing."""
    # Before the transaction, so a refusal costs nothing and reads as a refusal
    # rather than as a failed write.
    check_preconditions(con, migration)
    if migration.rebuilds:
        # Outside the transaction, because the pragma is a documented no-op
        # inside one -- which is exactly why the runner had to change before a
        # rebuild migration could exist at all (ADR 0019).
        con.execute("PRAGMA foreign_keys = OFF")
    triggers_before = _triggers(con) if migration.rebuilds else set()
    try:
        _run(con, migration, triggers_before)
    finally:
        if migration.rebuilds:
            # `foreign_keys` is connection state, not file state. Leaving it OFF
            # after a failure would hand the caller a connection on which every
            # later write silently skips referential integrity -- in a CLI whose
            # very next action is often `pt rebuild`.
            con.execute("PRAGMA foreign_keys = ON")


def _run(con: sqlite3.Connection, migration: Migration, triggers_before: set[str]) -> None:
    con.execute("BEGIN IMMEDIATE")
    try:
        for statement in split_statements(migration.sql):
            con.execute(statement)
        if migration.rebuilds:
            _check_foreign_keys(con, migration)
            lost = triggers_before - _triggers(con)
            if lost:
                raise PortfolioFileError(
                    f"migration {migration.version:04d}_{migration.name} did not "
                    f"restore trigger(s) {', '.join(sorted(lost))}; the file is "
                    f"unchanged",
                    code=E_MIGRATION_FAILED,
                    remedy="This is a bug in the migration, not in the file. Report it.",
                    version=migration.version,
                    triggers=sorted(lost),
                )
        # `meta.schema_version` is a required key that `pt validate` checks and
        # `pt export` carries. Nothing updated it after a migration, which never
        # showed because 0001 was the only migration: every upgraded file would
        # have reported forever the version it was created at.
        con.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(migration.version),),
        )
        con.execute(
            "INSERT INTO schema_migration (version, name, checksum, applied_at, applied_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                migration.version,
                migration.name,
                migration.checksum,
                _now(),
                f"portable {__version__}",
            ),
        )
    except PortfolioFileError:
        # Raised by the two rebuild checks above, which have already said
        # precisely what is wrong. Rolled back and re-raised unchanged rather
        # than wrapped in a generic "migration failed".
        con.execute("ROLLBACK")
        raise
    except sqlite3.Error as exc:
        con.execute("ROLLBACK")
        raise PortfolioFileError(
            f"migration {migration.version:04d}_{migration.name} failed: {exc}",
            code=E_MIGRATION_FAILED,
            remedy="The file is unchanged. Restore from the backup if one was taken.",
            version=migration.version,
        ) from exc
    con.execute("COMMIT")


def initialise(con: sqlite3.Connection) -> int:
    """Apply every migration to a fresh file. Returns the resulting version."""
    for migration in available_migrations():
        _apply(con, migration)
    return schema_version(con)


def verify_checksums(con: sqlite3.Connection) -> list[str]:
    """Report migrations whose file no longer matches what was applied.

    A mismatch means somebody edited an applied migration. That is not a
    routine change -- it means the DDL on disk no longer describes the database
    in front of you, and the audit trail is broken. Reported by `pt validate`.
    """
    if not _has_migration_table(con):
        return []
    on_disk = {m.version: m for m in available_migrations()}
    problems: list[str] = []
    for row in con.execute(
        "SELECT version, name, checksum FROM schema_migration ORDER BY version"
    ):
        version = int(row["version"])
        migration = on_disk.get(version)
        if migration is None:
            problems.append(
                f"migration {version:04d} was applied to this file but is not shipped "
                "with this build of portable"
            )
        elif migration.checksum != row["checksum"]:
            problems.append(
                f"migration {version:04d}_{row['name']} has been edited since it was "
                "applied: the DDL on disk no longer describes this database"
            )
    return problems


def check_openable(con: sqlite3.Connection, path: Path | None = None) -> None:
    """Refuse a file this build does not understand.

    Forward compatibility is one-directional on purpose. A newer `portable`
    can migrate an older file. An older `portable` **cannot** read a newer one:
    it would be interpreting columns whose meaning it does not know, which is
    the silently-wrong-number failure mode with extra steps.
    """
    version = schema_version(con)
    if version > CURRENT_SCHEMA_VERSION:
        raise PortfolioFileError(
            f"portfolio uses schema version {version}, but this build of portable "
            f"understands version {CURRENT_SCHEMA_VERSION}",
            code=E_SCHEMA_TOO_NEW,
            remedy=(
                "Upgrade portable: `pip install --upgrade portable`. This build will "
                "not read a newer file, because it would be guessing at what the new "
                "columns mean."
            ),
            file_version=version,
            supported_version=CURRENT_SCHEMA_VERSION,
            path=str(path) if path else None,
        )
    if version == 0:
        raise PortfolioFileError(
            "file has no portable schema: it is empty, or not a .port file",
            code=E_PORTFOLIO_CORRUPT,
            remedy="Create a portfolio with `pt init`.",
            path=str(path) if path else None,
        )


def backup_path(path: Path) -> Path:
    """Where `pt migrate` writes its automatic backup.

    Timestamped rather than a single `.bak`, so a second migration cannot
    overwrite the backup taken before the first.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return path.with_name(f"{path.name}.{stamp}.backup")


def migrate(
    con: sqlite3.Connection,
    path: Path,
    *,
    backup: bool = True,
    dry_run: bool = False,
) -> tuple[int, int, list[Migration], Path | None]:
    """Bring *path* up to :data:`CURRENT_SCHEMA_VERSION`.

    Returns ``(from_version, to_version, applied, backup_file)``. Under
    ``dry_run`` nothing is written and ``applied`` is what *would* be applied.
    """
    check_openable(con, path)
    before = schema_version(con)
    pending = [m for m in available_migrations() if m.version > before]

    if dry_run or not pending:
        return before, before if not pending else pending[-1].version, pending, None

    backup_file: Path | None = None
    if backup:
        # WAL means the -wal and -shm files matter too. Checkpoint first so the
        # single-file copy is a complete, consistent database.
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        backup_file = backup_path(path)
        shutil.copy2(path, backup_file)

    for migration in pending:
        _apply(con, migration)

    return before, schema_version(con), pending, backup_file
