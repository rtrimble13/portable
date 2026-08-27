# ADR 0011 — What the tax engine estimates and what it refuses

- **Status:** Accepted
- **Date:** 2026-08-27
- **Milestone:** v0.1

## Context

Accounts track P&L **net of tax** (bootstrap §4.2), with liability estimated on
realized gains using the holding period at disposition and the account's
effective-dated rate schedule. The owner files real taxes. `CLAUDE.md` invariant
10 forbids stubs that pretend to work, and invariant 9 requires failing loudly on
ambiguity.

The risk here is specific: a tax figure that looks like a 1099-B but is not one.

## Decision

The `TaxEngine` computes an **estimate**, labelled as such in every output, and
draws a hard line between what it computes exactly and what it approximates.

### Exact — computed from the ledger, not estimated

- **Realized gain or loss per disposition**: proceeds less adjusted cost basis
  less allocated fees. Exact, because every input is a ledger fact.
- **Holding period**: long-term when the disposition date is **more than one year
  after the day after acquisition**. The one-year-exactly boundary is
  **short-term**. Leap years are handled by real date arithmetic, not by 365.
  Tested at the boundary; those tests are not to be "simplified".
- **Short sales are always short-term**, regardless of holding period.
- **Basis adjustments** from commissions, return of capital, splits, spinoffs
  (by relative fair market value), mergers, and option premium on assignment or
  exercise. Each writes a `lot_basis_adjustment` row explaining itself.
- **Lot selection** under the relief method in force.

### Estimated — and labelled

- **Tax liability** = gain × the account's effective-dated rate for that holding
  period. The rate is stored as **separate federal, state, and NIIT components**
  so the effective rate is explainable rather than a magic number, and the
  components are shown in `pt tax --format json`.

The estimate is deliberately naive and says so. It does **not** model: bracket
progressivity, the capital-loss limitation and carryforward, the $3,000 ordinary
offset, qualified-dividend rate stacking, AMT, state-specific treatment of
federal gains, or the taxpayer's other income. It is "gain × rate", which is
useful for comparing two dispositions and useless as a filing figure.

### Refused

- **Wash sales.** Detection is deferred to v0.2 (backlog, P0). Until it lands,
  `pt tax` prints a **mandatory, non-suppressible** statement that the report
  does not account for wash sales — `CLAUDE.md` names the trap and the reason:
  the 30-day window spans *all* of the taxpayer's accounts including IRAs, and
  substantially identical securities and options. A tax report that quietly
  omits wash sales is the "silently wrong number" failure mode by definition.
  The statement is an envelope field in `--format json`, not a rendered string,
  so a consumer cannot drop it without noticing.
- **Average cost mixed with spec-ID for the same instrument.** Refused outright,
  as the IRS requires, with `PT-E-TAX-METHOD-CONFLICT`.
- **A disposition with no matching lot.** `PT-E-LOT-UNMATCHED`, exit 4. Never
  guessed, never zero-basis by default. `--force-zero-basis` exists so a human
  can make that call explicitly, and it records the decision on the lot's
  adjustment log.
- **A tax computation for an account with no rate schedule in force** on the
  disposition date. `PT-E-TAX-NO-RATE-SCHEDULE`, exit 4. A missing rate is an
  error, not a zero — the same rule `PORT-GIPS-B03` applies to flow policy.

### Standing disclaimer

Every tax output carries: this is not tax advice, and `portable` is not a
substitute for a broker's 1099-B. Separately — and this is a different point that
must not be merged with it — **after-tax performance is outside GIPS entirely**
(removed at the 2010 edition, handed to the US country sponsor). After-tax
returns, when `pert` gains them in v0.2, are **supplemental information** under
`PORT-GIPS-H08` and follow the USIPC After-Tax Performance Standards.

## Consequences

- `pt tax` is genuinely useful for lot selection and year-end planning and is
  honest about being nothing more.
- The exact/estimated split is a column in `docs/tax-methodology.md`, and the
  boundary is testable: everything in "exact" has a unit test asserting a known
  value; everything in "estimated" has a test asserting it is *labelled*.

## Alternatives considered

- **Model brackets and carryforwards** — more accurate in the common case and
  wrong in an unbounded number of uncommon ones, none of which `portable` can
  see because it does not know the taxpayer's other income. Rejected: a figure
  that looks authoritative and is not is worse than one that is obviously an
  estimate.
- **Emit no tax figure at all** — defensible, and rejected because holding-period
  and relief-method choice are exactly what the owner needs the tool for, and
  those are the exact parts.
