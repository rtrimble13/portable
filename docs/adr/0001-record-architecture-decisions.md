# ADR 0001 — Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-08-27
- **Milestone:** v0.1

## Context

`portable` computes numbers its owner files taxes from and defends investment
decisions with. The bootstrap prompt (§13) leaves a number of material choices to
the implementer and asks that each be decided, written down, and surfaced.
`CLAUDE.md` makes checking `docs/adr/` a precondition for redesigning anything.

A decision whose reasoning is not written down gets silently reverted by the next
agent session, and in this domain a silent reversal is a silently wrong number.

## Decision

Every decision that (a) was left open by the bootstrap prompt, (b) deviates from
it, or (c) would be expensive to reverse, gets an ADR in `docs/adr/`, numbered
sequentially and never renumbered.

Format: Context / Decision / Consequences / Alternatives considered. One page.
Status is one of `Proposed`, `Accepted`, `Superseded by ADR-xxxx`, `Rejected`.

A change to a `PORT-GIPS-xxx` obligation in `docs/gips-standard.md` requires an
ADR (that document's §13), and `PORT-GIPS-xxx` identifiers are never reused.

## Consequences

- The ADR index below is the map of "why is it like this".
- Disagreeing with a decision means writing the next ADR, not quietly changing code.

## Index

| ADR | Decision |
|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions |
| [0002](0002-raw-sql-repositories-not-orm.md) | Raw SQL behind repositories, not an ORM |
| [0003](0003-frozen-dataclasses-not-pydantic.md) | Frozen dataclasses for domain objects, not Pydantic |
| [0004](0004-instrument-detail-tables.md) | Instrument subtype detail tables, not a JSON detail column |
| [0005](0005-decimal-representation-and-rounding.md) | Decimal representation, storage, and rounding policy |
| [0006](0006-fafnir-access-path.md) | fafnir access path: direct warehouse SQL, `duk` for the yield curve |
| [0007](0007-cash-flow-classification.md) | Cash-flow classification is one level-aware service function |
| [0008](0008-cpp-integration-and-fallback.md) | C++ integration: optional extension, mandatory Python reference |
| [0009](0009-position-spans-instruments.md) | A position spans instruments; lots hang off legs |
| [0010](0010-derived-state-and-replay.md) | Derived state, the replay contract, and what `pt rebuild` guarantees |
| [0011](0011-tax-estimation-boundary.md) | What the tax engine estimates and what it refuses |
