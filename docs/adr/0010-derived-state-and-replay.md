# ADR 0010 — Derived state, the replay contract, and what `pt rebuild` guarantees

- **Status:** Accepted
- **Date:** 2026-08-27
- **Milestone:** v0.1
- **Governs:** `CLAUDE.md` invariants 2, 3, 6; `PORT-GIPS-J03`, `PORT-GIPS-J06`

## Context

The ledger is the source of truth; positions, legs, lots, dispositions, cash
balances, realized gains, and valuation snapshots are materialized for speed but
must be exactly reproducible by replaying the ledger (`CLAUDE.md` invariant 3).
`pt rebuild` does this and a test asserts it for every fixture.

"Exactly reproducible" needs a precise definition, because two of the inputs to
derived state are *not* in the ledger: **prices** and **policy rows**.

## Decision

### The partition

| Class | Tables | Rebuild behaviour |
|---|---|---|
| **Ledger** (append-only, immutable) | `transaction` | Never touched by rebuild. `UPDATE`/`DELETE` rejected by trigger. |
| **Reference** (externally sourced, immutable once written) | `instrument`, `instrument_option`, `instrument_bond`, `instrument_symbol_history`, `price`, `corporate_action` | Never touched by rebuild. A price is a fact about the world, not a derivation. |
| **Config** (effective-dated, immutable once effective) | `account`, `tax_rate_schedule`, `benchmark`, `benchmark_component`, `portfolio_benchmark`, `return_policy`, `portfolio_event`, `settings`, `meta` | Never touched by rebuild. |
| **Derived** | `position`, `position_leg`, `lot`, `lot_basis_adjustment`, `lot_disposition`, `realized_gain`, `cash_balance`, `valuation_snapshot`, `valuation_snapshot_price`, `snapshot_flow` | **Dropped and rebuilt** from ledger + reference + config. |

`pt rebuild` deletes every derived row and replays. If a table is not in the
derived row of that table, `pt rebuild` must not write to it. If you add derived
state, you add it to the replay path **and** to
`tests/property/test_replay_reproduces_state.py` in the same commit — this is
`CLAUDE.md` invariant 3 and it is not negotiable.

### The replay contract

**Replay order is the ledger's total order**, defined as
`(trade_date, seq, txn_id)` where `seq` is a monotonic integer assigned at
insert. Not `created_at` — a wall clock is not a total order across machines, and
`CLAUDE.md` invariant 6 forbids wall-clock dependence. Not `trade_date` alone —
two trades on one day in the wrong order consume the wrong lots under FIFO.

Given that order:

1. **Replay is deterministic.** Same ledger + same reference + same config →
   byte-identical derived state. This is what makes `PORT-GIPS-J01`'s
   report content hash meaningful, which is why `PORT-GIPS-J06` records
   determinism as evidence rather than convenience.
2. **Replay is idempotent.** `rebuild` twice equals `rebuild` once.
3. **Replay is order-stable.** Inserting transactions in a different wall-clock
   order but with the same `(trade_date, seq)` yields identical derived state.
   `seq` is assigned per `trade_date`, from the ledger's existing maximum, so a
   back-dated entry lands after same-day entries already recorded — which is the
   honest answer, since the ledger records when we learned things.

Properties 1–3 are hypothesis tests over generated transaction sequences.

### The two inputs that are not in the ledger

- **Prices.** A snapshot rebuilt after a better price arrives will legitimately
  differ. That is not a determinism violation — it is `PORT-GIPS-A09` working:
  an estimate was superseded, the snapshot was rebuilt, and the change must be
  *reported*. Each snapshot therefore records the exact set of prices it consumed
  in `valuation_snapshot_price` (`price_id`, source, as-of, valuation level,
  `is_estimate`). Replay determinism is asserted **holding the price set fixed**;
  the price-changed path is asserted separately by
  `test_stale_price_then_correction_rebuilds`.
- **Policy rows.** `return_policy` and `tax_rate_schedule` are effective-dated
  and immutable once effective, precisely so that a policy change cannot
  retroactively restate history. Adding a row effective from a future date
  changes no past derived value; `pt validate` refuses a row whose
  `effective_from` precedes the latest already-derived state and tells the user
  to rebuild explicitly.

### Corrections

A mistake is corrected with a **reversal transaction** pointing at the
transaction it reverses (`reverses_txn_id`), plus a new correct entry. History
shows all three. Derived state after replay reflects only the net effect. This
is `CLAUDE.md` invariant 2, and `PORT-GIPS-J02` extends the same discipline from
transactions to reports.

## Consequences

- `pt rebuild` is safe to run at any time and is the standard response to any
  suspected derived-state bug. After a bug fix, rebuild recovers correct state
  from a ledger that was never wrong.
- Derived tables carry no user-authored data. Anything a human types goes in the
  ledger or in config, or it is lost on the next rebuild. `pt position --label`
  therefore writes to `position` — so **`label` and `note` are ledger-carried**,
  set by the transaction that opened the position, and re-derived on replay.
- Materialized derived state can be dropped entirely to shrink a `.port` file;
  `pt rebuild` restores it.

## Alternatives considered

- **Compute everything on read, materialize nothing** — perfectly consistent by
  construction, and unusably slow once the ledger is long: `pt holdings` would
  replay from inception on every invocation. Rejected on ergonomics.
- **Materialize with incremental maintenance and no full rebuild** — faster, and
  the source of the entire class of bugs where materialized state and truth
  diverge with no way to tell. Rejected: the full rebuild *is* the audit.
