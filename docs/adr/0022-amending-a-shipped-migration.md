# ADR 0022 — Amending a shipped migration, and the one it was needed for

- **Status:** Accepted — implemented: migration 0004 amended, `SUPERSEDED_CHECKSUMS` in `schema/migrations.py`, and the index inventory ADR 0019 left open
- **Date:** 2026-10-15
- **Milestone:** v0.2
- **Governs:** `CLAUDE.md` invariants 2, 3; the migration rules in `schema/migrations.py`
- **Amends:** [ADR 0019](0019-migrations-that-rebuild-a-table.md) — adds the index inventory to a rebuild's checks

## Context

Migration 0004 rebuilds `"transaction"` to admit two new `txn_type` values
(ADR 0019's mechanism). Its `INSERT … SELECT` copied the column list from
0003's, which had copied from the v2 table. That list was correct in 0003 and
wrong in 0004: the v3 table has four more columns, the in-kind basis
provenance of ADRs 0015 and 0017 (`original_basis`, `original_acquired_date`,
`basis_source`, `basis_assumption`).

Two consequences, found by reading the code and confirmed by running it:

- **A v3 file holding any `transfer_in` row cannot upgrade.** The copy writes
  `NULL` into `basis_source`, the rebuilt table's `CHECK` refuses it, and the
  migration rolls back with the file still at v3. Every portfolio seeded by
  `pt import broker` (ADR 0017) or `pt transfer in` is in that set. This is a
  loud failure, and nothing was lost.
- **Nothing was lost anywhere else either.** Only `record_transfer_in` writes
  those four columns; `record_transfer_out` and every other writer leave them
  `NULL`. Every v3 row carrying a value in them is therefore a `transfer_in`,
  and the `CHECK` refused the copy of each one. A file that 0004 did upgrade
  had nothing in those columns to drop.

The migration rules say a migration is never edited once applied, and the
checksum is what enforces it. `pt validate` reports a migration whose text no
longer matches the checksum a file recorded. Any file at v4 has applied 0004,
including every file `pt init` has created since 0004 shipped.

## Decision

1. **A shipped migration may be amended when, and only when, the amended text
   leaves every file the original could migrate with the same schema and the
   same rows.** If that does not hold, the fix is a new migration.
2. **The original's checksum is recorded and still accepted.**
   `SUPERSEDED_CHECKSUMS[version]` maps each superseded checksum to the reason
   it was superseded. `verify_checksums` accepts those and no others, so a
   file still records which text it actually ran.
3. **The condition is tested by applying both texts, not argued.** The
   original text is kept as a fixture
   (`tests/fixtures/migrations/0004_capital_gain_distributions.as_first_shipped.sql`).
   A test applies it and the amended text to identical v3 files, then compares
   the full `sqlite_master` and every ledger row. A second test pins the
   recorded checksum to the fixture, so the fixture is provably what shipped.
4. **The amended file says it was amended**, in its header, with this ADR's
   number, so a reader of the SQL learns it there rather than from history.
5. **A rebuild must restore every explicitly created index, as well as every
   trigger.** ADR 0019 checked triggers across a rebuild and left indexes
   open. 0004 recreates `ux_txn_account_external_ref` by hand, and losing it
   would have passed every data test while quietly re-admitting the duplicate
   references migration 0002 exists to refuse. SQLite's own
   `sqlite_autoindex_*` entries are excluded: they follow from the rebuilt
   table's `UNIQUE` and `PRIMARY KEY` clauses and are named after the table
   they were created on.

## Consequences

- v3 files with in-kind transfers upgrade, keeping every provenance value.
- v4 files that ran the original 0004 keep passing `pt validate`, and their
  `schema_migration` row still records the original checksum.
- The amendment exception covers one narrow case: a defect that could only
  ever fail loudly. A migration whose defect could have written a wrong value
  into a file that then upgraded is not equivalent, and does not qualify.

## Alternatives considered

- **Leave 0004 alone and fix it in 0005.** Not possible. A v3 file with a
  `transfer_in` never gets past 0004, so 0005 would never run for the files
  that need it.
- **Edit 0004 and let `pt validate` report it on every v4 file.** That trains
  the owner to ignore the one report that says the DDL no longer describes
  the database, which is a worse outcome than the defect.
- **Teach the runner to skip 0004 for affected files and apply a corrected
  copy under another number.** Two histories for one schema version, and a
  `schema_migration` table that no longer says what ran. Rejected.
