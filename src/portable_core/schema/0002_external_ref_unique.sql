-- portable .port schema, version 2.
--
-- @schema_version 2
-- @name external_ref_unique
--
-- `transaction.external_ref` is the source row's identity: a broker confirm id
-- where one exists, and otherwise a key an importer synthesizes from the source
-- row (ADR 0012). Every ledger-writing command can now set it. Nothing stopped
-- the same reference being recorded twice, so re-importing an overlapping
-- statement period would silently double the book.
--
-- The uniqueness is scoped **per account**, not globally, and that is
-- deliberate. `pt ca split --ref X` with no `--account` writes one ledger row
-- per holding account, all naming the same corporate action: one announcement,
-- one reference, several accounts. Global uniqueness would refuse it and force
-- invented suffixes for no gain. Imported rows are unaffected either way --
-- ADR 0012's synthesized key already hashes the account in.
--
-- The index is **partial**. A row with no reference is the ordinary
-- hand-entered case and must stay unconstrained; SQLite treats NULLs as
-- distinct in a unique index regardless, but saying so keeps the index to the
-- rows that carry a reference and makes the intent legible.
--
-- `source` is deliberately NOT part of the key. A hand-entered row and an
-- imported row claiming the same reference in one account SHOULD collide --
-- that is exactly the case where somebody typed in a transaction the importer
-- is about to add again.
--
-- A file that already holds duplicates cannot take this index. That is checked
-- before the migration runs rather than discovered as `UNIQUE constraint
-- failed` half way through -- see `_PRECONDITIONS` in `migrations.py`.

-- Superseded by the unique index below, which serves the same lookups.
DROP INDEX IF EXISTS ix_txn_external_ref;

CREATE UNIQUE INDEX IF NOT EXISTS ux_txn_account_external_ref
    ON "transaction" (account_id, external_ref) WHERE external_ref IS NOT NULL;
