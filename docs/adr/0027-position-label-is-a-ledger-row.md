# ADR 0027 — A position label is a ledger row

- **Status:** Accepted — implemented in schema 6 (`position_label`) and `pt position label`
- **Date:** 2026-10-16
- **Milestone:** v0.2
- **Governs:** `CLAUDE.md` invariants 2, 3
- **Amends:** [ADR 0010](0010-derived-state-and-replay.md) — its consequence that "`label` and `note` are ledger-carried, set by the transaction that opened the position"

## Context

`position.label` has existed since the first schema, and nothing writes it.
ADR 0010 said labels are ledger-carried and set by the opening transaction,
but no command took a label and no ledger column held one. A label written
straight into `position` would be derived state edited outside replay. It
would go the way groupings went before ADR 0023: lost on the next back-dated
import, reversal or `pt rebuild`, with `pt validate` failing in between.

The owner chose a ledger row over a label fixed at the opening trade. A
position's name changes over its life (a core holding becomes an income
sleeve), and correcting a label set at opening would mean reversing and
re-entering the trade.

## Decision

1. **A label is a row in the ledger:** `txn_type = 'position_label'`
   (schema 6, migration 0006). It sets the label of the position opened by
   `related_txn_id` to `label`, or clears it when `label` is NULL. Replay
   applies it in ledger order, so the live path and a rebuild agree.
2. **It names the position by the transaction that opened it**, as a regroup
   names its target (ADR 0023), because `position_id` is a surrogate key a
   rebuild renumbers. `pt position label` takes the id people see in
   `pt position list` and translates it before writing.
3. **It changes nothing else.** `CHECK`s require a `related_txn_id`, no
   instrument, no quantity and zero cash, and only a `position_label` row may
   carry a `label`. It is not a cash flow at any level. It removes no
   valuation snapshot, because a snapshot records neither positions nor their
   names (ADR 0024).
4. **It is checked against the state on its own date** (ADR 0021): a label
   dated before its position was opened is refused at entry, because replay
   could never apply it and `pt validate` would report it for ever.
5. **The latest label in ledger order wins.** Relabelling is another row;
   `pt trade reverse` undoes one, and the label before it returns. An empty
   label is refused; `--clear` removes one.

## Consequences

- Migration 0006 rebuilds `"transaction"` (ADR 0019) and copies all 34
  columns, named one by one. It is tested on a v5 file in which every column
  carries a value somewhere. That check, applied here first, found that the
  0005 test's fixture left twelve columns empty.
- `pt position list` shows each position's label.
- The opening transaction's `note` still becomes the position's note; there
  is no command to change a note later. If one is wanted, it is a second
  column on the same row type.

## Alternatives considered

- **`--label` on opening trades only.** Simpler, and no new row type, but a
  label could then never change without reversing and re-entering the trade.
  Rejected by the owner.
- **An effective-dated label table outside the ledger.** An additive
  migration, but a second history with its own correction path, which
  `pt trade reverse` could not undo. Rejected for the reason ADR 0023 gives.
