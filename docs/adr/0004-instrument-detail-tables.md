# ADR 0004 — Instrument subtype detail tables, not a JSON detail column

- **Status:** Accepted
- **Date:** 2026-08-27
- **Milestone:** v0.1

## Context

Bootstrap §4.3: "Use a base `instrument` table plus type-specific detail tables
(or a validated JSON detail column — choose one, justify it in an ADR, and be
consistent)."

v0.1 supports equity/ETF/fund/ADR, cash and money market, listed options
(underlier, right, strike, expiry, multiplier, OCC symbol, exercise style), and
fixed income (issuer, coupon, frequency, maturity, day-count, face, callable).

## Decision

A base `instrument` table plus `instrument_option` and `instrument_bond` detail
tables, joined 1:1 on `instrument_id`.

## Rationale

1. **`NOT NULL` actually means something.** An option without a strike is not an
   option. In a JSON column that is a runtime check somebody has to remember to
   write; in a detail table it is a constraint the database enforces against
   every writer, including `pt import` and any future broker adapter.
2. **`CHECK` constraints enforce the enumerations** — `right IN ('call','put')`,
   `day_count IN ('30/360','ACT/ACT','ACT/365','ACT/360')`, `exercise_style IN
   ('american','european')`.
3. **The queries are real.** "All options expiring this month", "all bonds with a
   coupon date in the period" are things `ValuationEngine` and `pt holdings`
   run every day. `json_extract` in a `WHERE` clause is unindexable in the way we
   need; a typed column with an index is not.
4. **`docs/schema.md` is generated from DDL comments** (ADR 0002). A JSON blob
   documents as one line saying "detail"; the real contract would live in prose
   nobody regenerates.
5. **The multiplier trap.** `CLAUDE.md` requires the option multiplier be stored,
   not assumed to be 100. A `NOT NULL` column with no default makes forgetting it
   impossible. A JSON key makes forgetting it silent — and a missing multiplier
   is a 100× wrong number.

## Consequences

- Adding an instrument type means a migration. That is the right cost: an
  instrument type is a schema-level concept, and the migration is where the
  round-trip test and the `schema_version` bump get forced.
- Repositories do a `LEFT JOIN` per subtype. `InstrumentRepository` hides this.
- `instrument.instrument_type` and the presence of the detail row must agree; a
  `CHECK`-plus-trigger pair enforces it, and `pt validate` asserts it.

## Alternatives considered

- **Validated JSON detail column** — fewer tables, schema-less extension, and
  genuinely better if the subtypes were open-ended. They are not: this is a
  closed, small, slow-moving set defined by market convention. Rejected on
  points 1, 2, 3, and 5.
- **Single wide table with nullable columns** — no join, but every option column
  is nullable for every equity, and the "an option must have a strike" constraint
  becomes a table-wide `CHECK` expression that grows quadratically with types.
