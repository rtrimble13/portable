# ADR 0016 — Out-of-order appends force a rebuild, and `pt validate` compares stored state against replay

- **Status:** Accepted — implemented
- **Date:** 2026-09-08
- **Milestone:** v0.2
- **Governs:** `CLAUDE.md` invariants 3, 6; ADR 0010; `PORT-GIPS-J06`
- **Amends:** ADR 0010 — adds an operational obligation; the replay contract itself is unchanged

## Context

ADR 0010 defines the ledger's total order as `(trade_date, seq, txn_id)` and
requires that derived state be exactly reproducible by replaying it. Two code
paths derive state: `ReplayEngine.rebuild()`, which replays the whole ledger in
that order, and `ReplayEngine.apply_transaction()`, which a live command calls
once for the row it just appended. Sharing `apply_transaction` between them is
what ADR 0010 relies on to keep the two paths honest.

It is not sufficient. `apply_transaction` applies a row against derived state
**as it currently stands**, which equals replay only when the appended row sorts
*last*. A row that sorts earlier — any back-dated entry — is applied after
transactions that the ledger's own order says come after it.

Reproduced against a scratch portfolio:

```
buy  100 AAPL @ 100 on 2024-01-10
sell  50 AAPL @ 150 on 2024-06-03     (FIFO)
buy   50 AAPL @  80 on 2024-01-05     entered last, dated earliest

live derived state:   realized 2,500.00   estimated tax   925.00
after `pt rebuild`:   realized 3,500.00   estimated tax 1,295.00
```

Replay's answer is the correct one: `seq` is assigned per trade date, so the
back-dated buy sorts ahead of the sale and FIFO must consume it. The live path
consumed the only lot that existed when it ran.

`pt validate` reported **zero problems** on that portfolio. Reading it, the
reason is that it calls `rebuild()` twice and compares the two digests — but
`rebuild()` drops derived state before re-deriving it, so the stored state is
destroyed before anything can be compared against it. The command measures
idempotence, which is real but is not the property at risk, and it cannot
detect the divergence invariant 3 exists to make detectable. It also mutates the
portfolio as a side effect of a command whose entire purpose is to inspect one.

This is not a corner case for the work in progress. Importing history is
out-of-order by construction (ADR 0012), and every late-arriving correction is
out-of-order too.

## Decision

### 1. An append that does not sort last forces a rebuild

Every path that appends to the ledger compares the appended row's
`(trade_date, seq)` against the ledger's maximum. When the row does not sort
last, the caller runs a full `ReplayEngine.rebuild()` **inside the same database
transaction as the append**, so no commit ever leaves derived state
disagreeing with the ledger.

This is not optional and not a flag. A back-dated entry that leaves the book in
a state `pt rebuild` would change is exactly the silently-wrong-number failure
the repository is organised against, and "remember to run `pt rebuild`" is not a
control.

The command reports that it happened — `rebuilt: true` in the result payload,
and a line in human output — because a back-dated entry re-deriving the whole
book is something the owner should see, particularly when it changes a realized
gain already reported.

### 2. `pt validate` compares stored state against replay, and mutates nothing

The order becomes:

1. digest the **stored** derived state;
2. open a transaction, `rebuild()`, digest again;
3. **roll back**, so the file is byte-identical to before the command ran;
4. compare. A difference is `PT-E-REPLAY-MISMATCH`, exit 4, naming the tables
   that differ.

Idempotence is still worth checking and is kept as a second, separate check
inside the same rolled-back transaction. It is a different property with a
different failure mode and conflating the two is what produced a check that
tested neither.

`pt validate` becomes genuinely read-only. A command that repairs what it was
asked to inspect gives a different answer the second time it is run, which hides
from the next person exactly what it just found.

### 3. The digest must cover the relationships, not only the values

`Repositories.derived_rows` excludes every column whose name ends in `_id`. The
intent was to drop surrogate keys, which a rebuild legitimately reassigns, but
it also drops `account_id`, `instrument_id`, and `leg_id` — so the hash does not
directly cover *which* account or instrument a lot belongs to. Those columns
appear in the `ORDER BY`, which catches most rearrangements, but "most" is not
the standard a digest is held to.

Foreign keys are therefore **resolved to their natural keys** — account name,
instrument symbol — and hashed, rather than excluded. Surrogate primary keys stay
excluded, for the reason `DERIVED_DIGEST_TABLES` already gives.

## Consequences

- Each of the three changes lands with a test that fails without it: the
  back-dating sequence above asserted against `pt rebuild`; a portfolio whose
  derived state is corrupted out from under it asserted to fail `pt validate`;
  and a lot moved to a different instrument asserted to change the digest.
- A back-dated entry costs a full replay. At the scale `portable` is built for —
  the owner's real portfolio is on the order of a thousand ledger rows — this is
  imperceptible, and correctness at a thousand rows is not a performance
  question. Should a rebuild ever become slow enough to matter, the answer is the
  one ADR 0010 already gives: profile first, and the full rebuild *is* the audit.
- `pt import batch` (ADR 0012) rebuilds unconditionally, so this rule costs it
  nothing; it exists so that ad-hoc back-dated entry through `pt buy --date` is
  safe by the same guarantee.
- ADR 0010's contract is unchanged. What changes is that the code now upholds the
  part of it that says stored state equals replayed state, and that `pt validate`
  can tell when it does not.
- `PORT-GIPS-J06` records determinism as evidence. Evidence produced by a check
  that could not fail is not evidence, so this is a correction to that record as
  much as to the code.

## Alternatives considered

- **Refuse back-dated appends outright.** Rejected: correct, and useless. The
  portfolio being built is entirely historical, and the ledger's whole design
  anticipates entries arriving after the fact — ADR 0010 says so explicitly.
- **Make `apply_transaction` insert into the middle of derived state.** Rejected:
  it is a second implementation of replay, which is the thing ADR 0010 forbids;
  it would have to unwind and redo every lot relief after the insertion point,
  which is a rebuild wearing a disguise.
- **Warn rather than rebuild.** Rejected by invariant 9. The book is wrong at
  that moment; a warning leaves it wrong and makes correctness depend on somebody
  reading stderr.
- **Have `pt validate` leave the rebuilt state in place after reporting.**
  Rejected: it repairs the evidence. Running the command twice would report a
  break and then a clean file, with nothing to show which was true.
