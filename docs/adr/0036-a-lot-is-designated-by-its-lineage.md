# ADR 0036 — A lot is designated by its lineage, not by its row number

- **Status:** Accepted (owner, 2026-11-06) — implemented in schema 10 (`lot.lot_key`, `lot_designation`), `services/designations.py`, `pt migrate`, `pt import`, and `pt lot designation list` / `resolve`
- **Date:** 2026-11-06
- **Milestone:** v0.2 — blocks wash-sale detection (issue 27), which splits lots
- **Governs:** `CLAUDE.md` invariants 2, 3, 9; `docs/tax-methodology.md` "Relief methods"
- **Extends:** [ADR 0023](0023-position-regroup-is-a-ledger-row.md) — its rule that a ledger row may not name a derived key, applied to lots

## Context

A specific-identification sale records its designation on the ledger row as
`lot_selection = "2:10;5:3"`: lot **ids** and quantities. A lot id is a
surrogate key on a derived table. Every rebuild deletes the lots and replays,
and SQLite numbers them again in creation order. ADR 0023 found the same
fault in `transaction.position_id` and fixed it for positions: "a ledger row
naming a derived key names nothing stable". It was never fixed for lots.

Reproduced against the current code, both times with no error raised:

- **A back-dated buy.** A sale designating the 2024-01-20 lot (basis 2,000.00,
  a loss of 500.00) consumed the 2024-01-10 lot after one buy dated
  2024-01-05 was appended. The loss became a gain of 500.00.
- **A reversal.** With three lots, the same sale consumed the 2024-01-25 lot
  after an unrelated earlier buy was reversed, because the reversed buy drops
  out of replay. The loss became 1,500.00.

`pt validate` cannot see either: it compares stored state with a replay, and
both read the designation the same wrong way. Every back-dated import, every
reversal, and every `--incremental` update that sorts before an existing row
rebuilds (ADR 0016), so any of them can re-point any earlier spec-ID sale.
Broker import writes such designations for every sale the custodian's lot
report covers (`ImportBatchService._designate`).

Wash-sale detection must split a replacement lot into the shares a loss was
deferred into and the rest. Built on row numbers, every split would renumber
every later lot.

## Decision

1. **Every lot has a lineage key**, `lot.lot_key`, derived in replay and
   unique in the file. A lot an opening transaction created is keyed by that
   transaction: `1234`. A lot a corporate action derived from a parent lot is
   keyed by the action's transaction and the parent's key: `1500.1234` for
   the NEWCO shares a spinoff gave a parent lot. A split adjusts a lot in
   place and keeps its key. No numbering enters a key, so a row inserted
   earlier, or a reversal, cannot change one.
2. **New designations name keys.** The ledger stores `@1234:10;@1500.1234:3`.
   `pt sell --lots` still accepts the lot ids `pt lot list` shows, and
   translates them to keys when the trade is planned, against the state the
   user is looking at. `pt lot list` shows the key beside the id. Broker
   import writes keys.
3. **The ledger is not edited.** A row written before this change keeps its
   `lot_selection` text. What it means is recorded once, by key, in a new
   table, `lot_designation (txn_id, as_written, as_read, lot_selection,
   status, note)`. It is reference state: written by `pt migrate`, by
   `pt import`, and by the owner's decision below, and never by a rebuild.
   Triggers let a pending row be decided once and refuse every other change
   and every delete.
4. **An old designation means what it meant when it was written.** When the
   row was appended, stored state equalled a replay of the rows appended
   before it (ADR 0016), so the ids it names are the lots of that replay, in
   creation order. `pt migrate` replays each such prefix, reads the keys, and
   compares them with what the row reads as today:
   - **They agree** (no later row renumbered anything before it): recorded,
     `status = 'agreed'`. Only a row some later row disturbed needs its own
     replay; the rest are read from one replay of the whole ledger.
   - **They disagree:** the sale's meaning has already drifted. Nothing is
     chosen. Both readings are reported, with the lots, their bases and the
     gain each gives, and the sale is refused by replay until the owner
     decides with `pt lot designation resolve TXN --as-written` or
     `--as-read`. The decision is recorded with its reason.
   - **It cannot be established.** A batch import applies its rows
     incrementally and rebuilds once at the end. When its rows sorted
     before rows already in the file, its designations were written against
     a numbering no prefix replay reproduces. Such rows are reported as not
     establishable and decided the same way, with the custodian's lot report
     as the evidence. `pt migrate` does not guess them.
5. **Schema 10.** `lot.lot_key TEXT NOT NULL UNIQUE`: `lot` is derived, so it
   is recreated empty under ADR 0019 and `pt migrate` rebuilds it. The new
   `lot_designation` table is created and filled as above. Valuation
   snapshots are kept (ADR 0024). `pt validate` reports any old designation
   with no recorded meaning or still pending. A key that names no lot open
   for the sale is refused by replay, which `pt validate` already reports as
   unreplayable.
6. **The `pt ca` commands derive what replay derives.** `pt ca spinoff` and
   `pt ca convert` derive lots directly rather than through replay. They now
   append the action's row first and hand its id to the engine, so the lots
   they write carry the same `open_txn_id` and key a replay gives them. The
   spinoff command's spun lots used to carry the parent's `open_txn_id`
   instead, a difference the derived digest could not see. Its spun position
   had a note and no `opened_txn_id`, which the digest did see: `pt validate`
   failed after every `pt ca spinoff`. It now has what replay gives it.

## Consequences

- No sale's lot can be changed by a later row again. A drift that already
  happened in a file is found by `pt migrate` and put in front of the owner,
  with the restated gain, instead of persisting silently.
- Restating a sale changes a realized gain that may already have been
  reported or filed. `pt migrate` lists every such sale before anything is
  decided. Deciding it is the owner's call, sale by sale.
- `pt migrate` runs Python after the SQL: replays, which the `schema/` layer
  may not import. The step lives in `services/` and is run by the `pt migrate`
  and `pt import` commands. A file migrated by `M.migrate` alone has old
  designations with no recorded meaning, and `pt validate` says so.
- The one-time cost is one replay per old designation that a later row
  disturbed. Undisturbed rows are read from a single replay.
- Wash-sale splits (issue 27) get keys by the same rule, so a split never
  renumbers anything.

## Alternatives considered

- **Rewrite `lot_selection` in place.** One-time and simple, but it edits the
  append-only ledger, and its triggers exist to stop exactly that
  (invariant 2). Rejected.
- **Freeze every old row to what it reads as today.** No replays, and it
  matches what current reports show. But a sale that has already drifted
  would stay wrong, silently, until noticed by hand. Rejected, though it is
  the reading the owner can choose sale by sale under decision 4.
- **Keep lot ids stable across rebuilds.** No single numbering satisfies
  every old row: the same lot had different ids when different sales were
  written. Rejected.
- **Record the meaning as a ledger row**, as ADR 0023 did for regrouping.
  A new transaction type means rebuilding the ledger table (ADR 0019) for a
  record that is not an event. Rejected for a reference table with the same
  durability.
