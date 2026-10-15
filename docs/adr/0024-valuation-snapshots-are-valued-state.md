# ADR 0024 — Valuation snapshots are valued state: kept, removed when stale, checked

- **Status:** Accepted — implemented in `services/snapshots.py`, `TransactionRepository.append`, and `pt validate`
- **Date:** 2026-10-15
- **Milestone:** v0.2
- **Governs:** `CLAUDE.md` invariants 3, 9; `PORT-GIPS-A09`, `PORT-GIPS-B03`, `PORT-GIPS-J03`
- **Amends:** [ADR 0010](0010-derived-state-and-replay.md) — the partition gains a fourth class; [ADR 0016](0016-out-of-order-appends-and-validation.md) — a back-dated append's rebuild no longer deletes valuation history

## Context

ADR 0010 put `valuation_snapshot`, `valuation_snapshot_price` and
`snapshot_flow` in the derived class: "dropped and rebuilt" from ledger,
reference and config. Only the first half was true. A snapshot is a function of
the ledger **and the prices it consumed**, and replay reads no prices, so
`rebuild()` dropped snapshots and nothing rebuilt them (ADR 0021,
Consequences). Three failures followed, all reproduced:

- **Every valued portfolio failed `pt validate`.** Validate digested
  `valuation_snapshot` against a replay that produces none:
  `PT-E-REPLAY-MISMATCH`, exit 4.
- **The remedy destroyed what it was meant to repair.** Validate said to run
  `pt rebuild`, which deleted every snapshot of every account. So did every
  back-dated append, import and reversal, because ADR 0016 makes each of them
  rebuild.
- **An append that did not rebuild left snapshots wrong.** A deposit dated
  2024-01-03 into a ledger whose last row was 2024-01-02 was applied
  incrementally. The snapshot stored for 2024-01-05 kept cash of 1,000.00
  against a true 1,500.00, and nothing reported it.

Building the check exposed a fourth failure, which predates this ADR.
`save_snapshot` read the snapshot's id from `lastrowid` after an upsert. When
the upsert updates an existing row, SQLite leaves the last-insert rowid at the
previous insert on the connection, usually another snapshot's price or flow
row. So valuing a day a second time wrote its price set and flows under
another snapshot's id. That either overwrote the other snapshot's record or
failed with a foreign-key error. `PriceRepository.add` had the same defect and
reported another row's id when it replaced a price.

## Decision

### 1. A fourth class: valued

| Class | Tables | Rebuild behaviour |
|---|---|---|
| **Valued** | `valuation_snapshot`, `valuation_snapshot_price`, `snapshot_flow` | **Kept** by rebuild. Removed by the ledger writes that make them stale. Checked by `pt validate`. Recreated only by `pt value`. |

A valued row is derived from the ledger as it stood at the end of its date
**and** the prices it recorded. It is reproducible, as ADR 0010 always said,
**holding its recorded price set fixed**. That is a property that can be
checked, but not a reason to delete the row.

### 2. Rebuild keeps them

`clear_derived()` no longer touches the three tables, and they leave the
replay digest. A rebuild reproduces the ledger-derived state it started from,
so it cannot make a snapshot stale. If a rebuild does change that state (after
a bug fix in replay), `pt validate` names the snapshots that change affects.

### 3. Every ledger write removes the snapshots it makes stale

This is done in `TransactionRepository.append`, which every writer passes
through, so no writer can skip it:

- **Which accounts:** the row's account, and its counter account when it has
  one.
- **From which date:** the row's trade date. For a reversal, the earlier of
  its date and the reversed entry's date, and the reversed entry's accounts
  too. A reversal nets its original out of history from the original's date
  (ADR 0021).
- **What is removed:** every snapshot of those accounts dated on or after that
  date, with its price set and flows. Snapshots of other accounts, and earlier
  snapshots of the same account, are kept.
- **What is exempt:** a `position_regroup` row and its reversal. They move a
  leg between positions, change no quantity and no cash (ADR 0023), and a
  snapshot records neither positions nor legs.

**So back-dated appends still remove snapshots, and now only the ones they
change.** That covers every append dated on or before a stored snapshot,
whether or not it rebuilds; the incremental case above was the one that
leaked. Removal is not silent: every command that removes a snapshot says
which ones, for which account, and gives the `pt value` command that restores
them.

The other option was to keep a stale snapshot and let `pt validate` flag it.
That was rejected. Until someone runs validate, a stale snapshot is a
plausible wrong number on the path into `pert`. A missing snapshot is a
refusal there.

### 4. `pt validate` checks every stored snapshot two ways

`snapshots.verify`:

1. **Re-derive it** from the ledger as it now stands, at the end of its date
   (the ADR 0021 walk), using exactly the prices it recorded. Its beginning
   value is the stored ending value of the day before when one exists, and the
   value it recorded otherwise. Every field, every recorded price and every
   flow must come out identical. A difference names the account, the date and
   the fields.
2. **Check every recorded price against its price row.** The price, the
   estimate flag and the hierarchy level must all still agree. `as_of` is not
   compared, because reloading the same price restamps it without changing any
   figure. A disagreement means a final price replaced the one the snapshot
   used. That is the case `PORT-GIPS-A09` requires to be adjusted, and validate
   names the snapshot and both prices.

`pt value` and `pt validate` build through the same `snapshots.build`, and
`save_snapshot` and the check read and write through the same column lists, so
the check cannot drift from what it checks. Re-derivation accepts any
staleness that was recorded. The tolerance was applied when the snapshot was
built, and re-applying today's setting would report a configuration change as
a stale snapshot.

When every problem is a valuation problem, validate exits 4 with the new code
`PT-E-SNAPSHOT-STALE`, and its remedy is `pt value`, not `pt rebuild`. A
script can tell the two apart.

### 5. The id comes from the key, not from `lastrowid`

`save_snapshot` and `PriceRepository.add` look up the id by the natural key
they just upserted on.

## Consequences

- A valued portfolio validates, and `pt rebuild` keeps its valuation history.
- A property test generates valuations, deposits, withdrawals, purchases,
  transfers and reversals in any order and at any date. After every step it
  requires `verify` to find nothing and every stored snapshot to agree with a
  fresh valuation of its own date. Without the append-time removal it fails
  within a few examples.
- **`PORT-GIPS-A09`, the snapshot half:** a final price replacing the one a
  snapshot used is now detected and named, and `pt value` over that date
  adjusts the snapshot. Valuing a single day re-chains the next day's
  beginning value, so validate then names that next day, and valuing a range
  that covers both clears it. The return half of
  `test_stale_price_then_correction_rebuilds` (the return changed, and the
  change is reported) waits on `pert`.
- **A return policy dated before a stored snapshot is now caught for
  snapshots.** ADR 0010 says such a row is refused, and nothing refuses it.
  `pt validate` now names every snapshot whose large-flow flags the row
  restates. Rows dated before lots and gains, and tax-rate rows, are still
  unchecked.
- **Most dry runs do not report the snapshots they would remove.** Under
  `--dry-run`, most writers return before appending, so the removal never
  runs. The back-dated rebuild notice has the same gap. `pt import batch` is
  the exception: its dry run is the real commit, rolled back, so it reports
  what it *would* remove. That covers the path most likely to be back-dated.
  Closing the gap elsewhere would need a rolled-back append in every writer.
- `pt export` still leaves snapshots out. They are reproducible from what it
  carries, prices included, by `pt value` (ADR 0010).
- Snapshots stored before this change were never checked. Run `pt validate`
  once. A file valued twice over the same days may hold price sets filed
  under the wrong snapshot (§5), and validate names each one.

## Alternatives considered

- **Leave snapshots in the derived class and rebuild them during replay.**
  Rejected: replay would then need prices, a staleness tolerance and a
  provider policy, and a ledger change would silently revalue history at
  whatever prices exist today. Recorded price sets exist so that this does
  not happen (`PORT-GIPS-J03`).
- **Compare stored snapshots against fresh valuations at today's prices.**
  Rejected: a newly loaded price that is better, or merely different, would
  read as a broken invariant. A newer price is a new input, not evidence that
  the stored record is false. Check 2 above catches the case that does make
  it false.
- **Mark stale snapshots instead of removing them.** Rejected for the reason
  in §3. It would also add a flag that every consumer must remember to read.
