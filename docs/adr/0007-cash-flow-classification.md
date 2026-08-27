# ADR 0007 — Cash-flow classification is one level-aware service function

- **Status:** Accepted
- **Date:** 2026-08-27
- **Milestone:** v0.1
- **Governs:** `PORT-GIPS-B02`

## Context

`docs/gips-standard.md` calls `PORT-GIPS-B02` "the highest-risk item in the whole
document". The reason is that the correct classification of a cash movement
**depends on the level at which the return is being computed**:

- a transfer between two of the owner's own accounts is an external flow at
  **account** level and is **not** one at **portfolio** level, because it nets to
  zero;
- income — dividends, coupons, reinvestments, return of capital — is **never** an
  external flow at any level;
- a fee is a cost, not a flow.

Treat an inter-account transfer as external at portfolio level and a $100k
shuffle between the owner's own accounts silently rewrites the track record. The
resulting number is arithmetically defensible and economically meaningless, which
is the worst possible shape for a bug.

## Decision

Classification lives in **exactly one function**:

```python
# src/portable_core/services/cash_flow.py
def classify(txn: Transaction, level: FlowLevel) -> FlowClassification: ...
```

- `FlowLevel` ∈ `{ACCOUNT, PORTFOLIO}`.
- `FlowClassification` ∈ `{EXTERNAL, INTERNAL, INCOME, COST}` plus a signed
  `Decimal` amount and the flow date (day resolution, `PORT-GIPS-C02`).
- The function is **total** over `TransactionType`: a `match` with no default
  arm, so adding a transaction type without classifying it fails `mypy
  --strict`'s exhaustiveness check *and* raises at runtime.

**No call site re-derives it.** `pt cash-flows`, `ValuationEngine`, and
(in v0.2) every `pert` return path call this function and nothing else. There is
a test that greps the tree for the transaction types this function switches on,
appearing in a comparison anywhere outside it.

The `PORT-GIPS-B02` matrix is transcribed **verbatim** into
`tests/unit/test_cash_flow_classification.py` as a parametrised table, including
the rows whose answer is "no". A row that disappears from the matrix must
disappear from the test in the same commit.

### The portfolio-level netting rule, stated precisely

An inter-account transfer is recorded as a **single ledger transaction** with an
`account_id` and a `counter_account_id`, not as two independent transactions.
This is what makes portfolio-level netting structural rather than a matching
heuristic:

- at `ACCOUNT` level it yields two flows, `-amount` for the source and `+amount`
  for the destination;
- at `PORTFOLIO` level it yields **no flow at all** — not two flows that happen
  to sum to zero.

The distinction matters because "two flows that cancel" still triggers
revaluation and sub-period breaks under `PORT-GIPS-B03`, and would still appear
in the daily flow series that `PORT-GIPS-C02` feeds to the money-weighted solve.
Netting at the point of classification, not at the point of summation, is the
only version that is correct downstream.

## Consequences

- `pt cash transfer` is a first-class command that writes one transaction, and
  a transfer **cannot** be expressed as a withdrawal plus a deposit. Attempting
  that produces two genuine external flows at portfolio level — which is a true
  statement about what was recorded, and a wrong statement about what happened.
  `pt validate` flags a same-day withdrawal/deposit pair of equal amount across
  two accounts in the portfolio as a probable mis-entered transfer.
- The classification is **not** stored on the transaction row. It is derived, and
  it has two answers per transaction, so a stored column would have to be
  duplicated per level and would drift. `transaction.fee_class`
  (`PORT-GIPS-D01`) *is* stored, because that one has a single answer and is a
  fact about the fee, not about the report.
- In-kind transfers and stock distributions are external flows **valued at the
  time of distribution** (`PORT-GIPS-C02`, Firms 2.A.29.c), so the function
  returns an amount that the caller must have priced; it takes the valuation as
  an argument rather than reaching for a provider, keeping the domain pure.

## Alternatives considered

- **A stored `flow_class` column on `transaction`** — rejected: level-dependent,
  so it would need two columns that can disagree with each other and with the
  ledger.
- **Classification per call site with a shared enum** — rejected by the standard
  itself, which requires it "in exactly one service function", and by experience:
  the second call site is where the drift starts.
