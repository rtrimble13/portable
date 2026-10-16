# ADR 0028 — An instrument with no market price is valued at zero, at level 5

- **Status:** Accepted — implemented in schema 7 (`instrument.has_market_price`), `pt instrument market-price` and `pt instrument add --no-market-price`
- **Date:** 2026-10-16
- **Milestone:** v0.2
- **Governs:** `PORT-GIPS-A02`, `PORT-GIPS-H05`, `PORT-GIPS-J03`; `CLAUDE.md` invariants 9, 10
- **Answers:** [ADR 0020](0020-historical-price-import.md) R7

## Context

Some holdings will never have a market price from any provider. The one that
prompted this is `003CVR016`, the Abiomed contingent value right. Valuation
had one answer for a holding it cannot price: leave it out and mark the day's
snapshot incomplete. An incomplete snapshot is never used for a return, by
design. So for as long as the CVR is held, no snapshot of its account is
complete, and performance for everything else in that account is blocked too.

ADR 0020 offered two answers: a flag on the instrument (a schema change), or
a standing manual zero price reaffirmed periodically. The owner chose the
flag. It is explicit in the data, where a manual zero looks like a stated
valuation and has to be remembered.

## Decision

1. **`instrument.has_market_price`**, default 1 (schema 7, migration 0007,
   additive). 0 says nothing quotes this instrument and nothing will.
2. **A holding of one is valued at zero, as a level-5 valuation**
   (`PORT-GIPS-A02` (e): subjective, unobservable inputs). Zero is the value,
   not a gap, so the snapshot is complete. The holding is recorded in the
   snapshot's price set like any price (`PORT-GIPS-J03`): price 0, source
   `none: no observable market price`, level 5, no price row behind it, and
   an as-of of the valuation date so the record is deterministic. It counts
   toward the level-5 share of the portfolio (`PORT-GIPS-H05`), at zero.
3. **It is a property of the instrument, not of a date**, so it is refused
   for an instrument that has ever had a price: every one of those dates
   would be revalued at zero. A security that stops trading is priced until it
   stops, and its disposal is recorded when it happens. That is a different
   problem, and not this flag's.
4. **No price is taken for a marked instrument.** `pt price set` refuses it.
   `pt price load` does not ask for it, and says so.
5. **Changing the mark follows ADR 0025.** A stored snapshot holding the
   instrument changes when the mark does: from incomplete to complete, or
   back. That is refused with `PT-E-RESTATES-HISTORY` unless `--restate`.
   With it, each such snapshot is derived again with the prices it recorded,
   so nothing else in it moves. A zero recorded for an unmarked holding is
   not offered back as a price on re-derivation, so un-marking makes those
   snapshots incomplete again, as they should be, and `pt validate` agrees.

## Consequences

- An account holding the CVR can produce complete snapshots, and so returns,
  once the instrument is marked and the affected snapshots restated.
- The zero is disclosed in the snapshot's own record. `pert`, when it
  reports, should list holdings valued this way beside the level-5 share,
  so a reader can tell "worth nothing" from "not looked at".
- When the CVR pays out (or expires), that is a ledger event like any other
  (`merger_cash`, `delist`), and the realized gain or loss is the tax engine's
  as usual. The mark does not change basis, holding period or tax.

## Alternatives considered

- **A standing manual zero price** (`pt price set ... --price 0
  --valuation-level 5 --estimate`), reaffirmed within the staleness tolerance.
  It needs no code, but it is a chore that fails silently when forgotten (the
  snapshot goes incomplete again), and a zero entered as a price reads as a
  stated valuation. Rejected by the owner.
- **Effective-dating the mark.** It would cover a security that becomes
  unquotable partway through its life. Rejected for now: the case in hand
  never had a price, and a dated mark is a policy question (when did it stop
  being quotable, and at what value) that a ledger event answers better.
