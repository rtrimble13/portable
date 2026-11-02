# ADR 0037 — The return engine's conventions: daily sub-periods, flows at the close, and what it refuses

- **Status:** Proposed — the conventions `pert`'s time-weighted return engine (issue #2) is built to. Accepted when that engine lands, or amended by what building it shows
- **Date:** 2026-11-02
- **Milestone:** v0.2
- **Governs:** `PORT-GIPS-A03`, `A09`, `B01`–`B07`, `D01`, `H04`, `H06`; `CLAUDE.md` invariants 1, 6, 9, 10
- **Answers:** [ADR 0015](0015-in-kind-transfers-and-opening-positions.md)'s request that its opening-date exception be re-checked when the return engine lands

## Context

The 2020 GIPS provisions specify what a time-weighted return must achieve
and name no method (`docs/gips-standard.md` §6.B). The choices the provisions
leave to the entity have to be made once, written down, and applied the same
way to every period (`PORT-GIPS-B05`). Made at a call site, they would differ
between call sites. `portable` values every calendar day where prices exist
(`PORT-GIPS-A03`), and the reference portfolio's history is priced daily from
its first day. That decides most of what follows.

## Decision

1. **A sub-period is one calendar day**, between consecutive stored snapshots,
   weekends and holidays included. On a day with no trading, a snapshot's
   prices are the last close, within the staleness tolerance, so its return is
   its flows and accruals. Because every day is valued, the large-cash-flow
   rule (`PORT-GIPS-B03`) is satisfied by construction. Reports say so, and a
   `return_policy` row must still be in force for every day measured, or the
   engine refuses with `PT-E-GIPS-NO-FLOW-POLICY`.

2. **Flows count at the close of their day:**
   `r_t = (EMV_t − EMV_{t−1} − F_t) / EMV_{t−1}`. This is the same for inflows
   and outflows, account level and portfolio level. A flow is dated on its
   trade date, and an in-kind flow is valued at that day's close (ADR 0015),
   so the close is when its value is known. A flow does not earn the return of
   the day it arrives. The convention is disclosed.

3. **A day that begins at zero has no return.** Where `EMV_{t−1}` is zero,
   measurement begins at that day's close. That is ADR 0015's rule, that a
   transfer on the account's opening date sets its beginning value rather than
   flowing into it, reached without a special case. The opening transfer is
   valued at the close, and the first return is the next day's. Re-checked
   against the standard as ADR 0015 asked: in-kind capital is valued at the
   time of distribution (Firms 2.A.29.c), and that value is the beginning
   value, so there is no conflict. A negative beginning value (a margin loan
   larger than the assets) is refused, not divided by.

4. **A break in the track record is not linked** (`PORT-GIPS-H06`). A
   portfolio whose every account reaches zero, and is funded again later, has
   two records, and no return links across the gap. An account at zero while
   others hold value is not a portfolio break.

5. **Days are linked geometrically, in `Decimal`, and nothing is rounded
   before the formatter** (`PORT-GIPS-B04`; ADR 0005's rule for rates). Every
   result records the method of each sub-period (`PORT-GIPS-B05`), and its
   basis and period (`PORT-GIPS-H04`). A period runs from the close of its
   first date to the close of its last, the convention `spans_a_year` uses:
   a calendar year is 31 December to 31 December.

6. **Modified Dietz only where the owner has said a period was not valued
   daily.** It is implemented as the archived 2011 Guidance Statement gives
   it, with the reference tests issue #2 names. It is never used to bridge a
   missing day. A missing daily snapshot is a refusal, not an interpolation
   (`PORT-GIPS-A03`). The reference portfolio has no such period.

7. **What the engine refuses, naming the account and the date:**
   - a day with no stored snapshot for an account open on it;
   - an incomplete snapshot, with the holdings it names (ADR 0035);
   - a snapshot whose beginning value is not the day before's ending value;
   - an account blocked by a price-acceptance break, from the break's date,
     and the portfolio from the earliest such date ([ADR 0031](0031-price-acceptance-against-statements.md));
   - a snapshot whose recorded price its price row no longer agrees with
     (`PORT-GIPS-A09`). `pt value` over that date values it again;
   - a day with no `return_policy` in force.

   A snapshot that used an estimate is accepted and disclosed
   (`PORT-GIPS-I10`).

8. **Net-of-fees first.** The ending value is already net of every cost paid,
   so a basis is derived by treating the fees it does not deduct as if they
   had been withdrawn on their day. Net-of-fees deducts every class except
   `other_admin`, which is therefore added back that way. Margin interest is
   never added back. Gross-of-fees and net-of-external-costs-only wait for
   [ADR 0036](0036-a-wrap-fee-is-a-bundled-fee.md), and are refused until it
   lands. Which classes a basis deducts is one function, beside the
   classifier, and never re-derived at a call site.

9. **The level is explicit and has no default**, as for `classify`. At
   portfolio level the day's value is the sum of its accounts' values, and its
   flow is the sum of their portfolio-level flows, so a transfer between two
   of the owner's accounts is no flow at all (`PORT-GIPS-B02`).

10. **Returns are computed from stored snapshots when asked for, not stored.**
    Determinism makes the recomputation byte-identical. The monthly series that
    `PORT-GIPS-F01` wants materialised is stored when the risk measures need
    it, as valued state under ADR 0024's rules.

11. **The engine is a service in `portable_core`**, and the `pert` command
    over it is thin. It is bound by the classification test
    (`tests/unit/test_flow_classification_is_not_rederived.py`), as all of
    `pert` is.

## Consequences

- A return can be computed only over a range that `pt value` has valued and
  that has no unresolved price break. That is by design, and the refusal says
  which command fixes it.
- Two conventions here are ones another system might choose differently:
  flows at the close (2), and dropping the first day's return when the day
  begins at zero (3). Both are disclosed on every report, so a reader
  comparing against a custodian's figure can see why the two differ.

## Alternatives considered

- **Flows at the start of the day.** Defensible, and equally consistent. It
  lets a deposit earn the day it arrives, before it could have been invested,
  and it values an in-kind transfer at a close it did not see. Rejected for
  that reason, not because the standard prefers either.
- **Half-day weighting**, which is Modified Dietz applied within the day.
  Rejected: it adds a convention for no gain when every day is valued.
- **Modified Dietz across a missing day.** Rejected: it would make a gap in
  the data look like a valuation.
