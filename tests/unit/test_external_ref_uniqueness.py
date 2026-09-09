"""One source row, one ledger row: the uniqueness of `external_ref`.

Migration 0002. Every ledger-writing command can now set a reference, so
nothing stopped the same source row being recorded twice -- which is how
re-importing an overlapping statement period silently doubles a book.

The index is the backstop that binds every writer. These tests cover the three
places the rule has to hold and the two places it must *not* over-reach: a row
with no reference, and the same reference in a different account.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from portable_core.domain.enums import TransactionType
from portable_core.domain.models import Account
from portable_core.errors import PortfolioFileError, ValidationError
from portable_core.errors.kinds import E_DUPLICATE_REF, E_MIGRATION_BLOCKED
from portable_core.persistence.repositories import Repositories
from portable_core.schema import migrations as M
from tests.conftest import append

pytestmark = pytest.mark.unit

ON = date(2024, 1, 10)


# ── the constraint's shape ───────────────────────────────────────────────────


def test_a_reference_cannot_be_reused_within_an_account(
    repos: Repositories, taxable_account: Account
) -> None:
    append(repos, taxable_account.account_id, TransactionType.DEPOSIT, ON, external_ref="wb:1")
    with pytest.raises(ValidationError) as caught:
        append(
            repos,
            taxable_account.account_id,
            TransactionType.DEPOSIT,
            ON,
            external_ref="wb:1",
        )
    assert caught.value.code == E_DUPLICATE_REF
    # Naming the row the caller already has is the point; a constraint name is
    # not an answer to "which one is this a duplicate of".
    assert "transaction 1" in caught.value.message


def test_the_same_reference_in_another_account_is_allowed(
    repos: Repositories, taxable_account: Account, ira_account: Account
) -> None:
    """Load-bearing, not a concession.

    `pt ca split --ref X` with no `--account` writes one row per holding
    account, all naming one corporate action. Global uniqueness would refuse a
    correct command and force invented suffixes for no gain.
    """
    append(repos, taxable_account.account_id, TransactionType.DEPOSIT, ON, external_ref="ca:1")
    append(repos, ira_account.account_id, TransactionType.DEPOSIT, ON, external_ref="ca:1")
    assert repos.transactions.count() == 2


def test_rows_without_a_reference_are_unconstrained(
    repos: Repositories, taxable_account: Account
) -> None:
    """The ordinary hand-entered case, which the partial index leaves alone."""
    for _ in range(3):
        append(repos, taxable_account.account_id, TransactionType.DEPOSIT, ON)
    assert repos.transactions.count() == 3


def test_the_lookup_finds_the_row_that_holds_a_reference(
    repos: Repositories, taxable_account: Account, ira_account: Account
) -> None:
    txn_id = append(
        repos, taxable_account.account_id, TransactionType.DEPOSIT, ON, external_ref="wb:9"
    )
    found = repos.transactions.with_external_ref(taxable_account.account_id, "wb:9")
    assert found is not None
    assert found.txn_id == txn_id
    # Scoped to the account, so the same reference elsewhere is not this row.
    assert repos.transactions.with_external_ref(ira_account.account_id, "wb:9") is None


# ── the migration's precondition ─────────────────────────────────────────────


def _portfolio_at_version_one(path: Path) -> sqlite3.Connection:
    """A file carrying only migration 0001, as one created before today would."""
    from portable_core.persistence.connection import open_portfolio

    con = open_portfolio(path, must_exist=False)
    first = next(m for m in M.available_migrations() if m.version == 1)
    for statement in M.split_statements(first.sql):
        con.execute(statement)
    con.execute(
        "INSERT INTO schema_migration (version, name, checksum, applied_at, applied_by) "
        "VALUES (1, 'initial', ?, '2024-01-01T00:00:00Z', 'test')",
        (first.checksum,),
    )
    con.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
    return con


def test_a_file_with_duplicate_references_is_refused_before_anything_runs(
    tmp_path: Path,
) -> None:
    """The alternative is `UNIQUE constraint failed` from half way through.

    Worse than cryptic: `_apply`'s remedy tells you to restore the backup, which
    reproduces the same duplicates and the same failure. For the one operation
    that can lose a ledger that is not good enough.
    """
    con = _portfolio_at_version_one(tmp_path / "old.port")
    con.execute(
        "INSERT INTO account (account_id, name, account_type, opened_date, created_at, "
        "updated_at) VALUES (1, 'B', 'taxable', '2024-01-02', 'x', 'x')"
    )
    for seq in (1, 2):
        con.execute(
            'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, '
            "net_cash_effect, external_ref, created_at) "
            "VALUES (1, '2024-01-10', ?, 'deposit', '100.00', 'wb:dup', 'x')",
            (seq,),
        )
    con.commit()

    with pytest.raises(PortfolioFileError) as caught:
        M.migrate(con, tmp_path / "old.port", backup=False)

    assert caught.value.code == E_MIGRATION_BLOCKED
    problems = caught.value.context["problems"]
    assert len(problems) == 1
    # The rows at fault, by id, so the remedy can actually be carried out.
    assert "wb:dup" in problems[0]
    assert "1,2" in problems[0]
    con.close()


def test_the_refused_migration_leaves_the_file_at_its_old_version(
    tmp_path: Path,
) -> None:
    """A refusal is not a partial upgrade."""
    con = _portfolio_at_version_one(tmp_path / "old.port")
    con.execute(
        "INSERT INTO account (account_id, name, account_type, opened_date, created_at, "
        "updated_at) VALUES (1, 'B', 'taxable', '2024-01-02', 'x', 'x')"
    )
    for seq in (1, 2):
        con.execute(
            'INSERT INTO "transaction" (account_id, trade_date, seq, txn_type, '
            "net_cash_effect, external_ref, created_at) "
            "VALUES (1, '2024-01-10', ?, 'deposit', '100.00', 'wb:dup', 'x')",
            (seq,),
        )
    con.commit()

    with pytest.raises(PortfolioFileError):
        M.migrate(con, tmp_path / "old.port", backup=False)
    assert M.schema_version(con) == 1
    con.close()


def test_a_clean_file_migrates_and_meta_keeps_step(
    tmp_path: Path,
) -> None:
    """`meta.schema_version` is a required key that nothing updated.

    It never showed, because 0001 was the only migration there had ever been:
    every upgraded file would have reported forever the version it was created
    at, and `pt export` would have carried that claim out of the file.
    """
    path = tmp_path / "old.port"
    con = _portfolio_at_version_one(path)
    con.commit()
    assert M.schema_version(con) == 1

    before, after, applied, _ = M.migrate(con, path, backup=False)
    head = M.CURRENT_SCHEMA_VERSION
    # Asserted against the head rather than a hard-coded number: the claim is
    # that `meta` keeps step with whatever was applied, and pinning a version
    # here would make this fail on every future migration for no reason.
    assert (before, after) == (1, head)
    assert [m.version for m in applied] == list(range(2, head + 1))
    assert M.schema_version(con) == head
    stored = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert stored[0] == str(head)
    con.close()


def test_the_migration_is_idempotent(tmp_path: Path) -> None:
    """Every statement is guarded, so a re-run is a no-op rather than an error."""
    path = tmp_path / "old.port"
    con = _portfolio_at_version_one(path)
    con.commit()
    second = next(m for m in M.available_migrations() if m.version == 2)
    for _ in range(2):
        for statement in M.split_statements(second.sql):
            con.execute(statement)
    con.close()


def test_the_superseded_index_is_gone_and_the_unique_one_is_there(
    repos: Repositories,
) -> None:
    """0002 replaces the lookup index rather than leaving both."""
    names = {
        r["name"]
        for r in repos.con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'transaction'"
        )
    }
    assert "ux_txn_account_external_ref" in names
    assert "ix_txn_external_ref" not in names


# ── the fourth entry point: `pt import` ──────────────────────────────────────


def test_an_export_with_duplicate_references_is_refused_before_any_insert(
    repos: Repositories,
) -> None:
    """`pt import` builds a fresh file at the current schema.

    So the unique index exists from the first row, and without this check the
    failure arrives part-way through the inserts -- against a file that already
    exists on disk and is missing most of its history.
    """
    payload = {
        "account": [
            {
                "account_id": 1,
                "name": "B",
                "account_type": "taxable",
                "opened_date": "2024-01-02",
                "created_at": "x",
                "updated_at": "x",
            }
        ],
        "transaction": [
            {
                "txn_id": 1,
                "account_id": 1,
                "trade_date": "2024-01-10",
                "seq": 1,
                "txn_type": "deposit",
                "net_cash_effect": "100.00",
                "external_ref": "wb:dup",
                "created_at": "x",
            },
            {
                "txn_id": 2,
                "account_id": 1,
                "trade_date": "2024-01-10",
                "seq": 2,
                "txn_type": "deposit",
                "net_cash_effect": "100.00",
                "external_ref": "wb:dup",
                "created_at": "x",
            },
        ],
    }
    with pytest.raises(ValidationError) as caught:
        repos.import_tables(payload)
    assert caught.value.code == E_DUPLICATE_REF
    assert "transactions 1, 2" in caught.value.context["duplicates"][0]
    # Nothing was written: the check runs before the first insert.
    assert repos.transactions.count() == 0


def test_an_export_reusing_a_reference_across_accounts_imports_fine(
    repos: Repositories, taxable_account: Account, ira_account: Account
) -> None:
    """The same scoping as the index, so a legitimate export is not refused.

    A corporate action recorded across two accounts under one reference is
    exactly what `pt ca split --ref` produces, and it has to survive an export
    and re-import unchanged.
    """

    def row(txn_id: int, account_id: int, seq: int, ref: str | None) -> dict[str, object]:
        return {
            "txn_id": txn_id,
            "account_id": account_id,
            "trade_date": "2024-01-10",
            "seq": seq,
            "txn_type": "deposit",
            "net_cash_effect": "100.00",
            "external_ref": ref,
            "created_at": "2024-01-10T00:00:00Z",
        }

    counts = repos.import_tables(
        {
            "transaction": [
                row(1, taxable_account.account_id, 1, "ca:1"),
                row(2, ira_account.account_id, 2, "ca:1"),
                row(3, taxable_account.account_id, 3, None),
                row(4, taxable_account.account_id, 4, None),
            ]
        }
    )
    assert counts["transaction"] == 4
    assert repos.transactions.count() == 4
