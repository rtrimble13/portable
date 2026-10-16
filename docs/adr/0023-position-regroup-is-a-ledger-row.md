# ADR 0023 — A change of grouping is a ledger row

- **Status:** Accepted — implemented in schema 5 (`position_regroup`), `pt position group` / `ungroup`, and `--position` on a trade
- **Date:** 2026-10-15
- **Milestone:** v0.2
- **Governs:** `CLAUDE.md` invariants 2, 3; ADR 0009 consequence 4
- **Amends:** [ADR 0009](0009-position-spans-instruments.md) — regrouping moves one column *by replaying a ledger row*, not by editing it

## Context

ADR 0009 makes a position the unit of trader intent. A long stock holding
becomes a covered call when a call is written against it, and
`pt position group` says so. It said so by updating `position_leg.position_id`
directly. That is derived state (ADR 0010), and the next rebuild discards it.
Every back-dated import, reversal and `--incremental` update rebuilds
(ADR 0016). Reproduced:

- `pt position group` moved a leg, and `pt validate` failed at once, because
  stored state no longer matched a replay.
- `pt rebuild` split the group back into single positions.
- `pt buy --position N` wrote `N` into `transaction.position_id`, and replay
  never read it. The trade opened a position of its own.
- `--new-position` was accepted and never recorded.

There is a second, quieter problem: `N` is a `position_id`, a surrogate key that
a rebuild replaying in a different order renumbers. Even if replay had honoured
it, a ledger row naming a derived key names nothing stable.

## Decision

1. **A regroup is a row in the ledger:** `txn_type = 'position_regroup'`
   (schema 5, migration 0005). It moves *this account's leg in
   `instrument_id`* into the position opened by `related_txn_id`. When that is
   NULL, it moves the leg into a new position, opened by the regroup row itself.
   `strategy_type` restates the target's strategy; NULL means "name it from
   its legs". Replay applies it in ledger order, so the live path and a rebuild
   agree, and a grouping is exactly as durable as a trade.
2. **Positions are named by the transaction that opened them**
   (`position.opened_txn_id`), and legs by account and instrument. Neither
   `position_id` nor `leg_id` reaches the ledger. The commands still take the
   ids people see in `pt position list` and translate them before writing.
3. **It moves intent and nothing else.** A `CHECK` requires an instrument, no
   quantity and zero cash. The lots' denormalised `position_id` moves with the
   leg. No quantity, basis or holding period changes, and the tax engine never
   reads positions (ADR 0009 consequence 3).
4. **It is checked against the state as of its own date before it is written**
   (ADR 0021). A ledger row cannot be edited, so one that replay cannot apply
   would be reported by `pt validate` forever. Moving a leg not yet held, or
   into a position not yet open, is refused at entry.
5. **A position emptied by a move closes that day.** One that keeps legs is
   renamed from them. Strategy inference names two legs that share a role
   `custom`, not `single`: two stocks grouped by hand are not one holding.
6. **`--position N` on an opening trade is the trade plus a regroup row**, in
   one database transaction. It is refused when the account already holds the
   instrument in another position, because the trade would add to that
   holding; `pt position group` moves it. **`--new-position` is honoured when
   the trade opens a new position anyway, and refused otherwise.** One
   account's holding of one instrument is one leg (ADR 0009), and a second
   position in it is not modelled. Trades no longer write
   `transaction.position_id`. Rows that already carry one keep it, as recorded
   and inert, exactly as before.
7. **An older file is refused until it is migrated** (`PT-E-SCHEMA-TOO-OLD`,
   which was defined and never raised). Read unmigrated, a v4 file failed on
   the first new column with "No item with that key". `pt migrate` takes a
   backup first and is the one command that opens an older file.

## Consequences

- `pt position group` and `pt position ungroup` write ledger rows, and
  `pt trade reverse` undoes one like any other entry.
- `pt validate` passes after a grouping, and a rebuild reproduces it. A
  property test enters purchases, groupings and ungroupings through the live
  path and requires a rebuild to reproduce the stored state exactly.
- Migration 0005 rebuilds `"transaction"` (ADR 0019) and copies all 33
  columns, named one by one. It is tested on a v4 file carrying in-kind
  provenance. This said every column carried a value; twelve did not, which
  the stricter test written for migration 0006 found (ADR 0027).
- Not addressed: holding one instrument in two positions in one account.
  Position labels, also open here, became ledger rows the same way
  (ADR 0027).

## Alternatives considered

- **An effective-dated grouping table outside the ledger.** An additive
  migration, with no ledger rebuild. Rejected: the history of intent would live
  in a second place with its own correction path, and `pt trade reverse` could
  not undo it. `symbol_change` is the precedent for a ledger row that changes
  no quantity and no cash.
- **Honour `transaction.position_id`.** It names a surrogate key that a
  rebuild renumbers, so it cannot be honoured reliably.
- **Refuse grouping until a design is chosen.** Honest, and the fallback had
  this not been decided. Rejected because the owner trades covered calls, and
  ADR 0009 exists for them.
