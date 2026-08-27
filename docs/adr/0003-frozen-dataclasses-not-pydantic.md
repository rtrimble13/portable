# ADR 0003 — Frozen dataclasses for domain objects, not Pydantic

- **Status:** Accepted
- **Date:** 2026-08-27
- **Milestone:** v0.1

## Context

The bootstrap prompt (§6.1) says "dataclasses or Pydantic — pick one, justify, be
consistent". Domain objects carry `Decimal` money fields and `date` fields, have
no I/O, and are constructed both from database rows and from CLI input.

## Decision

**`@dataclass(frozen=True, slots=True)` from the standard library**, with
`typing` annotations and mypy strict on `portable_core`.

Validation is not absent — it is **relocated to the boundaries**:

- **CLI boundary** — Typer parses and `portable_core/cli/params.py` converts
  strings to `Decimal`/`date`, raising `PortableError` subclasses with stable
  codes on bad input.
- **Persistence boundary** — the schema's `CHECK` constraints and `NOT NULL`s are
  the enforcement of record, because they bind data written by any path
  (including `pt import` and a future broker importer).
- **Service boundary** — invariant assertions in the engines, property-tested.

Domain objects additionally run a `__post_init__` guard that rejects `float` in
any money field. That guard is cheap and catches the one mistake this repo fears.

## Rationale

1. **Pydantic coerces to `float` too eagerly for comfort.** `Decimal` handling is
   configurable and correct in Pydantic v2, but the default behaviour of numeric
   coercion — and the ease with which a future contributor adds a `float` field
   without noticing — is precisely the failure mode `CLAUDE.md` invariant 1
   exists to prevent. A `frozen` dataclass with an explicit no-float guard makes
   the money boundary a thing we wrote rather than a thing we configured.
2. **Immutability matches the domain.** A `Transaction` is a ledger event; it
   cannot change. A `Lot` changes only through the `LotEngine`, which returns a
   new `Lot`. `frozen=True` makes that structural.
3. **Determinism.** No validator ordering, no model config inheritance, no
   version-dependent serialisation. `CLAUDE.md` invariant 6 wants byte-identical
   output; the fewer libraries between a `Decimal` and its string, the better.
4. **Zero dependency, mypy-strict-clean, `slots=True` keeps them cheap** — the
   replay path builds hundreds of thousands of these.

## Consequences

- We write the row→object mapping ourselves (already implied by ADR 0002).
- JSON Schema for command output is authored in `schemas/` and validated in
  tests, rather than generated from models. This is what the bootstrap asks for
  anyway (§6.6) — the schema is a published contract, so it should be written
  deliberately and versioned, not derived from whatever the models happen to be.
- No free `.model_dump()`. Serialisation lives in `formatters/`, which is where
  `CLAUDE.md` says presentation rules belong.

## Alternatives considered

- **Pydantic v2 with `Decimal` and `strict=True`** — workable, and its error
  messages are better than ours. Rejected on points 1, 3, and 4, and because the
  validation it would give us at the *domain* layer is validation we need at the
  *boundary* layer regardless.
- **`attrs`** — same shape as dataclasses with more features; not worth a
  dependency when `slots=True` landed in stdlib dataclasses in 3.10.
