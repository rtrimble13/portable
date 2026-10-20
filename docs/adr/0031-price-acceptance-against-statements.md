# ADR 0031 — Price history is accepted against the custodian's stated prices, to the cent

- **Status:** Accepted — implemented in schema 8 (`statement_position`, `price_acceptance`), `services/price_acceptance.py`, `pt reconcile --against … [--record]`, `pt price check` and `pt price accept`
- **Date:** 2026-10-20
- **Milestone:** v0.2
- **Governs:** `CLAUDE.md` invariants 1, 9; `PORT-GIPS-A01`, `PORT-GIPS-J03`
- **Answers:** [ADR 0020](0020-historical-price-import.md) R10, the price half (the trade-price check is its own section of that ADR's progress)

## Context

A return needs every held instrument priced on every held day (ADR 0020).
Loading those prices is one thing. Knowing they are right is another, and
nothing checked it. Broker import has an acceptance test: reconcile to the
custodian (`docs/broker-import.md` §9). Price history had none.

The custodian already states what each position was worth on a statement
date. Those statements are independent anchors: a price from the wrong day,
the wrong instrument (a reused ticker) or the adjusted series disagrees with
them.

The owner decided (2026-10-20), on the recommendations put to them:

1. **Anchors:** the holdings export already used for the import, and each
   account's year-end statement for every year since 2022. Annual returns are
   measured between year-ends, so a wrong December 31 price moves two years at
   once. History starts at 2022-05-18.
2. **Compare the price, not the market value, to the cent.** A percentage of
   market value either hides a price from the wrong day on a large position or
   flags rounding as a break.
3. **A break blocks that account's returns from its date** until it is fixed
   or accepted with a written reason.
4. **The check lives in `pt reconcile --against`**, which already compares
   quantities and cash per position.

## Decision

### 1. The comparison

For each statement line that carries a `market_value` and is not cash:

- the **custodian's price** is its market value divided by its quantity
  (times the contract size);
- **portable's price** is the one valuation would use on the statement date:
  the newest on or before it, within the staleness tolerance
  (`ValuationEngine`'s rule). An instrument marked as having no market price
  is valued at zero (ADR 0028), and the custodian's figure is compared with
  that zero.

The line **ties** when the two prices agree to the cent, allowing for the
statement having rounded its market value to the cent:

    |portable price × quantity − market value| ≤ $0.01 × quantity + $0.005

It is computed on the custodian's quantity, so that the price question is
asked on its own. A quantity disagreement is the quantity line's break, not
a price break.

The tolerance is fixed. It is not the `--tolerance` flag, which governs
quantities and cash. A legitimate exception, such as a bond or fund the
custodian values with its own evaluated price, is accepted with a reason
(§3). It is not absorbed by a looser threshold.

A line with no usable price (none on or before the date, or stale beyond
tolerance) is **unpriced**. That is unresolved too: an unchecked price is not
an agreeing one.

### 2. Statements become anchors in the `.port`

`pt reconcile --against statement.csv --as-of D --record` stores each priced
line as a `statement_position` row: account, date, instrument, the
custodian's quantity and market value, and the file it came from.
`--record` requires an explicit `--as-of`, because the statement's date must
be stated, not defaulted. A line already recorded for that account, date and
instrument is replaced.

Why store them? A break must block returns until it is resolved, and `pert`
will compute returns long after the statement file is gone. The anchor is
reference data, like a price: the ledger does not derive it and a rebuild
does not touch it. Whether it ties is derived on demand, from the anchor and
the prices held at the time. So **reloading a price re-decides every anchor
it affects**, and nothing has to be invalidated.

`pt reconcile --as-of D` now also works for past dates. Positions and cash
are read as they stood at the end of D (ADR 0021). The refusal of `--as-of`
in ADR 0021's Consequences is withdrawn.

### 3. Accepting a break

`pt price accept SYMBOL --account A --date D --reason "…"` records a
`price_acceptance`. It holds the reason and exactly what was accepted:

- the custodian's quantity and market value;
- portable's price and that price's date.

**The acceptance applies only while all four are unchanged.** If a price is
reloaded or the statement re-recorded with different figures, the line is a
break again and must be looked at again. An acceptance is a person's judgment
about one specific disagreement, never a standing exemption for a position.
An unpriced line cannot be accepted: there is no price to accept. A price is
loaded or set first.

### 4. What a break blocks

`price_acceptance.blocked_from(account)` is the earliest recorded statement
date with an unresolved line. `pert` will refuse that account's returns for
any period ending on or after it. This is the gate the owner decided, and
`pert` is not built yet. Until then:

- `pt price check` evaluates every recorded anchor, lists each line with its
  status, and exits **6** when any is unresolved, like any reconciliation
  break;
- `pt value` warns, for every account it values on or after its blocked date,
  that returns from that date are blocked and why.

Valuation snapshots are still built and stored. A snapshot records what the
prices were; the gate decides whether a return may be computed from them.

## Consequences

- **Schema 8**, additive: two new tables, both exported by `pt export`. A
  rebuild leaves them alone.
- An anchor is the custodian's statement. It contains amounts, as the ledger
  does, and stays in the owner's `.port` like everything else. Nothing from
  a real statement enters version control.
- The trade-price check from R10 (fills against the day's range) is separate.
  It flags for review and never blocks.
- Bond positions: custodians usually state market value without accrued
  interest, and price bonds with evaluated prices. Expect them as breaks to
  accept with a reason until bond pricing is built.

## Alternatives considered

- **A percentage of market value** (max($1, 0.1%) was the first suggestion).
  Rejected by the owner: $100 on a $100k position can hide a price from the
  wrong day.
- **Compare without storing**: `pt reconcile` only, with breaks reported and
  forgotten. Rejected: nothing could block returns later, and the result
  would depend on which file someone remembered to pass.
- **Store the reconciliation result instead of the anchor.** Rejected: a
  stored "tied" goes stale the moment a price is reloaded. Storing the
  anchor and deriving the result cannot go stale.
- **An acceptance that exempts a position for good.** Rejected: a later wrong
  price would ride on an old explanation.
