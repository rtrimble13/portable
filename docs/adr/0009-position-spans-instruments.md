# ADR 0009 — A position spans instruments; lots hang off legs

- **Status:** Accepted
- **Date:** 2026-08-27
- **Milestone:** v0.1

## Context

Bootstrap §4.4: "**A position may span multiple securities** — a vertical spread,
a covered call, a collar, a bond plus its hedge." Positions track P&L
**independent of tax**; tax is an account-level concern (§4.2). Lots are the tax
engine's atoms (§4.5).

Most portfolio systems make "position" a synonym for "holding of one instrument
in one account". That model cannot express a covered call as one thing, and the
owner trades covered calls.

## Decision

Three tables, and the distinction between them is load-bearing:

```
position(position_id, account_id, strategy_type, opened_date, closed_date, status)
   └── position_leg(leg_id, position_id, instrument_id, role, sign)
          └── lot(lot_id, leg_id, position_id, instrument_id, account_id, ...)
```

- **`position`** is the container and the unit of *trader intent*. Its
  `strategy_type` ∈ `single`, `covered_call`, `vertical`, `calendar`, `collar`,
  `custom`. It is the unit at which P&L is reported and at which strategy-level
  analytics (greeks, max gain/loss — interface only in v0.1) will attach.
- **`position_leg`** binds one instrument to a position with a `role`
  (`underlying`, `short_call`, `long_put`, …) and a `sign` (+1 long, −1 short).
  The role is what lets the `CorporateActionEngine` and the option-lifecycle
  commands know that *this* short call is the one written against *that* stock.
- **`lot`** hangs off a **leg**, not off a position, and carries
  `instrument_id` and `account_id` denormalised for the queries the `LotEngine`
  runs. A leg has many lots; a lot belongs to exactly one leg.

### Consequences that fall out of this, and are the reason for it

1. **`sum(lot.remaining_quantity) == position.quantity` is per leg, not per
   position.** A position spanning two instruments has no scalar quantity.
   `CLAUDE.md` invariant 5 is therefore implemented and property-tested as
   *per-leg*, and the invariant text says so. Stating it per position would be
   meaningless for a spread, which is precisely the case the model exists for.
2. **Assignment of a written call moves value between legs.** The premium on the
   short-call leg flows into the *proceeds* of the stock leg's disposition
   (`CLAUDE.md`, "Option premium"). Because both legs are in one position, this
   is a within-position operation the `PositionEngine` can perform atomically,
   with a `lot_basis_adjustment` row explaining it. In a one-instrument-per-
   position model it is a cross-position fixup, which is where these bugs live.
3. **Tax does not see positions.** `TaxEngine` consumes `lot_disposition` rows,
   which carry `account_id` and holding period. It never reads `position`. This
   keeps §4.2's "positions track P&L independent of tax" structural rather than
   aspirational.
4. **Regrouping is a first-class operation.** `pt position group|ungroup` moves
   legs between positions when the trader's intent changes (a long stock holding
   becomes a covered call when a call is written against it). Because lots hang
   off legs and legs carry `position_id`, regrouping updates one column and does
   not touch a single lot or basis figure. Tax history is untouched by a change
   of intent, which is correct.

### Derivation, not entry

`position` and `position_leg` are **derived state** (ADR 0010), rebuilt by
replay. A trade carries an explicit `position_id` or `--new-position`; the
`PositionEngine` applies a documented default when neither is given (extend the
open single-instrument position in that account for that instrument, else open a
new one). The default is applied at *entry* and recorded on the transaction, so
replay is deterministic and a later change to the default cannot restate history.

## Alternatives considered

- **Position = (account, instrument)**, with strategies as a reporting-time
  grouping. Simpler, and how most systems do it. Rejected: the covered-call
  assignment path (consequence 2) becomes a special case that reaches across
  positions, and "what did this collar earn me" becomes a query the user has to
  write correctly rather than a number the system knows.
- **Lots hang off positions, legs are advisory** — rejected: a lot must resolve
  to exactly one instrument for the tax engine, and a position does not.
