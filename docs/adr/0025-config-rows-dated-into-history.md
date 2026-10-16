# ADR 0025 — A config row dated into history is refused, or restated on request

- **Status:** Accepted — implemented in `services/restatement.py`, `pt account tax-rates set --restate` and `pt policy set --restate`
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
   one from its own date on.

## Consequences

- `pt tax` can no longer report tax at a rate the schedule no longer holds
  without an error having been raised first.
- **Open: a mistyped rate cannot be corrected on its own date.** Because rows
  are not edited and a date takes one row, a schedule entered wrongly for
  2024-01-01 can only be overridden from 2024-01-02. A gain realized on
  2024-01-01 keeps the wrong rate. Supporting that needs a correction
  mechanism for config rows (a superseding row, or an edit that is itself
  recorded). That is a design question for the owner, not decided here.
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
- **Remove the affected snapshots, as a ledger write does.** Rejected for a
  policy change: it moves flags, not values, and re-deriving with the
  recorded prices is exact. A ledger write may change holdings that no
  recorded price covers, which is why it removes instead.
