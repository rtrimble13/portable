# The `.port` file

One portfolio, one file. Everything needed for analysis is inside it — hand
somebody the file and they have the whole book of record.

Column-by-column reference: [`schema.md`](schema.md), generated from the DDL
comments by `make docs` so it cannot drift.

---

## Physical format

A SQLite database, opened with:

| Pragma | Value | Why |
|---|---|---|
| `foreign_keys` | `ON` | Off by default in SQLite. Without it, every `REFERENCES` clause is decoration. |
| `journal_mode` | `WAL` | Concurrent readers alongside one writer, and a crash-safe journal. |
| `synchronous` | `FULL` | This file is a tax record. An fsync per commit is worth paying for. |
| `busy_timeout` | `5000` | Wait for a writer rather than failing instantly. |

Set in exactly one place — `persistence/connection.py` — which is also the only
module permitted to register the `Decimal` adapter.

**WAL means the sidecar files matter.** A plain copy of a `.port` file while the
`-wal` file has uncommitted pages can miss recent commits. `pt backup`
checkpoints first, which is why it exists rather than the docs just saying "copy
the file".

---

## Four classes of table

The distinction drives `pt rebuild`, and it is the whole architecture in one
table ([ADR 0010](adr/0010-derived-state-and-replay.md)).

| Class | Tables | Mutability | Rebuilt? |
|---|---|---|---|
| **Ledger** | `transaction` | Append-only, enforced by trigger | Never touched |
| **Reference** | `instrument*`, `price`, `corporate_action` | Immutable once written | Never touched |
| **Config** | `account`, `tax_rate_schedule`, `benchmark*`, `return_policy`, `portfolio_event`, `meta`, `settings`, `report_issue` | Effective-dated, immutable once effective | Never touched |
| **Derived** | `position`, `position_leg`, `lot`, `lot_basis_adjustment`, `lot_disposition`, `realized_gain`, `cash_balance`, `valuation_snapshot*`, `snapshot_flow` | Materialized for speed | **Dropped and rebuilt** |

If a table is not in the derived row, `pt rebuild` must not write to it. If you
add derived state, you add it to the replay path **and** to the replay test in
the same commit.

---

## The ledger is append-only

Database triggers reject `UPDATE` and `DELETE` on `transaction`:

```
PT-E-LEDGER-IMMUTABLE: the ledger is append-only; correct with a reversing
entry (pt trade reverse), never by editing history
```

Enforced by the **database**, not by convention, so no code path — including one
nobody has written yet, and including the `sqlite3` shell — can edit history.
That is what makes the tax trail defensible and what makes `--as-of` time travel
mean something.

A mistake is corrected with a **reversing entry plus a new entry**. All three
stay visible in `pt activity`; derived state reflects only the net effect,
because replay nets the reversal against what it reverses.

### Ledger order

`(trade_date, seq, txn_id)`.

**Not `created_at`** — a wall clock is not a total order across machines, and
depending on one would break the determinism `CLAUDE.md` invariant 6 promises.
**Not `trade_date` alone** — two trades on one day in the wrong order consume
the wrong lots under FIFO.

`seq` is assigned at insert from the existing maximum for that date, so a
back-dated entry lands *after* same-day entries already recorded. That is the
honest ordering: the ledger records when we learned things.

---

## Money is `TEXT`

Every money, quantity, price, and rate column is `TEXT` holding the canonical
decimal form, marked `-- decimal` in the DDL.

**There is no `REAL`, `FLOAT`, `DOUBLE`, or `NUMERIC` column**, asserted twice:
by the lint rule over the DDL text, and by a test that reads what SQLite
actually built — affinity is inferred rather than declared, and the two can
disagree.

`NUMERIC` is on that list deliberately. SQLite's `NUMERIC` affinity silently
stores as `REAL` anything it cannot hold as an integer, which is the exact bug
this rule exists to prevent, wearing a disguise.

Consequence worth knowing: **money does not sort correctly as text.** The
canonical form preserves trailing zeros, so `'10.500' < '10.6'` compares as
text and not as money. Ordering by money happens in Python, in `Decimal`, in the
service layer — which is where HIFO and LOFO live anyway.

---

## Constraints that carry invariants

Each of these binds every writer, including `pt import` and any future broker
adapter — not just the code paths somebody remembered to guard.

| Constraint | Requirement |
|---|---|
| `UPDATE`/`DELETE` triggers on `transaction` | `CLAUDE.md` invariant 2 |
| A fee with a `NULL` `fee_class` is rejected | `PORT-GIPS-D01` |
| `benchmark.return_type` is `NOT NULL` with **no default** | `PORT-GIPS-G01` |
| A transfer must carry a `counter_account_id`, and nothing else may | ADR 0007 |
| A reversal must name what it reverses | `CLAUDE.md` invariant 2 |
| `price.valuation_level` confined to 1–5 | `PORT-GIPS-A02` |
| Large and significant flow thresholds are separate fields | `PORT-GIPS-E09` |

The fee constraint works because fees are always stored as canonical 2dp, so
`'0.00'` is an exact test for "no fee" and the `CHECK` can bind without numeric
comparison in SQL.

---

## Migrations

Numbered `NNNN_name.sql` files in `src/portable_core/schema/`, applied in order
and recorded in `schema_migration` with a **checksum**.

- **Never renumbered, never edited once applied.** The checksum makes that
  enforceable rather than merely asked for: `pt validate` reports a migration
  whose file no longer matches what was applied, because the DDL on disk no
  longer describes the database in front of you.
- **Idempotent** — every statement uses `IF NOT EXISTS`, so a half-applied
  migration can be re-run.
- **`pt migrate` takes an automatic backup**, checkpointing the WAL first. A
  migration is the one operation that can lose a ledger.

**Forward compatibility is one-directional.** A newer `portable` migrates an
older file. An older `portable` **refuses** a newer one
(`PT-E-SCHEMA-TOO-NEW`, exit 3) rather than interpreting columns whose meaning
it does not know — which would be the silently-wrong-number failure mode with
extra steps.

Migrations are applied statement by statement inside one transaction, split with
`sqlite3.complete_statement`. `executescript` cannot be used: it issues an
implicit `COMMIT` before running, which would make a migration non-atomic
exactly where atomicity matters. And splitting on `;` naively would break every
`CREATE TRIGGER`, whose body contains semicolons — `sqlite3_complete()` knows a
trigger is incomplete until its `END;`.

---

## Export and import

```bash
pt --port p.port export -o dump.json
pt import dump.json --into new.port
```

Human-readable, diffable JSON of every **non-derived** table, ordered by primary
key so two exports of the same portfolio are byte-identical.

Derived state is deliberately excluded: it is reproducible from the ledger,
including it would double the file size, and a round-trip that carried it could
*hide* a replay bug rather than expose one. `pt import` rebuilds derived state
from the imported ledger, so the round trip exercises replay rather than
bypassing it.

**export → import → export produces identical bytes.** There is an integration
test asserting exactly that, and it also checks the tax figures agree on both
sides.

---

## Backups

```bash
pt --port p.port backup                    # timestamped, alongside the file
pt --port p.port backup -o /elsewhere.port
```

Timestamped rather than a single `.bak`, so a second backup cannot overwrite the
one taken before the first. `pt migrate` takes one automatically.

---

## What is not in the file

- **Secrets.** No DSN, no API key, no real account number — only an alias.
  Credentials come from the environment.
- **User configuration.** `~/.portablerc` is user-scoped and separate.
- **The flow and materiality thresholds are, deliberately, *in* the file** —
  see `return_policy`. They are effective-dated rows rather than configuration
  because they must be reconstructible for historical periods
  (`PORT-GIPS-B03`, `E09`). A threshold living in `~/.portablerc` could not be
  recovered for a period that ended two years ago.
