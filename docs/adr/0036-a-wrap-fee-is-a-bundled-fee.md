# ADR 0036 — A wrap fee is a bundled fee, deducted at every return basis

- **Status:** Proposed — needs the owner's go-ahead for the schema change and the ledger correction below. Until then `pert` computes net-of-fees returns only, which this question does not affect
- **Date:** 2026-11-02
- **Milestone:** v0.2
- **Governs:** `PORT-GIPS-D01`, `PORT-GIPS-D02`, `PORT-GIPS-D03`, `PORT-GIPS-I04`; `CLAUDE.md` invariants 2, 9
- **Corrects:** [ADR 0014](0014-advisory-fees-paid-across-accounts.md) — "The fee class"

## Context

ADR 0014 classed every advisory fee of the reference custodian as
`external_mgmt_fee`, and said the accounts are "a wrap arrangement in which
the commission column is empty", so that gross-of-fees and
net-of-external-costs-only are "numerically identical for these accounts".
The conclusion is right and the reasoning is not. Under the ladder
`docs/gips-standard.md` §6.D adopts, the whole difference between those two
bases *is* the external management fee. An `external_mgmt_fee` is added back
for gross-of-fees, so the two would differ by every quarter's fee.

The owner confirmed on 2026-11-02 that the fee is **a true wrap**: one fee
that pays for the management and the trading, and not decomposable. That is
`PORT-GIPS-D03`'s bundled fee (Firms 2.A.14, AO 22.A.11), which
`docs/gips-standard.md` marked "not applicable to a self-directed brokerage
portfolio". It applies. Its rule: where transaction costs cannot be estimated
or segregated from a bundled fee, gross-of-fees returns are reduced by the
entire bundled fee or the portion of it that contains the transaction costs,
and net-of-fees returns by the entire fee or the portion containing the
transaction costs and the management fee. No schedule here separates the
portions, so **the entire fee is deducted at every basis**. Gross-of-fees,
net-of-external-costs-only and net-of-fees are then equal for these accounts,
because nothing else is deducted at one basis and not another. That is the
figure ADR 0014 predicted, for the reason this ADR gives.

Leaving the rows as `external_mgmt_fee` would make the gross-of-fees return
add the fee back. That is a return before trading costs, which
`PORT-GIPS-D02` says is not a GIPS return at all ("pure gross-of-fees",
supplemental only). It would overstate gross by the whole wrap fee, every
quarter.

## Decision (proposed)

1. **A sixth fee class, `bundled_fee`**: a fee that includes transaction
   costs and cannot be decomposed. It is deducted at every basis, gross
   included, like a transaction cost. A schedule that did decompose it would
   be recorded as two fees of their own classes instead. The `fee_class` CHECK
   gains the value. SQLite cannot alter a CHECK, so this is a table-rebuild
   migration under ADR 0019 (migration 0010, schema version 10), tested in
   both directions, with a `CHANGELOG.md` entry.

2. **Existing rows are corrected by reversal and re-entry, not rewritten.**
   `fee_class` is a stored fact on an immutable row, and here the fact was
   wrong. Invariant 2 has one remedy for that. A command, proposed as
   `pt fee reclassify --class external_mgmt_fee --to bundled_fee -a <account>
   --reason "..."`, writes for each matching fee a reversal and a new fee of
   the same date, amount and account in the new class, in one transaction,
   with `--dry-run` rehearsing it (ADR 0026). The reversed row keeps its
   `external_ref`, so an overlapping `--incremental` import still recognises
   it as already recorded and does not append it again. The re-entry carries
   no reference of its own. The reason is recorded on every pair. This is
   roughly a hundred pairs for the reference portfolio, and the dry run shows
   each one.

3. **The adapter's mapping changes with it**: `Expense (Management Fee)` maps
   to `fee_class = "bundled_fee"`, so new imports arrive correct.

4. **The disclosure says it** (`PORT-GIPS-I04`): the wrap fee is a bundled fee
   that includes trading costs, deducted in full at every return basis. That
   is why the gross-of-fees, net-of-external-costs-only and net-of-fees
   figures are equal for the accounts that pay one. `pert` reports the figure
   once and names all three bases it stands for, not three identical columns.

## Consequences

- `docs/gips-standard.md` `PORT-GIPS-D03` changes from "not applicable" to
  applicable, which §13 says needs an ADR. This is that ADR, and the
  `CHANGELOG.md` records it.
- Until decisions 1–3 land, `pert` refuses gross-of-fees and
  net-of-external-costs-only. Net-of-fees, the asset-owner standard's required
  presentation for a total fund (24.A.1.b), deducts the whole fee whichever
  class it carries, and is unaffected.
- Margin interest is unchanged: a financing cost that reduces every basis.

## Alternatives considered

- **Leave the rows and declare the account a wrap account**, so that an
  `external_mgmt_fee` in it is read as bundled. Rejected: the class would then
  be inferred at report time from a second fact. `CLAUDE.md` says fee
  classification is a stored enum, "not an inference at call time".
- **A migration that rewrites `fee_class` on the existing rows.** Rejected: it
  edits history, which the ledger's triggers forbid for a reason. The
  correction would leave no trace, and the tax trail is defensible only
  because every change to the ledger is a row.
- **Estimate the trading portion and split each fee.** `PORT-GIPS-D02`
  permits estimated transaction costs only where actual costs are unknown,
  and requires the method disclosed. No basis for an estimate exists in this
  data. Rejected unless the custodian's agreement gives one.
