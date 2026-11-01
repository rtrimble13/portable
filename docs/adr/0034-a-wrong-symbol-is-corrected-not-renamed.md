# ADR 0034 — A wrong symbol is corrected, not renamed

- **Status:** Accepted — implemented as `pt instrument correct-symbol` (`services/security_master.py`, `InstrumentRepository.correct_symbol`)
- **Date:** 2026-11-01
- **Milestone:** v0.2
- **Governs:** `CLAUDE.md` invariants 2, 9; `PORT-GIPS-J03`; the crosswalk rule of `docs/broker-import.md` §6 ("no fuzzy matching")
- **Relates to:** [ADR 0020](0020-historical-price-import.md) R4–R5 (the ticker a day is priced under), [ADR 0029](0029-prices-through-duk.md), [ADR 0031](0031-price-acceptance-against-statements.md)

## Context

The reference custodian's transaction export names securities by description,
so its adapter carries a crosswalk from description to symbol. Where the
holdings export did not settle a symbol, the crosswalk proposed one and marked
it `symbol to verify`. On 2026-11-01 the ledger was reconciled against every
monthly statement the custodian had issued since 2022 (160 of them). Four of
the proposed symbols were wrong. Each time it was the same position under two
names, the book's and the statement's, in every month it was held:

| Book | Statement | What the book's symbol is in fafnir |
|---|---|---|
| ARSMX | ARIMX | the same fund's Class N |
| FZAMX | FIDFX | a different fund |
| TRPUX | TGIPX | nothing |
| WSMRX | WBCRX | nothing |

Quantities reconciled anyway, because the reconciliation that accepted the
import compared units, and units are units. Pricing would not have refused
either: `pt price load` asks fafnir for the ticker the master records, and
fafnir answers for FZAMX and ARSMX with another security's prices. This is
the trap `docs/broker-import.md` §6 names: "a name resolved to a *plausible*
wrong symbol — one share class for another — reconciles at the quantity level
and is wrong at every other."

A fifth difference was a real rename the master had never recorded: Pure
Storage became Everpure on 2026-04-20, ticker PSTG to P, and the master had
named the instrument P for all of history.

`pt` had one way to change a symbol, `pt ca symbol-change`. That records a
rename: it closes the old symbol's period on a date, opens the new one, and
writes a `symbol_change` row to the ledger, because a rename is an event in
the security's life. Used for a crosswalk error, it would write an event that
never happened, and leave the wrong ticker in force for every day before its
date, which is every day that matters.

## Decision

**`pt instrument correct-symbol OLD --to NEW --reason "..."`** says the
instrument was never OLD:

1. Every period of the instrument's symbol history that carried OLD carries
   NEW instead, with the same dates. No period is opened or closed. The
   instrument row follows if OLD was current.
2. **No ledger row.** The ledger records events, and nothing happened. Ledger
   rows refer to the instrument by id, never by symbol, so none changes
   meaning. Notes written at the time ("converted into … FZAMX") keep their
   text, as every ledger row does.
3. **The change is logged** in `meta_change_log`, keyed
   `instrument:<id>:symbol`, with the old value, the new value, the start of
   the earliest corrected period as its effective date, and the reason. The
   reason is required. The log is the existing home for redefinitions and
   name changes (`PORT-GIPS-I17`); the key keeps instrument corrections apart
   from the portfolio-level entries that requirement is about.
4. **Refused**, not guessed:
   - **when another instrument carries NEW over an overlapping period.**
     Resolution by NEW would then answer with two instruments. A duplicate in
     the master is a merge, which a symbol correction does not do. A ticker
     another instrument carried on *other* days is not a clash, because
     tickers are reused.
   - **when the instrument has any stored price.** Every price reached it
     through the symbol being corrected, whether a provider was asked under
     that ticker or a file or a hand-set price named it. Each may be another
     security's price. Correct first, then price. No command removes a
     stored price today; one that is needed later is its own decision.

A rename the master never recorded is a correction followed by the rename:
correct P to PSTG, so the master says what the security was called, then
`pt ca symbol-change PSTG --to P --date 2026-04-20` records the event. Each
day then resolves, and is priced, under the ticker it carried.

## Consequences

- A `symbol to verify` is settled against the custodian's own statements
  before pricing, and the crosswalk is corrected in the same change. The
  reference adapter's four entries were corrected, and three others that the
  statements confirmed (TRSGX, LCGFX, WSMDX) are marked verified.
- `pt instrument sync` compares the corrected master afterwards. A
  difference it then shows is fafnir's to explain, not portable's to adopt.
- A statement line already recorded as a price anchor (ADR 0031) is keyed by
  instrument and is unaffected.

## Alternatives rejected

- **`pt ca symbol-change` dated before the first holding.** It writes a
  rename into the ledger that did not happen. Dated at any real date, it
  leaves the wrong ticker in force before it.
- **Editing the instrument row and its history by hand in SQL.** That leaves
  no record of what changed or why, which `PORT-GIPS-J03` exists to prevent,
  and it is a write outside the repositories.
- **Rebuilding the portfolio from a corrected crosswalk.** It is correct in
  principle, but it discards the hand-recorded corporate actions, the
  return-of-capital splits and the conversions, which all would have to be
  re-recorded to change four labels.
- **Correcting and keeping stored prices.** That keeps a number found under
  the wrong ticker, which is the silently wrong number invariant 9 forbids.
