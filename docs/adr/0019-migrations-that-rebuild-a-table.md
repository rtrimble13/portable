# ADR 0019 — A migration may rebuild a table, and what that costs

- **Status:** Proposed
- **Date:** 2026-09-09
- **Milestone:** v0.2
- **Governs:** `CLAUDE.md` invariants 2, 3
- **Required by:** [ADR 0015](0015-in-kind-transfers-and-opening-positions.md) (two new `txn_type` values), [ADR 0017](0017-cutover-reconstruction-and-basis-provenance.md) (`lot.basis_source NOT NULL`)

## Context

ADR 0015 adds `transfer_in` and `transfer_out` to `txn_type`. That column is
constrained by a `CHECK (txn_type IN (...))`, and **SQLite cannot alter a
`CHECK` constraint.** The only way to change one is the twelve-step procedure in
SQLite's own `ALTER TABLE` documentation: create a new table with the wanted
shape, copy the rows, drop the old table, rename the new one, and recreate every
index and trigger.

On `"transaction"` that is not a routine operation. It is the ledger:

- `CLAUDE.md` invariant 2 makes it append-only, enforced by two triggers that
  `RAISE(ABORT)` on `UPDATE` and `DELETE`. A rebuild must drop and recreate them,
  and a rebuild that recreated them wrongly would silently remove the guarantee
  that makes the tax trail defensible.
- It is the one table in the file that cannot be reconstructed from anything
  else. Everything else is derived (invariant 3).
- Several tables hold foreign keys into it, **and the ledger holds two into
  itself** — `related_txn_id` and `reverses_txn_id`.

That last point is the actual blocker, and it is worth being exact about
because the obvious statement of the problem is wrong. The externally
referencing tables — `lot`, `position`, `realized_gain`, `snapshot_flow` — are
all derived, so a migration can simply clear them first and they stop being an
obstacle. **The self-references cannot be cleared: they are ledger data.** A
rebuild copies rows carrying `reverses_txn_id` into the new table, and dropping
the old one then orphans them.

Measured rather than assumed, on a v2 file with migration 0003's own SQL and
the marker removed:

| file | rebuild mode | result |
|---|---|---|
| no self-referencing rows | off | succeeds |
| one reversal | off | `FOREIGN KEY constraint failed` |
| either | on | succeeds, `foreign_key_check` clean |

So a portfolio with a single reversal, spinoff, or option roll cannot be
migrated without this, and one without them can — which is the worst possible
shape for a bug: it would work on the fixture and on a new file, and fail on
the owner's real portfolio.

SQLite's procedure requires `PRAGMA foreign_keys = OFF` **outside any
transaction**; the pragma is a documented no-op while one is open.
`migrations._apply` opens `BEGIN IMMEDIATE` *before* executing a migration's
statements, so a `.sql` file cannot turn foreign keys off by itself.

So the runner has to change before the migration can exist. That is a change to
the one operation that can lose a ledger, which is why it gets an ADR rather
than a commit message.

## Decision

### 1. A migration declares that it rebuilds, and the runner obeys the declaration

A migration whose first line is the marker comment

```sql
-- portable:rebuild
```

is run in **rebuild mode**. The marker is in the file, not in a registry, for
the same reason the checksum is over the file: the fact travels with the thing
it describes, and a migration moved or re-read carries its own requirements.

Rebuild mode changes exactly three things and nothing else:

1. `PRAGMA foreign_keys = OFF` is issued **before** the transaction opens, and
   restored to `ON` after it closes — on the success and the failure path alike.
2. `PRAGMA foreign_key_check` runs **inside** the transaction, after the
   migration's statements and before the commit. Any row it returns aborts the
   migration and names the table, the rowid, and the constraint. This is what
   replaces the enforcement that was switched off: the check is not skipped, it
   is moved from per-statement to once-at-the-end, which is the whole point of
   the procedure.
3. The trigger inventory is compared before and after. A rebuild that lost
   `trg_transaction_no_update` would leave a file that looks fine and is no
   longer append-only, and no test of the migration's *data* would catch it.

Everything else is unchanged: same checksum rule, same preconditions, same
automatic backup, same all-or-nothing transaction, same refusal to open a file
whose schema is newer than the build.

### 2. Foreign keys are restored even when the migration fails

`PRAGMA foreign_keys` is connection state, not file state. A migration that
raised and left it `OFF` would hand the caller a connection on which every
subsequent write silently skips referential integrity — for the lifetime of the
process, in a CLI whose next action is often `pt rebuild`. The restore is in a
`finally`, and there is a test that asserts the pragma is back `ON` after a
failed rebuild.

### 3. One migration, not two

`0003` carries both ADR 0015's transaction columns and ADR 0017's
`lot.basis_source`. They are one change: a `transfer_in` row is the thing that
seeds a lot whose basis is not `derived`, and shipping the transaction type
without the provenance column would mean a release in which a seeded lot cannot
say where its basis came from — which is the exact failure ADR 0017 exists to
prevent, available in a released version.

The two halves are not equally risky and the migration says so:

- **`"transaction"` is rebuilt**, preserving every row. This is the delicate
  half.
- **`lot` and its dependants are dropped and recreated empty.** Lots are derived
  state (invariant 3), so the migration does not copy them; `pt migrate` reports
  that a rebuild is required and `pt validate` fails until it is run. Adding a
  `NOT NULL` column with no default to a populated table is impossible in SQLite
  anyway — and inventing a default would defeat the column, whose entire purpose
  is that a writer cannot create a lot without answering the question.

### 4. `basis_source` is stored on the ledger row as well as on the lot

The lot's `basis_source` cannot be derived from anything else in the row: the
difference between `reconstructed`, `estimated` and `unavailable` is an
assertion by whoever built the lot, not a consequence of its numbers. Invariant
3 requires derived state to be reproducible by replaying the ledger, so the
assertion has to be *in* the ledger. `"transaction"` therefore gains
`basis_source` and `basis_assumption`, bound by `CHECK` to `transfer_in` and
`transfer_out`, and the replay copies them onto the lot it opens. Every other
opening transaction produces `derived`.

## Consequences

- `pt migrate` gains a visible extra step on a rebuild migration, and its
  backup becomes load-bearing rather than precautionary. The output says which
  tables were rebuilt and which were dropped for regeneration.
- A file migrated to `0003` has no lots until `pt rebuild` runs. That is a
  deliberate, reported, recoverable state and not a silent one: `pt validate`
  fails with the remedy, per invariant 9.
- The rebuild capability, once it exists, will be reached for again. That is the
  argument for putting the mechanics in the runner with its own tests rather
  than writing bespoke SQL in each migration that needs it.
- `docs/schema.md` and `examples/sample.port` are regenerated. The fixture is
  generated, so this is `make fixtures`, not an edit.
- The marker is parsed from the migration text, which is inside the checksummed
  content — so a migration cannot be silently promoted to rebuild mode after it
  has been applied.

## Alternatives considered

- **Drop the `CHECK` constraint and validate `txn_type` in Python.** Rejected.
  The `CHECK` is what makes an invalid type impossible for *every* writer,
  including a future one nobody has written yet and anything that opens the file
  with the `sqlite3` CLI. Moving it into application code is the same trade as
  moving the append-only triggers into application code, and `CLAUDE.md` already
  refused that one.
- **Run rebuild migrations outside a transaction entirely.** Rejected: it gives
  up all-or-nothing on the operation that most needs it. A half-rebuilt ledger
  with the old table dropped and the new one partly filled is unrecoverable
  except from the backup.
- **A separate `pt migrate --rebuild` command the user must invoke.** Rejected:
  it makes correct upgrading a thing the user has to know about, and the failure
  mode of forgetting is a file that will not open. The migration knows what it
  needs; the runner should read that rather than ask.
- **Keep `foreign_keys` on and use `PRAGMA defer_foreign_keys`.** Tried, and it
  does not work: `defer_foreign_keys` defers enforcement to commit time but the
  `DROP TABLE` still fails immediately against the referencing rows. Measured,
  not assumed.
- **Clear the derived tables and hope that is enough.** This is the one that
  would have shipped if the blocker had not been measured: it *does* work on a
  file with no reversals, spinoffs or option rolls — which includes the
  generated fixture and every freshly created portfolio. It fails on a real
  ledger. A migration that passes CI and breaks the owner's file is the worst
  available outcome, and the reason the table above is in this ADR rather than
  a sentence asserting the problem.
- **Two migrations, 0003 and 0004.** Rejected under §3: they are one change, and
  splitting them creates a version in which a seeded lot cannot state its
  provenance.
