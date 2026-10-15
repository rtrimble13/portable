# ADR 0021 — Derived state as of a past date

- **Status:** Accepted — implemented for `pt value`, `pt mark`, `pt holdings`, `pt pnl` and `pt lot list`
- **Date:** 2026-10-15
- **Milestone:** v0.2 — a prerequisite of the daily time-weighted return engine, alongside [ADR 0020](0020-historical-price-import.md)
- **Governs:** `CLAUDE.md` invariants 3, 6, 9; `PORT-GIPS-A01`, `A03`, `B02`, `J01`, `J02`
- **Amends:** [ADR 0010](0010-derived-state-and-replay.md) — a second consumer of the one replay path; `rebuild(until=)` is removed

## Context

Derived state is materialized for one moment: the end of the ledger. Five
commands answer for an earlier date — `pt value --date/--from/--to`,
`pt mark --date`, and `pt holdings`, `pt pnl` and `pt lot list` under
`--as-of` — and every one of them read that current state and labelled the
result with the earlier date. A deposit of 100,000, 100 AAPL bought 2024-01-03
and sold 2025-03-03, 10 MSFT bought 2025-03-04:

| `pt value --date 2024-06-30` | Correct | Recorded |
|---|---|---|
| Securities | 15,500.00 (100 AAPL) | 3,000.00 (10 MSFT, not yet bought) |
| Cash | 85,000.00 | 101,000.00 (today's balance) |
| Ending market value | 100,500.00 | 104,000.00 |
| `is_complete` | true | **true**, no warning |

`pt reconcile` already refused `--as-of` for exactly this reason. Nothing else
did.

It went unnoticed because old prices were missing: most past dates came out
incomplete for want of a price, and an incomplete snapshot is never used for a
return. ADR 0020 sets out to load every price since 2022-05-18. Doing that
before this fix would have turned roughly 1,600 days per account of today's
book at historical prices into snapshots marked complete — the substrate the
return engine reads.

A second defect has the same root: **what "the ledger up to D" means once a
reversal is involved.** Three code paths gave three answers:

- **replay** nets a reversal and the entry it reverses out of derived state
  entirely (ADR 0010, *Corrections*);
- **snapshot flows** counted the reversed entry as a flow on its own date and
  classified the reversal row as internal;
- **`pt cash-flows`** counts the reversed entry on its date and the reversal,
  negated, on the reversal's date.

A snapshot took its market value from the first view and its flows from the
second, so a deposit later reversed as a mistake left a contribution in the
flow record that never appeared in value — a phantom flow, and a wrong return
on that day.

## Decision

1. **State as of D is the corrected ledger, truncated at D.** The *effective
   ledger* is the whole ledger minus every reversal and the entry it reverses,
   netted over the **whole** ledger rather than only the rows dated on or
   before D. State as of D is the replay of the effective rows dated on or
   before D, in ledger order.

   A reversal corrects the record; it is not an economic event on its own
   date. ADR 0010 already defines current state this way. The alternative,
   "as booked through D", would hold a mis-keyed trade and the entry that
   corrects it at the same time on every day between them. Integration
   scenario 7 would report 1,100 shares of AAPL on 2024-02-01, when the owner
   held 100. The
   consequence is that correcting an old error **restates** past snapshots.
   That is what the standard asks for: `PORT-GIPS-J01` detects that an issued
   report changed by its content hash, and `PORT-GIPS-J02` makes the
   correction additive at the report level. It is not hidden at the ledger
   level.

2. **One function defines the effective ledger:** `replay.effective_ledger`.
   `rebuild()`, the cash-conservation check, the walk below, and snapshot flows
   all call it. Before this change the netting was written out twice, with a
   comment explaining why the two copies had to agree.

3. **One forward replay inside a savepoint that is always rolled back:**
   `ReplayEngine.walk(dates, visit)`. It clears derived state, then applies
   effective rows in ledger order through the same `apply_transaction` that
   every other path uses (ADR 0010). At the end of each requested date it
   calls `visit`, which reads derived state through the repositories as usual.
   The savepoint is rolled back afterwards, so stored derived state is
   byte-identical before and after. The mechanism is the one ADR 0016 gave
   `pt validate`. A whole `pt value --from --to` range costs one replay, not
   one per day.

4. **Fast path.** If no ledger row is dated after D, current state *is* state
   as of D, and no walk runs. ADR 0016 makes that an invariant and
   `pt validate` checks it.

5. **Snapshot flows come from the same effective rows as snapshot value.**
   Across a range, beginning market value chains in memory. It used to be read
   from the previous day's stored snapshot, which meant a `--dry-run` range
   disagreed with the real run after its first day.

6. **`ReplayEngine.rebuild(until=)` is removed.** It had no caller, and it
   answered the other question: as booked through D.

## Consequences

- Snapshots of past dates that were written before this change are wrong
  wherever a later ledger row exists. Re-run `pt value` over the range.
- **`pt cash-flows` still uses the as-booked view, and that is not decided
  here.** For a reversed external flow, its series and the snapshots now
  differ in timing only: +X on the original's date and −X on the reversal's,
  against nothing at all. The money-weighted solve (`PORT-GIPS-C01`–`C05`)
  is sensitive to timing, so this must be settled before it consumes either
  series. The recommendation is the effective ledger there too, with the
  netted pair still visible in `pt activity`.
- `pt reconcile --as-of` is still refused, but it could now be built on the
  walk. ADR 0020 R10's anchor, the custodian's 2026-08-27 holdings snapshot,
  wants exactly that.
- ADR 0010's partition table said valuation snapshots are "dropped and
  rebuilt". `rebuild()` dropped them and nothing rebuilt them, because they
  depend on prices as well as the ledger. `pt validate` digested them against
  a replay that cannot produce one, so any valued portfolio failed it, and its
  remedy deleted the valuation history. **Resolved by
  [ADR 0024](0024-valuation-snapshots-are-valued-state.md):** a valued class,
  kept by rebuild and checked by re-deriving each snapshot through this walk
  with its recorded prices held fixed.
- The walk costs one rebuild per invocation, inside a write-locked
  transaction. A profile showing that is too slow for the full history comes
  before any optimisation (`docs/roadmap.md`, *Candidate C++ hot paths*).

## Alternatives considered

- **Refuse a past date, as `pt reconcile` does.** Honest, and the right
  stopgap if this were expensive. Rejected because the return engine needs
  this answer for every day since inception; a refusal only moves the problem
  onto `pert`.
- **`rebuild(until=D)` per date in a savepoint.** Correct state, but one
  replay per day rather than one per range. It also nets only reversals dated
  on or before D, which is the as-booked view rejected in decision 1.
- **A materialized, date-indexed positions history.** Faster reads, but a
  second derived truth to keep in step with the first. ADR 0010 rejected
  incremental maintenance for that reason. Revisit only if a profile asks.
