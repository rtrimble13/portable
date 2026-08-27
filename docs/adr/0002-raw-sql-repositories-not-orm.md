# ADR 0002 — Raw SQL behind repositories, not an ORM

- **Status:** Accepted
- **Date:** 2026-08-27
- **Milestone:** v0.1

## Context

The `.port` file is a SQLite database that must satisfy four constraints an ORM
does not naturally serve:

1. **No binary floating point** anywhere in a money path (`CLAUDE.md` invariant 1).
   Money is canonical decimal `TEXT`, not `REAL` and not `NUMERIC`.
2. **The ledger is append-only**, enforced by database triggers that reject
   `UPDATE` and `DELETE` on `transaction` (invariant 2). An ORM's unit-of-work
   pattern exists to issue exactly the statements we forbid.
3. **`docs/schema.md` is generated from DDL comments** so it cannot drift
   (bootstrap §5). That requires the DDL to be the source of truth, in SQL files.
4. **Migrations are numbered, idempotent, and tested in both directions.**

## Decision

Write the schema as versioned SQL files in `src/portable_core/schema/`, and access
it through hand-written repository classes in `src/portable_core/persistence/`
using `sqlite3` from the standard library.

- SQL appears **only** in `persistence/` and `schema/`. Nowhere else. There is a
  test that greps for it.
- Repositories return domain objects (ADR 0003), never rows or cursors.
- `sqlite3` connections are configured in exactly one place
  (`persistence/connection.py`): `foreign_keys=ON`, `journal_mode=WAL`,
  `synchronous=FULL`, and the `Decimal` adapter/converter pair from ADR 0005.

## Consequences

- More code than an ORM, all of it explicit and all of it reviewable.
- No lazy-loading surprises, no N+1 that we did not write ourselves, no dialect
  translation layer between us and a `CHECK` constraint.
- Query plans are ours. The indexes in the DDL are chosen for the queries `pt
  query`, `pt holdings`, and `ValuationEngine` actually run.
- `pt query --sql` becomes a natural read-only escape hatch rather than a foreign
  concept bolted onto an object graph.
- The cost lands on us: mapping rows to objects by hand in ~20 repositories.

## Alternatives considered

- **SQLAlchemy Core** (not the ORM) — genuinely tempting: it keeps SQL explicit
  while handling parameter binding and reflection. Rejected because it adds a
  substantial dependency for a single-file, single-writer, single-dialect
  database, and because its type system wants to own the `Decimal` boundary that
  ADR 0005 deliberately keeps in one hand-written place.
- **SQLAlchemy ORM / SQLModel** — rejected on constraints 1 and 2 above.
- **`sqlite3` with `Row` factory and no repositories** — rejected: business logic
  would leak into call sites, which `CLAUDE.md`'s placement rules forbid.
