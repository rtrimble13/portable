# ADR 0018 — The minimum broker dataset, and capability-gated import

- **Status:** Proposed — the capability model, the mapping files and the generic tabular adapter are implemented; the per-custodian post-pass of §4 and the `pt info` carry-forward are not
- **Date:** 2026-09-09
- **Milestone:** v0.2
- **Governs:** `CLAUDE.md` invariants 9, 10
- **Refines:** ADRs 0012, 0013, 0014, 0017 — which were derived from one custodian and are here separated into the general contract and the instance

## Context

ADRs 0012 through 0017 were written against three exports from one adviser. That
is a sound way to find the traps — every one of them came from real data rather
than from imagination — and an unsound way to fix an interface. A pipeline whose
required inputs are whatever the first custodian happened to supply works for
exactly one custodian.

Custodians differ enormously in what they will hand over. Some give a lot-detail
report; most do not. Some put a symbol on every transaction row; some give only
a description. Some assign a confirm number; some assign nothing. Some let you
pull history to account inception; many default to a two-year window and cannot
be persuaded past it.

The only safe assumption is the **intersection**, and the intersection is small.
Everything above it has to be optional, and — this is the part that is easy to
get wrong — its absence has to change what the resulting portfolio *claims*,
not merely what it contains.

`portable` already has the pattern. `providers/base.py` makes capabilities
separately declarable so that "a partial provider is legal and its gaps are
visible", and `FileProvider` declares a capability only where the caller actually
supplied a file, because declaring one with nothing behind it "would produce an
empty result that reads as 'no prices exist' rather than 'you did not tell me
where they are'". Import needs the same construction for the same reason.

## Decision

### 1. Two documents are required, and nothing else is

| Required input | Fields |
|---|---|
| **Holdings snapshot** | as-of date · account · instrument identifier · quantity · **cash balances** |
| **Transaction history** | date · account · activity · instrument · quantity · amount |

An import refuses without both. Each earns its place:

- The **holdings snapshot** is the reconciliation target and the anchor the
  cutover reconstruction rolls back from (ADR 0017). Without it there is nothing
  to check the ledger against, and reconciliation is the only thing standing
  between a plausible import and a correct one.
- The **transaction history** is the ledger. Nothing substitutes for it.
- **Cash on the snapshot** is required rather than optional because cash
  reconciliation is the only check that catches a sign error, a dropped row, or a
  double-counted transfer — the errors that leave quantities right and money
  wrong. Every custodian states a cash balance on a position statement.

Everything else — cost basis, acquisition dates, lot detail, transaction
identifiers, symbols, settlement dates, a separate flows report, history reaching
inception — is **optional**.

### 2. Optional inputs are capabilities, declared by the adapter

An `ImportCapability` enum, mirroring `providers.Capability`. The adapter
declares what its sources support; the pipeline computes what the resulting
portfolio can claim; anything asked of it beyond that is refused **by name**.

| Capability | Absent means |
|---|---|
| `COST_BASIS` | Every seeded lot is `basis_source = 'unavailable'`. The portfolio still builds and performance is unaffected; `pt tax` cannot report a realized gain on any pre-cutover lot. |
| `ACQUISITION_DATE` | Seeded lots are dated at the cutover. Holding-period character is then **conservative by construction** — everything seeded reads short-term until a year past the cutover — which is the safe direction to be wrong in. |
| `LOT_DETAIL` | No specific identification within a seeded block. `pt sell --lots` can name the block, not shares inside it. |
| `TRANSACTION_ID` | `external_ref` is synthesized from row content (ADR 0012). Re-import is safe only while the custodian's export is stable row-for-row. |
| `INSTRUMENT_SYMBOL` | A name-to-symbol crosswalk is required and any unmapped name is a refusal. |
| `SETTLEMENT_DATE` | Settlement is not recorded. No effect on recognition — `portable` is trade-date accounting (invariant 7). |
| `CORPORATE_ACTIONS` | Splits and reorganisations are absent from the history, so rolled-back quantities will not reconcile. Detected rather than assumed: the reconstruction's own check fails and names the instrument. |
| `EXTERNAL_FLOWS` | Contributions and withdrawals must be identified from the activity map alone, with no second document to cross-check against. A misclassified deposit rewrites the track record silently (`PORT-GIPS-B02`), so the map is reviewed with that specifically in mind. |
| `HISTORY_TO_INCEPTION` | A cutover is required and ADR 0017 applies in full. |

### 3. A capability is declared on validated data, not on a present column

This is the rule that makes the mechanism worth having. The reference custodian's
export **has** a settlement date column in which most populated cells hold a date
earlier than their own trade date. A column is not a capability.

An adapter declares a capability only after checking the data behind it, and the
check is part of the adapter's tests. Where a column exists but fails its check,
the capability is **not** declared and the reason is recorded in the import
report — which is strictly better than the column being absent, because the user
learns their custodian's export is broken rather than assuming it is unavailable.

### 4. Adapters are mapping declarations first, code only where forced

Two data files per custodian, under `src/portable_core/importers/<name>/`:

- **`source.toml`** — which file is which, the column map per document, date and
  number formats, the cash-equivalent identifier set for that account (ADR 0013),
  and the declared capabilities with the checks that justify them.
- **`activity_map.toml`** — every activity string, its `TransactionType`, its
  effect on quantity and on cash, its `fee_class` where it is a fee, and its sign
  convention. No default arm.

A custodian whose exports are plain tabular files needs **no Python at all** —
two mapping files and a fixture. The generic tabular adapter reads the files, and
that is the common case.

Python is written only where a custodian's data needs genuine logic that a
mapping cannot express: the cross-account fee settlement of ADR 0014, a split
ratio embedded in prose, a reorganisation whose outgoing side is missing. Those
live in a per-custodian module that the generic adapter calls as a post-pass,
and each is a documented deviation rather than the norm.

### 5. Everything downstream sees canonical records only

Adapters emit `HoldingRecord` and `TransactionRecord`. The cutover
reconstruction, the batch builder, the reconciler, and every refusal operate on
those and have no knowledge of spreadsheets, custodians, or column names. This is
what makes the reconstruction in ADR 0017 a general procedure rather than a
description of one adviser's data.

## Consequences

- ADRs 0013, 0014, and 0017 split into a general rule and an instance. Sweep
  handling generalises to "a declared set of cash-equivalent identifiers per
  account" rather than two named tickers. Cross-account fee settlement becomes a
  declarative pairing rule that most custodians will not need. The reconstruction
  is unchanged and was already general.
- `pt import` reports the declared capability set before doing anything, so the
  user sees what their custodian supports and what the resulting portfolio will
  therefore be unable to claim. That report is the first thing to read and the
  thing to keep.
- `pt info` carries the capability set forward, because a portfolio built without
  `COST_BASIS` is permanently different from one built with it and a reader
  months later needs to know which they have.
- Adding a custodian is a fixture plus two TOML files plus a reconciliation. That
  is the test of whether this ADR worked, and the second adapter is what proves
  it — the first one always fits.
- The reference custodian declares four of the nine capabilities. That is a
  useful number: an interface justified only by a source that supports everything
  would not have been exercised.

## Alternatives considered

- **Require what the first custodian supplies.** Rejected: it is not a contract,
  it is a description, and it silently becomes a requirement.
- **Accept any subset and degrade quietly.** Rejected by invariants 9 and 10.
  Degrading quietly means a portfolio missing cost basis reports realized gains
  of zero rather than refusing, which is the landmine invariant 10 describes,
  with money attached.
- **Require only the transaction history, and derive holdings from it.** Rejected:
  it removes the reconciliation anchor. A ledger that has never been checked
  against an independently produced position statement is an assertion, and every
  acceptance criterion in `docs/broker-import.md` §8 rests on having one.
- **A per-custodian Python adapter as the norm, with mapping files as a
  convenience.** Rejected: it puts the highest-risk part of the work — the
  activity vocabulary — in the least reviewable place, and makes adding a
  custodian a programming task rather than a data task.
