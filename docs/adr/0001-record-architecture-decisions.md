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
| [0012](0012-broker-import-pipeline.md) | Broker import is a staged pipeline with a human gate, not a command |
| [0013](0013-cash-sweep-is-cash.md) | A cash sweep vehicle is cash, not an instrument |
| [0014](0014-advisory-fees-paid-across-accounts.md) | Advisory fees paid by one account for another |
| [0015](0015-in-kind-transfers-and-opening-positions.md) | In-kind transfers, and how a position that predates the ledger enters it |
| [0016](0016-out-of-order-appends-and-validation.md) | Out-of-order appends force a rebuild; `pt validate` compares stored state against replay |
| [0017](0017-cutover-reconstruction-and-basis-provenance.md) | Cutover reconstruction by roll-back, and where each basis came from |
| [0018](0018-minimum-broker-dataset.md) | The minimum broker dataset, and capability-gated import |
| [0019](0019-migrations-that-rebuild-a-table.md) | A migration may rebuild a table, and what that costs |
| [0020](0020-historical-price-import.md) | Historical price import: requirements and open questions |
| [0021](0021-derived-state-as-of-a-past-date.md) | Derived state as of a past date |
| [0022](0022-amending-a-shipped-migration.md) | Amending a shipped migration, and the one it was needed for |
| [0023](0023-position-regroup-is-a-ledger-row.md) | A change of grouping is a ledger row |
| [0024](0024-valuation-snapshots-are-valued-state.md) | Valuation snapshots are valued state: kept by rebuild, removed when stale, checked by validate |
| [0025](0025-config-rows-dated-into-history.md) | A config row dated into history is refused, or restated on request |
| [0026](0026-dry-run-rehearses-the-write.md) | A dry run is the real write, rolled back |
| [0027](0027-position-label-is-a-ledger-row.md) | A position label is a ledger row |
| [0028](0028-instrument-with-no-market-price.md) | An instrument with no market price is valued at zero, at level 5 |
| [0029](0029-prices-through-duk.md) | Prices through `duk ph`; identity and corporate actions through `mart` |
| [0030](0030-default-benchmark-is-cash-at-zero.md) | The default benchmark is cash at 0%, labelled as such |
| [0031](0031-price-acceptance-against-statements.md) | Price history is accepted against the custodian's stated prices, to the cent |
| [0032](0032-fafnir-through-duk-alone.md) | fafnir through `duk` alone: identity, actions and calendar join prices |
| [0033](0033-identity-spells-the-account-as-the-portfolio-does.md) | A row's identity spells its account as the portfolio does |
| [0034](0034-a-wrong-symbol-is-corrected-not-renamed.md) | A wrong symbol is corrected, not renamed |
| [0035](0035-accrued-income-in-valuation-snapshots.md) | What a valuation snapshot accrues, and the bond it cannot yet value |
| [0036](0036-a-wrap-fee-is-a-bundled-fee.md) | A wrap fee is a bundled fee, deducted at every return basis |
| [0037](0037-return-engine-conventions.md) | The return engine's conventions: daily sub-periods, flows at the close, and what it refuses |
