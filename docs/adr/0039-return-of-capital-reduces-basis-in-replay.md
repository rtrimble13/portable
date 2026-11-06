# ADR 0039 — Return of capital reduces basis in replay, entitled on the ex-date

- **Status:** Accepted. The owner asked for the fix (2026-11-06). The rules below are the implementer's and were surfaced to the owner with the change
- **Date:** 2026-11-06
- **Governs:** `CLAUDE.md` "Return of capital" and "Ex-date vs. pay-date" in "Domain traps", invariants 3, 9; `docs/tax-methodology.md`
- **Extends:** [ADR 0010](0010-derived-state-and-replay.md). A basis adjustment is derived state, so it comes from replay

## Context

`pt income roc` appended a `return_of_capital` row that moved cash, and that
was all.

- Replay stopped at the row, because it carries an instrument and no
  quantity.
- `CorporateActionEngine.return_of_capital`, which reduces basis and splits
  off any excess as gain, had no caller.

The basis was therefore never reduced, live or on a rebuild. Buy 100 shares
at 50.00, receive 200.00 of return of capital, and sell at 60.00 a year
later: `pt tax` reported 1,000.00 of gain where the figure is 1,200.00. The
sample portfolio's VTI return of capital was ignored the same way.

`docs/broker-import.md` §9 sends the owner to `pt income roc` for a
distribution reclassified as return of capital. That remedy could not work.

## Decision

1. **Replay derives it.** A `return_of_capital` row is dispatched before the
   quantity check, and it reduces the basis of the lots it entitles. A
   `lot_basis_adjustment` row with reason `return_of_capital` records each
   reduction. The command already appends through `apply_or_rebuild`, so a
   live entry and a rebuild agree.
2. **Entitlement is fixed on the ex-date, and the reduction on the
   pay-date.**
   - The shares entitled are the long lots in the account before the
     ex-date, and still open when the row is applied.
   - "In the account" is measured from the trade date of the row that put
     the lot there, not from its acquisition date. A lot transferred in keeps
     its older acquisition date, but it was not here to be entitled.
   - The row sorts on its pay-date, so that is when the reduction applies.
     That is when the capital came back.
   - `pt income roc` takes `--ex-date`, which defaults to the pay-date.
3. **The total is allocated by quantity, by largest remainder**, so the parts
   sum exactly to what was paid. The row states a total, not a rate per
   share.
4. **The excess over basis is a capital gain**, recognised on the pay-date
   at the lot's own holding period. There is no negative basis, and the
   holding period is not reset.
   - It is recorded as a disposition of **no units**: the lot, the excess as
     proceeds, no basis, and the excess as the gain.
   - Every report that reads gains therefore shows it, with its estimate.
   - Nothing in the schema forbids a zero quantity, and no consumer divides
     by one.
5. **Refused, with `PT-E-ROC-ENTITLEMENT`:**
   - **Shares held before the ex-date but disposed of before the row,** in
     whole or in part. They were entitled, but their basis is gone. Whether
     their part then adjusts that sale, or something else, is not decided
     here (invariant 9).
   - **A return of capital with no shares entitled.**

## Consequences

- **Every file with a `return_of_capital` row restates.** `pt validate`
  reports a replay mismatch until `pt rebuild`. That rebuild reduces those
  bases, and the gains of later sales of those shares rise by the amount
  reduced. Run `pt tax` for the affected years before and after.
- **The sample portfolio's VTI lots** carry the 2025-08-01 reduction.
- **A reclassification is two rows.** A distribution already recorded as a
  dividend and later reclassified as return of capital means reversing the
  dividend row and entering the return of capital. Recording the return of
  capital alone counts its cash twice. `docs/broker-import.md` and the
  broker-import skill now say so.
- **`pt reconcile --realized`** shows an excess-over-basis gain as one the
  custodian's lot report lacks. Custodians report nondividend distributions
  on Form 1099-DIV, not lot by lot, so the line is expected. The owner
  checks it against box 3.
- **A return of capital on a short position** is not handled. That is a
  payment in lieu, which the short seller makes; it is refused as having no
  entitled shares.

## Alternatives considered

- **Apply it on the ex-date.** Entitlement is decided then, but the row sorts
  on its pay-date, as every income row does. Sorting this one row
  differently would make the ledger order disagree with the cash balance
  between the two dates. Rejected. Entitlement is read from the ex-date
  instead.
- **Reduce whatever lots are open on the pay-date.** That reduces shares
  bought after the ex-date, which received nothing. Rejected.
- **Refuse the excess over basis** rather than record it. The rule is well
  established, and refusing it would block any long-held fund whose
  distributions have run its basis down. Rejected.
