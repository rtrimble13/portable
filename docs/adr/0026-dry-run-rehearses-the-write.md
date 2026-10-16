# ADR 0026 — A dry run is the real write, rolled back

- **Status:** Accepted — implemented in `CommandContext.write()` and every mutating `pt` command
- **Date:** 2026-10-16
- **Milestone:** v0.2
- **Governs:** `CLAUDE.md` "CLI shape" (`--dry-run` on every mutating command); `docs/architecture.md` §2
- **Supersedes:** the `--dry-run` paragraph of `docs/architecture.md` §2, which cut between the service and persistence

## Context

`docs/architecture.md` said a dry run "cuts between the service and
persistence", and that this is why "the effect of a dry run is exactly the
effect of the real run". It stopped being true as soon as anything happened at
write time:

- **A rebuild.** A back-dated entry rebuilds derived state when it is appended
  (ADR 0016). A dry run never appended, so it could not say a rebuild would
  happen.
- **Snapshot removal.** A ledger write removes the valuation snapshots it
  makes stale (ADR 0024). A dry run removed none and reported none.
- **Refusals raised while writing.** These include a tax rate or policy that
  would restate history (ADR 0025), a duplicate external reference (refused
  in `TransactionRepository.append`), and a regroup that replay cannot apply
  (ADR 0023). A dry run accepted what the real run would refuse.

`pt import batch` already did it the other way, and said why: a sale's lot
relief has to see the purchase earlier in the same batch, so the only dry run
that answers the question is the real commit, rolled back (ADR 0012).

The owner chose that for every command.

## Decision

1. **Every mutating command writes through `CommandContext.write()`.**
   Normally it is one all-or-nothing transaction, exactly as before. Under
   `--dry-run` it is a savepoint inside a single outer transaction that is
   never committed. It is rolled back when the command closes its connection,
   whether the command succeeded or not.
2. **A command runs the same code with and without `--dry-run`.** The early
   returns are gone. The result is the real result, marked
   `"dry_run": true` with the `DRY RUN` warning, and carries every warning
   the real run would: rebuilds, snapshots it *would* remove, restatements.
   A refusal is raised exactly as the real run would raise it, with the same
   exit code.
3. **Writes within one dry run see each other.** Several writes in one command
   (`pt ca sync` applying actions in turn, `pt price load` writing a page at a
   time) accumulate in the outer transaction, so a later step sees an earlier
   one, as it would for real.
4. **Commands that write files keep their own dry run.** `pt init`,
   `pt export`, `pt backup` and `pt migrate` write or replace files rather
   than rows, and a rolled-back transaction does not undo a file. They report
   what they would do without doing it, as before.

## Consequences

- A dry run takes the same write lock as the real run (`BEGIN IMMEDIATE`), for
  as long as the command runs. Another process writing the same file waits,
  as it would for a real write.
- Ids in a dry run's output (`txn_id`, `rate_id`) are the ids the rows would
  have had, and are not reserved.
- A dry run of a command that fetches from a provider still fetches. It
  already did: only the write was skipped.
- A test runs one of each kind of mutating command with `--dry-run` and
  requires every row of every table to be unchanged afterwards.

## Alternatives considered

- **Preview only the snapshot removal**, with a query beside each command's
  current dry-run path. Rejected by the owner: rebuilds and refusals would
  still be missed, and each preview would be a second implementation of what
  the write does.
- **A rolled-back transaction per write.** Simpler, but a later write in the
  same command would not see an earlier one, so a multi-step dry run would
  answer a different question from the real run.
