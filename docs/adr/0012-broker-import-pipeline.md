# ADR 0012 — Broker import is a staged pipeline with a human gate, not a command

- **Status:** Accepted — all three stages implemented: `pt import broker` extracts (and `--incremental` re-extracts, with the overlap shown as skips), the batch file is the review, `pt import batch` commits
- **Date:** 2026-09-08
- **Milestone:** v0.2
- **Governs:** `CLAUDE.md` invariants 2, 9, 10; `PORT-GIPS-D01`, `PORT-GIPS-J03`

## Context

The owner has three real accounts at one adviser — a taxable brokerage, a
traditional IRA, and a Roth IRA — and wants them in a `.port` file. The adviser
supplies three spreadsheet exports: a transaction history, a capital-flows
report, and a holdings snapshot. `docs/broker-import.md` records what they
actually contain.

The exports are the only machine-readable source available. They have no
transaction identifier, no symbol column on the transaction rows, a settlement
date column in which the majority of populated cells are impossible, an activity
vocabulary whose members do not map one-to-one onto `TransactionType`, and — in
the file named for capital flows — rows that are not capital flows. A third of
the transaction file is sweep bookkeeping that must be discarded rather than
recorded.

Two properties of this repository make the naive shape — parse the file, insert
the rows — unavailable:

- **The ledger is append-only** (invariant 2). A row inserted wrongly is
  corrected with a reversing entry plus a replacement, forever visible. The cost
  of a bad row is not "fix it"; it is "carry the correction in the audit trail
  for the life of the portfolio".
- **A silently wrong number is the worst failure mode** (`CLAUDE.md`). Every
  mapping ambiguity in these files has a plausible wrong answer that reconciles
  at some level and is wrong at another: sweep echoes double income while
  leaving cash correct; the adviser's cross-account fee arrangement double-counts
  a fee while leaving each account's cash correct.

## Decision

Import is **three stages with a reviewable artifact between the first and the
last**, and the stages are separately invocable:

```
broker export ──[extract]──▶ batch file ──[review]──▶ ──[commit]──▶ ledger
   .xlsx / .csv              .json, in-repo         validate       + rebuild
                             diffable, editable
```

### Stage 1 — extract

`pt import broker <file> --broker <name> --account <acct> -o batch.json`

Reads the export, applies the adapter's column map and activity map, and writes
a **canonical batch file**. Writes nothing to the portfolio. This stage owns
every broker-specific concern; nothing downstream of it knows what a spreadsheet
is.

The batch file holds one object per prospective ledger row, each carrying the
`portable` fields it will become **and** the source row it came from, verbatim.
Carrying the source is what makes a review possible without opening the original
in a spreadsheet, and what makes the mapping auditable afterwards.

Rows the adapter deliberately discards are written to the batch too, marked
`action: "drop"` with the rule that dropped them. **A discarded row is a decision
and must be visible as one.** Four hundred silently vanished sweep rows and four
hundred deliberately dropped sweep rows are the same file and completely
different claims.

### Stage 2 — review

The batch file is the artifact the owner reads. It is JSON, ordered
deterministically, and diffable, so re-extracting after an adapter change shows
exactly what moved. Corrections are made **in the batch file**, not in the
ledger afterwards — which is the entire point, because a correction here costs a
text edit and a correction after commit costs a reversing entry.

### Stage 3 — commit

`pt import batch batch.json`

Validates, appends every row inside **one** database transaction, then runs a
**full `ReplayEngine.rebuild()`** in the same transaction. Never the incremental
`apply_transaction` path: a historical batch is by definition out of order
relative to whatever the file already holds, and ADR 0016 records why that path
is wrong for out-of-order rows.

`--dry-run` runs stages 3's validation and arithmetic and writes nothing, so the
refusals below are all reachable without touching the portfolio.

### Identity, since the broker supplies none

The exports carry no confirm number. `transaction.external_ref` is therefore
**synthesized** and must be deterministic across re-exports:

```
external_ref = "wb:" + sha256(
    account | trade_date | activity | description | quantity | amount | ordinal
)[:16]
```

where `ordinal` is the row's index among otherwise-identical rows in the same
export, so that two identical dividends on one day do not collide. The
components are the source row's raw text, not the mapped values, so a change to
the activity map does not change the identity of a row already committed.

`UNIQUE (account_id, external_ref)` enforces this at the schema — added in
migration 0002, scoped per account so that one corporate action recorded across
several accounts under one reference is still legal. Duplicate detection
**reports and refuses**; it never silently skips, because a silent skip cannot be
distinguished from a row the adapter failed to produce.

That makes one ordinary workflow need a home: re-importing an overlapping
statement period, which is a thing people do on purpose. It belongs at
**extract**, not commit. A row already in the ledger is written to the batch as
`action: "skip"` with the reason, so the overlap appears in the artifact under
review — consistent with this ADR's rule that a discarded row is a visible
decision. A duplicate that survives to commit is by definition unexpected, and
refuses.

### The maps are versioned repository artifacts, not adapter internals

Two files under `src/portable_core/importers/<broker>/`:

- **`activity_map.toml`** — every distinct activity string the broker emits,
  mapped to a `TransactionType` and a handling rule. No default arm. An
  unmapped string is a refusal naming the string and the row.
- **`instruments.toml`** — the name-to-symbol crosswalk, needed because the
  transaction rows carry a description and no symbol.

They are data, reviewed as data, with a test asserting the adapter's fixtures
are fully covered by both. Burying either in Python would make the highest-risk
part of this work the least reviewable part.

### Refusals

Stage 3 refuses, per `CLAUDE.md` invariant 9, on: an unmapped activity string;
an unresolvable or ambiguous instrument name; a fee whose `fee_class` the map
does not determine (`PORT-GIPS-D01`); a duplicate `external_ref`; a settlement
date earlier than its trade date; a closing quantity exceeding open lots; a
fractional quantity in an account without `allows_fractional`; a trade dated
before the account's `opened_date`; and a batch whose stated source file hash
does not match the file on disk.

### Provenance

A new `import_batch` table records the source filename, its SHA-256, the broker,
the accounts touched, the period covered, and the row counts — with
`transaction.import_batch_id` referencing it. `transaction.source` is set to
`'import'`, which requires giving `TradingService` a source parameter it does not
currently have. Without this, no committed row can be traced to the document it
came from, which `PORT-GIPS-J03` requires and which the owner will want long
after remembering which export produced what.

### Acceptance is reconciliation, not parser tests

An import is accepted when, for each statement period, ending cash and every
ending position quantity match the broker per account, and — for a closed tax
year — realized gains tie to the 1099-B. Parser unit tests establish that the
adapter does what it says; only reconciliation establishes that what it says is
right. `pt reconcile` needs per-account scoping and a cash comparison before the
first real import; today it has neither.

## Consequences

- Importing is a **three-command workflow**, deliberately. The friction is
  proportionate to writing permanently into an append-only tax record.
- The batch format is a published, versioned schema under `schemas/`, so a
  future OFX adapter or a hand-written batch is a first-class input. The format
  is the interface; the adapters are replaceable.
- Every adapter is a pure function from bytes to a batch, testable against a
  redacted fixture with no database in sight.
- This adds a package, `src/portable_core/importers/`, sibling to `providers/`
  and subject to the same layering rule — it may import `domain` and `errors`,
  and nothing in `persistence` or either CLI. `CLAUDE.md`'s directory map gains
  the entry in the commit that creates the package, not in this one.
- Re-running an extract after fixing the activity map produces a clean diff
  against the previous batch. This is the main development loop.
- The owner must review a batch of roughly a thousand rows once. Grouping the
  batch by activity and by instrument makes this an afternoon rather than a week,
  because the review is per *rule*, not per row.

## Alternatives considered

- **Direct file-to-ledger import with `--dry-run`.** Rejected: a dry run shows
  what will happen but gives nowhere to *correct* it. Every fix would be a new
  extract, and any fix not expressible in the adapter would have to be made after
  the fact with reversing entries.
- **Import into a staging table inside the `.port` file.** Rejected: it puts
  un-reviewed data inside the artifact whose integrity is the point, adds a table
  class ADR 0010's partition has no room for, and is harder to diff than a file.
- **Deriving identity from a row hash without an ordinal.** Rejected: two
  identical dividends on one day are not hypothetical in these exports, and the
  collision would silently drop the second one.
- **Inferring the fee class from the activity string at commit time.** Rejected
  by `PORT-GIPS-D01`: the classification is a stored fact decided when the row is
  recorded. It is decided in the activity map, which is reviewed, rather than by
  a rule buried in the importer.
