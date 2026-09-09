# ADR 0017 — Reconstructing the cutover state by roll-back, and recording where each basis came from

- **Status:** Proposed
- **Date:** 2026-09-09
- **Milestone:** v0.2
- **Governs:** `CLAUDE.md` invariants 9, 10; `PORT-GIPS-J03`
- **Amends:** ADR 0015 — replaces the flat refusal on averaged basis with a recorded provenance ladder

## Context

ADR 0015 and `docs/broker-import.md` were written assuming two further documents
could be obtained: the transaction export re-run from account inception, and a
lot-level cost basis report. **Neither is available.** The three exports already
in hand are the whole of the evidence, permanently.

That closes the question those documents were meant to answer and opens a
sharper one. ADR 0015 says a position-level average cost is refused rather than
seeded as a lot, because an averaged block silently imposes average-cost relief
on an account whose method is spec-ID or FIFO. Applied literally to what is
actually available, that rule refuses to build the portfolio at all — which is
not a defensible reading of invariant 9. Invariant 9 requires failing loudly on
ambiguity *and* providing an explicit path where a human can legitimately
decide. Refusing to function is the failure mode invariant 10 warns about from
the other direction: a tool that cannot be used produces no wrong numbers and no
right ones.

The real requirement is therefore not "refuse approximation". It is **never let
an approximate number be mistaken for an exact one**.

### What the evidence actually supports

Measured against the three exports:

- **Position quantities at a cutover date are exactly recoverable.** Applying the
  transaction file in reverse to the dated holdings snapshot yields the holding
  of every instrument on the day before the transaction file begins. Over 106
  account-and-instrument pairs this produced 70 positions held at cutover, 33
  opened after it, and **three discrepancies, all explained**: one is the
  symbol-change modelling described in `docs/broker-import.md` §5, and two are
  sub-share differences between a two-decimal transaction quantity and a
  three-decimal holdings quantity.
- **Basis at cutover is exactly recoverable for most of it.** Where a position
  saw no disposal and no share-class transformation after the cutover, its
  cutover basis is today's basis less the cost of every subsequent addition.
  This holds for **38 of the 70 positions, carrying about 83% of the
  pre-cutover cost basis**, and produced no negative or implausible unit cost.
- **The remaining 32 positions, about 17% of pre-cutover basis, are not exactly
  recoverable**, because shares were sold after the cutover and which lots the
  custodian relieved is not recorded anywhere available.
- **Nothing before the cutover can be reconstructed at all.** The accounts were
  funded more than two years earlier and turned over completely in the gap, with
  no transaction record and therefore no possible valuation series.

## Decision

### 1. The cutover is the transaction file's first date

The portfolio's reporting inception is the day the transaction history begins,
not the day the accounts were funded. Position quantities at that date come from
the roll-back described above; each becomes a `transfer_in` (ADR 0015).

The earlier funding events in the capital-flows export are **not** loaded as
ledger rows. They are external flows into a period for which no market value can
ever be computed, and a flow with no valuation on either side of it does not
produce a return — it produces a division that looks like one. They are recorded
instead as `portfolio_event` rows, which exist to let a reader interpret a report
and are exactly the right home for "this account was funded in kind two years
before this record begins".

### 2. Every lot records where its basis came from

A new `NOT NULL` column on `lot`, with **no default**, so that no writer can add
a lot without answering the question:

| `basis_source` | Meaning |
|---|---|
| `derived` | Computed by `portable` from its own ledger. Exact. |
| `reconstructed` | Cutover block, basis obtained by subtracting subsequent additions from the custodian's stated current basis. Exact **as an aggregate**, averaged within the block. |
| `estimated` | Cutover block where a disposal since the cutover makes the aggregate unrecoverable; solved backwards under a stated relief-method assumption. |
| `custodian_asserted` | Taken directly from a custodian lot-detail report. Reserved; nothing supplies it today. |

`reconstructed` and `estimated` lots additionally carry the assumption that
produced them, so the arithmetic can be re-derived and re-argued later.

### 3. Approximation is disclosed at the point the number is used

`pt tax` and `pt pnl` mark any realized gain whose disposition consumed a lot
that is not `derived`, and state the proportion of the reported figure that rests
on such lots. A report in which every number is exact and a report in which a
sixth of the basis is reconstructed must not look identical, because the reader's
next action differs.

This is the same principle as `valuation_snapshot.uses_estimates` and
`price.is_estimate` — already in the schema for exactly this reason — extended
to the other input that can be estimated.

### 4. What remains exact, stated precisely

Reconstruction degrades one number and leaves others untouched, and the
difference is worth being exact about.

**Holding-period character is certain for any disposition more than one year
after the cutover.** Every seeded lot was acquired on or before the cutover date,
so even the latest possible true acquisition date is more than a year before such
a disposition. It is long-term whatever the seeded date says. Of the 42
dispositions in the transaction file, **28 fall in that window and are certain**.

The remaining 14 fall within the first year after the cutover, where character
depends on the seeded acquisition date being right. All 14 are in the taxable
account, in tax years already filed from the custodian's 1099-B — so the exposure
is to `portable`'s reporting of past gains, not to a filing. The seeded date is
taken from the holdings export's `Open date`, which is the block's **earliest**
acquisition and therefore biases toward long-term. That bias is the wrong
direction to be relaxed about, so the reconstruction **enumerates** those
dispositions in its report rather than counting them, and they are reviewed
individually. The list is generated, not written down here: it changes with the
cutover date, and a copy in a document would go stale silently.

**Current holdings and current basis are exact**, because the reconstruction is
anchored to the custodian's stated present position. That is the number that
governs every *future* tax-aware decision, which is what `pt` is for.

**Cash, income, fees, and external flows after the cutover are exact.** They come
from the transaction file directly and are unaffected.

## Consequences

- Schema change: `lot.basis_source NOT NULL`, no default; a nullable
  `basis_assumption` for the reconstruction argument. Migration,
  `schema_version` bump, `CHANGELOG.md` entry.
- Adding the column with no default means every existing writer and every fixture
  must state a value. That is the intent — it is not a field to be filled in
  later.
- The reconstruction is a `portable_core` service with its own tests, not a
  script run once. It has to be re-runnable, because the correct response to
  finding a mapping error is to re-derive the cutover state and rebuild, not to
  patch lots.
- A pre-cutover block is one lot, so specific identification within it is not
  available. `pt sell --lots` can name the block; it cannot name shares inside
  it. For an account whose default is spec-ID this is a real reduction in
  capability, and the honest description is that the custodian's records support
  spec-ID and `portable`'s reconstruction of them does not.
- One position arrives with a genuine zero basis — a contra/CVR security from an
  acquisition. `BasisAdjustmentReason.FORCED_ZERO_BASIS` already exists for this
  and a zero basis here is `derived`, not `estimated`.
- `docs/broker-import.md` §9's acceptance test is unchanged and becomes more
  important: the reconstruction is credible because current holdings and current
  basis reconcile to the custodian exactly, and that check is the only thing
  standing behind it.

## Alternatives considered

- **Cut over at the holdings snapshot date instead.** Every basis would be
  `custodian_asserted` and exact, with no reconstruction at all. Rejected: it
  discards two years of transaction history that is complete and exact, and
  leaves no track record whatsoever. The reconstruction's imprecision is confined
  to pre-cutover basis; cutting over today buys precision that is already
  available and pays for it with everything else.
- **Refuse, per ADR 0015 as drafted.** Rejected above: it declines to build the
  portfolio at all on evidence that supports building most of it exactly, and
  offers the owner nothing in exchange for the refusal.
- **Seed the averaged blocks silently and say nothing.** Rejected — this is the
  silently-wrong-number failure in its purest form, and the whole repository is
  organised against it.
- **Reconstruct the pre-cutover period from market history and inference.**
  Rejected: no record of what was held or traded exists for that period, so any
  reconstruction would be invention with a plausible shape.
