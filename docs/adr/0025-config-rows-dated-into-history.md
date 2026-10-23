# ADR 0025 — A config row dated into history is refused, or restated on request

- **Status:** Accepted — implemented in `services/restatement.py`, `pt account tax-rates set --restate` and `pt policy set --restate`; decision 5, the correction of a row on its own date, in migration `0009_config_row_corrections` and `--corrects` (2026-10-20)
- **Date:** 2026-10-16
- **Milestone:** v0.2
- **Governs:** `CLAUDE.md` invariants 3, 9; `PORT-GIPS-B03`
- **Amends:** [ADR 0010](0010-derived-state-and-replay.md) — its "Policy rows" paragraph, which said `pt validate` refuses such a row and nothing did

## Context

`tax_rate_schedule` and `return_policy` are effective-dated so that a new row
cannot rewrite what was already reported (ADR 0010). ADR 0010 said that a row
whose `effective_from` precedes existing derived state would be refused, and
the user told to rebuild explicitly. Nothing did that. Reproduced:

- **A tax rate schedule effective before a recorded sale was stored, and the
  sale's estimated tax was not.** A 1,000.00 short-term gain taxed at
  0.37 + 0.05 carried 420.00. A schedule correcting the state rate to 0.0575
  from a date before the sale should make it 427.50. `pt tax` went on
  reporting 420.00 with no warning, until something else rebuilt. Only
  `pt validate` noticed, as a replay mismatch in `realized_gain`. This is a
  wrong number in the report the owner files taxes from.
- **A return policy dated before a stored snapshot changed none of its
  large-flow flags.** After ADR 0024, `pt validate` named the snapshots, but
  the write itself was accepted silently.
- **A second row for the same date failed with an unexpected
  `IntegrityError`**, exit 1, instead of a refusal naming the existing row.

## Decision

1. **A config row that would change a recorded figure is refused** with
   `PT-E-RESTATES-HISTORY`, exit 4. The refusal names the figures and the
   change: which gains and the estimated tax before and after, or which
   snapshots. The transaction rolls back, so nothing is stored.
2. **`--restate` records the row and restates the figures in the same
   transaction.** This is CLAUDE.md invariant 9: a person can legitimately
   decide this (correcting the rate in force at the time), so the decision
   is an explicit flag, never a default. ADR 0016's rule applies too: "run
   `pt rebuild` afterwards" is not a control, so the restatement is not left
   for later.
   - **Tax rates:** replay estimates tax as it relieves lots, so the ledger is
     replayed. Whether anything changed is decided by comparing the
     account's realized gains before and after, by their figures: date,
     instrument, holding period, proceeds, basis, gain, rates and estimated
     tax. Surrogate ids are not compared, because a rebuild reassigns them.
     Neither is `rate_id`, which names a schedule rather than a figure.
   - **Return policy:** a policy is read only to flag large flows in
     snapshots, and snapshots depend on prices replay does not read
     (ADR 0024). Each stored snapshot in the policy's window (from its date to
     the next policy's) is derived again with its recorded prices held fixed.
     The ones that come out different are saved. No valuation history is
     lost, and no price is replaced.
3. **A row that changes nothing needs no flag.** The common cases are a rate
   for a coming year, or a first policy entered before any valuation. They
   are stored as before.
4. **A second row for the same date is refused by name.** Config rows are
   effective-dated and not edited (ADR 0010). A later row replaces an earlier
   one from its own date on. The refusal names `--corrects` for a row that
   was entered wrongly.
5. **A row entered wrongly is corrected on its own date by a superseding
   row** (decided by the owner, 2026-10-20). `--corrects` records a second
   row for the same date that names the one it replaces
   (`tax_rate_schedule.corrects_rate_id`, `return_policy.corrects_policy_id`).
   - **Nothing is edited.** The wrong row is kept, so the history shows what
     was entered, what replaced it and when. Every lookup reads the row that
     nothing corrects, so a corrected row is never read for a rate or a
     threshold again.
   - **A correction is a row like any other.** If it changes a recorded figure
     it is refused under decision 1, and `--restate` is still needed. The
     restatement re-derives with the corrected row, so a realized gain names
     the schedule it was really taxed at.
   - **A correction may be corrected,** so the rows of one date form a chain.
     The schema keeps it one: a partial unique index allows one original per
     date and another lets each row be corrected once. A trigger refuses a
     correction naming a row of a different account or date.
   - **`--corrects` with no row on that date is refused.** Without the flag the
     row is simply the schedule from that date.
   - `pt account show` and `pt policy show` list a corrected row with the id
     of the row that replaced it.

## Consequences

- `pt tax` can no longer report tax at a rate the schedule no longer holds
  without an error having been raised first.
- **A mistyped rate can be corrected on its own date** (decision 5). Before
  it, rows were not edited and a date took one row, so a schedule entered
  wrongly for 2024-01-01 could only be overridden from 2024-01-02, and a gain
  realized on 2024-01-01 kept the wrong rate.
- **Schema 9 rebuilds both tables** to replace their `UNIQUE` constraint with
  the partial indexes (ADR 0019). Every row and id is kept, so
  `realized_gain.rate_id` still names the same schedules.
- **The same rule binds any future config writer that replay reads.** Replay
  falls back to an account's default relief method when a closing trade names
  none. No command can change that default today
  (`AccountRepository.update` allows the column and has no caller). A command
  that does must refuse or restate in the same way, because changing it would
  re-relieve every such lot. No derivation reads benchmark rows.
- A dry run of either command raises the same refusal, because it is the
  real write rolled back (ADR 0026).

## Alternatives considered

- **Restate silently on every write.** Rejected: effective-dating exists so
  that history is not restated by accident, and the owner would learn of a
  changed tax figure only by comparing reports.
- **Refuse, and leave the restatement to `pt rebuild`.** This is what ADR 0010
  described. Rejected: a refusal that cannot be overridden leaves no way to
  correct a rate; one overridden by storing the row and rebuilding later
  leaves the window this ADR closes. It also cannot restate snapshots, which
  a rebuild keeps (ADR 0024).
- **Correct a row by editing it, with the edit itself recorded** (the other
  option for decision 5). Rejected: it makes these tables mutable, and the
  audit trail a separate log. A superseding row keeps both rows in the table
  the lookups already read, and needs no new kind of history.
- **Remove the affected snapshots, as a ledger write does.** Rejected for a
  policy change: it moves flags, not values, and re-deriving with the
  recorded prices is exact. A ledger write may change holdings that no
  recorded price covers, which is why it removes instead.
