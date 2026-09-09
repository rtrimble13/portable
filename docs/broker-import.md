# Broker import

How a custodian's exports become ledger rows: what `portable` requires of any
custodian, what it does with more when more is available, and what it refuses to
guess.

**Status:** partly built. The prerequisites, the batch format and its commit
stage, and the generic tabular adapter with its capability model are
implemented; the extract stage that turns canonical records into a batch, and
the cutover reconstruction it depends on, are not. §1 says which command is
which. The decisions are ADRs
[0012](adr/0012-broker-import-pipeline.md) (the pipeline),
[0013](adr/0013-cash-sweep-is-cash.md) (sweep vehicles),
[0014](adr/0014-advisory-fees-paid-across-accounts.md) (adviser fees),
[0015](adr/0015-in-kind-transfers-and-opening-positions.md) (in-kind transfers),
[0017](adr/0017-cutover-reconstruction-and-basis-provenance.md) (reconstructing
the opening state, and basis provenance), and
[0018](adr/0018-minimum-broker-dataset.md) (the required dataset and the
capability model). [ADR 0016](adr/0016-out-of-order-appends-and-validation.md)
fixes a replay defect that blocks all of it. Milestone `v0.2`.

**This document is custodian-neutral.** §12 works one real custodian through it
end to end; everything before §12 holds for any of them.

---

## 1. The shape

```
custodian export ──[extract]──▶ batch file ──[review]──▶ ──[commit]──▶ ledger
   .xlsx / .csv                 .json, diffable         validate      + rebuild
```

```bash
pt import inspect adapters/<name>/       # what this custodian can support
pt import broker adapters/<name>/ -o batch.json
$EDITOR batch.json                       # the review is the point
pt import batch batch.json --dry-run
pt import batch batch.json
pt reconcile --account <acct> --against holdings.csv --as-of <date>
```

**Implemented so far:** `pt import inspect` (the adapter and the capability
report) and `pt import batch` (validate and commit). `pt import broker` — the
step that turns canonical records into a batch — needs the cutover
reconstruction of §7 and is the next piece of work; until it lands, `inspect`
reads and reports and nothing writes a batch.

Three commands rather than one, deliberately. The ledger is append-only: a wrong
row is corrected with a reversing entry that stays visible for the life of the
portfolio, so the cheap place to catch a mistake is the batch file, where fixing
one costs a text edit.

The batch carries every row the adapter produced **and** every row it
deliberately discarded, each with its source row verbatim and the rule that
handled it. A silently dropped row and a deliberately dropped row are the same
absence in the ledger and completely different claims about the import.

---

## 2. What every import requires

Two documents. Nothing else is mandatory ([ADR 0018](adr/0018-minimum-broker-dataset.md)).

| Required | Fields |
|---|---|
| **Holdings snapshot** | as-of date · account · instrument identifier · quantity · **cash balances** |
| **Transaction history** | date · account · activity · instrument · quantity · amount |

The holdings snapshot is the reconciliation target and the anchor the cutover
reconstruction rolls back from (§7). The transaction history is the ledger.
Cash is required rather than optional because cash reconciliation is the only
check that catches a sign error, a dropped row, or a double-counted transfer —
the errors that leave every quantity right and the money wrong.

Any custodian can produce both. If yours cannot, the problem is upstream of
`portable`.

---

## 3. What more buys you

Everything beyond §2 is optional and **declared** by the adapter. The pipeline
computes what the resulting portfolio can claim, and refuses anything beyond it
by naming the missing input rather than degrading quietly.

| Capability | Absent means |
|---|---|
| `COST_BASIS` | Every seeded lot is `basis_source = 'unavailable'`. The portfolio builds and performance is unaffected; `pt tax` cannot report a realized gain on a pre-cutover lot. |
| `ACQUISITION_DATE` | Seeded lots are dated at the cutover, so holding-period character is conservative by construction — everything seeded reads short-term until a year past it. |
| `LOT_DETAIL` | No specific identification inside a seeded block. `pt sell --lots` names the block, not shares within it. |
| `TRANSACTION_ID` | `external_ref` is synthesized from row content (§5). Re-import is safe only while the export is stable row-for-row. |
| `INSTRUMENT_SYMBOL` | A name-to-symbol crosswalk is required; an unmapped name is a refusal. |
| `SETTLEMENT_DATE` | Settlement is not recorded. No effect on recognition — `portable` is trade-date accounting. |
| `CORPORATE_ACTIONS` | Splits and reorganisations are missing from the history, so rolled-back quantities will not reconcile. Detected, not assumed: §7's check fails and names the instrument. |
| `EXTERNAL_FLOWS` | Contributions and withdrawals rest on the activity map alone, with no second document to cross-check. A misclassified deposit rewrites the track record silently (`PORT-GIPS-B02`). |
| `HISTORY_TO_INCEPTION` | A cutover is required and §7 applies in full. |

**A column is not a capability.** An adapter declares one only after validating
the data behind it, and the check is part of its tests. A custodian that emits a
settlement-date column full of impossible dates has not supplied settlement
dates — and saying so in the import report is better than silence, because the
user learns their export is broken rather than assuming the field is unavailable.

`pt import inspect` prints the declared set before doing anything — it is the
first command to run against a new custodian and the one to read before trusting
any number that comes out later — and `pt info` carries it forward. A portfolio
built without `COST_BASIS` is permanently different from one built with it, and a
reader months later needs to know which they have.

---

## 4. Canonical records

Adapters emit two record types and everything downstream sees only these — the
reconstruction, the batch builder, the reconciler, and every refusal have no
knowledge of spreadsheets, custodians, or column names.

```python
@dataclass(frozen=True, slots=True)
class HoldingRecord:
    as_of: date
    account: str
    identifier: str          # symbol, CUSIP, ISIN, or a name to be crosswalked
    quantity: Decimal
    is_cash_equivalent: bool
    market_value: Decimal | None = None
    cost_basis: Decimal | None = None      # COST_BASIS
    acquired: date | None = None           # ACQUISITION_DATE
    lot_id: str | None = None              # LOT_DETAIL

@dataclass(frozen=True, slots=True)
class TransactionRecord:
    trade_date: date
    account: str
    activity: str            # the custodian's own string, unmapped
    identifier: str | None
    quantity: Decimal | None
    amount: Decimal
    source_row: Mapping[str, str]          # verbatim, for the batch file
    external_id: str | None = None         # TRANSACTION_ID
    settlement_date: date | None = None    # SETTLEMENT_DATE
    note: str | None = None
```

`activity` stays the custodian's raw string. Mapping it is the activity map's
job, done once, where it can be reviewed.

Two conventions are fixed at this boundary rather than left to each adapter,
because leaving them open is how a sign error gets in. `amount` is the **cash
effect on the account** — positive in, negative out — and `quantity` is the
**signed change in units**, `None` where the event has none. Note what `amount`
is not: it is what the statement says the cash did, not what `portable`
concludes follows from the event. Deriving the consequences stays with the
services (ADR 0012).

---

## 5. The batch format

`schemas/import-batch-1.0.json` — **published and implemented**. Unlike every
other schema in that directory it describes an *input*, so it does not extend
the output envelope. `pt import batch` reads it.

One object per prospective ledger row:

```json
{
  "format": "portable-import-batch",
  "format_version": 1,
  "source": {
    "broker": "example",
    "capabilities": ["COST_BASIS", "ACQUISITION_DATE", "CORPORATE_ACTIONS"],
    "files": [{"name": "activity.xlsx", "sha256": "…"}],
    "period": {"from": "2024-09-25", "to": "2026-08-25"}
  },
  "rows": [
    {
      "action": "append",
      "external_ref": "example:9f2c1a04d3e88b71",
      "account": "Brokerage",
      "txn_type": "sell",
      "trade_date": "2026-01-15",
      "symbol": "EXMPL",
      "quantity": "30",
      "price": "100.00",
      "gross_amount": "3000.00",
      "fees": "0.00",
      "fee_class": null,
      "rule": "activity:Sell",
      "source_row": {"Date": "01/15/2026", "Type": "Sell", "…": "…"}
    },
    {
      "action": "drop",
      "rule": "sweep:cash-equivalent-transfer (ADR 0013)",
      "source_row": {"Type": "MoneyTransfer", "…": "…"}
    }
  ]
}
```

Rows are ordered by `(trade_date, source ordinal)` so re-extracting after a map
change produces a clean diff. Money and quantities are strings, never JSON
numbers (ADR 0005). The declared capability set travels with the batch, because
what the rows mean depends on it.

The format is the interface: a future OFX adapter, or a batch hand-written for a
handful of corrections, is a first-class input on the same footing.

**A row states what happened, not what follows from it.** There is no
`net_cash_effect` in a batch: `portable` derives the cash effect, the lot relief
and the tax through the same services a typed command uses, so every refusal
that guards hand entry guards an import too rather than an importer growing a
second, laxer path into the ledger.

**Format version 1 carries trades, cash and income** — the types with a service
behind them. Corporate actions and the options lifecycle are refused by name
rather than half-supported: they need position context a typed command gathers
interactively, and an importer deriving basis by a second, unreviewed route is
the failure that avoids. ADR 0018 puts them in a per-custodian post-pass.

**`--dry-run` is the real commit, rolled back.** Validating each row against the
state *before* the batch would refuse a batch that commits perfectly well, since
a sale's lot relief has to see the purchase earlier in the same batch. Running
it for real and discarding the result is the only dry run that answers the
question asked — the same pattern ADR 0016 established for `pt validate`.

**The source documents are hash-checked** where they can be found next to the
batch: a review approves particular rows against a particular export, and if the
export has since been re-downloaded the review no longer covers what is about to
be committed. A file that cannot be found is *reported* rather than refused,
because a batch is often reviewed elsewhere — but reported, so that "verified"
and "not checked" stay distinct.

The published schema is validated in CI, and a test asserts that anything the
runtime loader accepts also validates against it. The loader checks by hand
because `jsonschema` is a development dependency and a hand-written check can
name the row index, the field, and the remedy — which a batch under human review
needs.

**Re-importing an overlapping period** is ordinary, not an error: you pull
Jan–Jun, then Apr–Dec. That is resolved at **extract**, where rows already in
the ledger are written to the batch as `action: "skip"` with the reason, so the
overlap is visible in the artifact you review. It is deliberately *not* a
`--skip-duplicates` flag at commit: a commit-time skip makes the decision
invisible, and cannot be told apart from an adapter that failed to emit the row.
A duplicate reaching commit is therefore unexpected, and refuses.

**Identity.** Where the custodian supplies `TRANSACTION_ID`, that is the
`external_ref`. Where it does not, `external_ref` is `sha256` over the source
row's raw text plus an ordinal distinguishing otherwise-identical rows in the
same export. Components are the *source* text, not mapped values, so revising the
activity map never changes the identity of a row already committed.
`UNIQUE (account_id, external_ref)` enforces it; a collision is a refusal, never
a silent skip.

---

## 6. Writing an adapter

**Implemented.** `portable_core.importers.TabularAdapter` reads the two files
below and emits the canonical records of §4; `pt import inspect <directory>`
runs it and reports. A worked example lives in
`examples/importers/example-brokerage/` — copy the directory, change the
mappings, run `pt import inspect` against your own export.

Two data files, reviewed as data (a built-in custodian lives under
`src/portable_core/importers/<name>/`; your own can live anywhere):

**`source.toml`** — which file is which, the column map per document, date and
number formats, the account's cash-equivalent identifier set (ADR 0013), and the
declared capabilities with the checks that justify each.

**`activity_map.toml`** — every activity string the custodian emits, with its
`TransactionType`, its effect on quantity and cash, its `fee_class` where it is a
fee, and whether the amount's sign is authoritative or derived. No default arm:
an unmapped string stops the import and names the row.

A custodian whose exports are plain tabular files needs **no Python** — two
mapping files and a fixture. That is the common case and the point of the design.

Python is written only where the data needs logic a mapping cannot express: a
split ratio embedded in prose, a reorganisation whose outgoing side is missing
from the file, a fee settled from a different account. Those live in a
per-custodian post-pass and each is a documented deviation, not the norm.

A crosswalk file (`instruments.toml`) is additionally required where
`INSTRUMENT_SYMBOL` is absent.

### The checks a capability can name

A capability names one of six checks, which run over the parsed rows. The list
is deliberately short and each entry is deliberately dumb: a check clever enough
to be interesting is a check nobody can review, and reviewing these is the
entire safeguard.

| Check | Passes when | Typically earns |
|---|---|---|
| `populated` | the field carries a value in at least `min_ratio` of rows (default: all) | `cost_basis`, `acquisition_date`, `lot_detail` |
| `unique` | the field is populated everywhere and never repeats | `transaction_id` |
| `matches` | every value matches a declared regex — how a symbol column is told from a column of descriptions | `instrument_symbol` |
| `not_before_trade_date` | no row's date precedes its own trade date | `settlement_date` |
| `history_since` | the earliest row is on or before a stated inception date | `history_to_inception` |
| `activity_covers` | the activity map names a rule producing each listed transaction type | `corporate_actions`, `external_flows` |

**A withheld capability's data is not read.** If `settlement_date` fails its
check the records carry no settlement dates — not the dates that failed. A
capability that merely labels data which flows through anyway is a comment, not
a safeguard.

### Sign conventions

Custodians are not consistent even with themselves: some sign the amount column,
some state a magnitude and put the direction in the activity string, some use
accounting parentheses on one report and a minus on another. `activity_map.toml`
declares which, per activity, and the adapter normalises to the canonical
convention — positive is in, negative is out.

| Convention | Means |
|---|---|
| `as_stated` | the column's own sign is authoritative; use only where it was checked |
| `positive` | a magnitude that always means an increase for this activity |
| `negative` | a magnitude that always means a decrease for this activity |
| `none` | this activity carries no value in that column at all — a stated absence, not a zero |

### What is refused, and when

Everything wrong with a *mapping* is refused when the file loads, because a map
is reviewed once and used for every row after: an unmapped activity string, two
rules for one string, a fee with no `fee_class`, a skip with no reason, an
unknown transaction type or check name, a capability declared twice, a date
pattern that is invalid *or that carries only part of a date* (`%Y-%m` parses
happily and silently returns the first of the month).

Everything wrong with the *data* is refused with the row quoted: a mapped column
the file does not have (the error lists the headers it does have), a cell that is
not a number in the declared format, a date matching no declared pattern, a blank
where the map expects a value, a snapshot carrying two as-of dates, and a
snapshot stating no cash for an account — which is refusable rather than
tolerable because cash reconciliation is the only check that catches a sign
error or a dropped row. An account whose custodian genuinely reports no cash
line is listed in `allow_missing_cash`, so the exception is on the record.

Spreadsheets are refused by name with the remedy. `portable`'s runtime
dependencies are Typer and Rich; adding a workbook parser for a file the
custodian will also emit as CSV is a large dependency for no capability
(`CLAUDE.md` invariant 10).

---

## 7. The cutover, and the reconstruction

Where `HISTORY_TO_INCEPTION` is absent — the common case, since most custodians
default to a two-year window — the portfolio's reporting inception is the
transaction history's first date, not the date the accounts opened
([ADR 0017](adr/0017-cutover-reconstruction-and-basis-provenance.md)).

**The opening position set is derived, not read.** Apply the transaction history
**in reverse** to the holdings snapshot to obtain the holding of every instrument
on the day before the ledger begins. Each becomes a `transfer_in` (ADR 0015).

The roll-back is also the completeness check. A position that rolls back to a
negative holding proves the history is missing something — most often a corporate
action, which is what makes `CORPORATE_ACTIONS` detectable rather than something
to take on trust.

**Basis at the cutover**, in descending order of what the evidence supports:

| | `basis_source` |
|---|---|
| Position untouched since the cutover: basis today less every subsequent addition | `reconstructed` |
| Block partly survives: solved backwards under the account's assumed relief method | `estimated` |
| Nothing of the block survives — sold out, or fully consumed | `unavailable` |
| Read from a lot-detail report, where `LOT_DETAIL` is present | `custodian_asserted` |

The third row is the one to understand. The solve anchors to the custodian's
stated *present* basis, so a block that contributes nothing to the present
holding has no anchor and no equation — under any relief method. Those lots are
seeded at cutover market value so the arithmetic closes (invariant 4 needs a
basis to balance against), and **that value is not a basis claim**: `pt tax`
excludes those dispositions from every total, reports them separately, and marks
the year incomplete rather than printing a gain measured from an arbitrary date.

What survives the reconstruction intact: current holdings and current basis
(exact, anchored to the custodian), every future tax-aware decision, and all
cash, income, fees, and flows after the cutover.

---

## 8. Refusals

Per invariant 9, the import stops and explains rather than guessing, on:

- either required document missing, or a required field absent from it;
- an unmapped activity string;
- an unresolvable or ambiguous instrument identifier;
- a fee whose `fee_class` the activity map does not determine (`PORT-GIPS-D01`);
- a duplicate `external_ref`;
- a settlement date earlier than its trade date;
- a roll-back that produces a negative holding;
- a closing quantity exceeding open lots;
- a fractional quantity in an account without `allows_fractional`;
- a trade dated before the account's `opened_date`;
- a batch whose stated file hash does not match the file on disk;
- a request for an output the declared capabilities do not support.

---

## 9. Acceptance

An import is accepted when it reconciles, not when it parses.

1. **Per period, per account:** ending cash and every position quantity match
   the custodian. `pt reconcile --against <statement.csv>`; a break exits **6**.
   The statement needs `quantity` plus one of `symbol`, `cusip` or `isin`, an
   `account` column once more than one account is in scope, and a `cash` column
   marking the cash line and any sweep vehicle.
2. **Per closed tax year:** realized gains tie to the custodian's tax reporting,
   excluding dispositions marked `unavailable`.
3. `pt validate` passes — which, after ADR 0016, means stored derived state
   actually equals replayed state.
4. `pt export` → `pt import` → `pt export` is byte-identical.

Check 1 carries extra weight, and cash carries most of that. A sign error, a
dropped row, and a double-counted transfer all leave every share count correct
and the money wrong, so a quantity-only comparison passes on all three.

The reconstruction also works backwards from the
custodian's stated present position, so agreement there is not a coincidence —
it is the arithmetic closing. What the check proves is that the transaction
history is complete enough to bridge the two ends, and that is the whole of the
evidence the reconstruction rests on. Run it per account and per period, not once
at the end.

---

## 10. Gaps in `portable` this depends on

**Closed.**

- ~~`taxes_withheld` has no write path.~~ `TradingService.record_income` sets it,
  with `--withheld` and `--reclaimable` on `pt income dividend` and `coupon`.
  `gross_amount` stays the income and `net_cash_effect` is what landed, because
  a report needs both; the reclaimable portion is stored separately, since
  reclaimable is accrued and non-reclaimable reduces return (`PORT-GIPS-A06`).
  A split that cannot be true is refused with `PT-E-WITHHOLDING-INVALID`.
- ~~`--ref` is exposed on trades, deposit, and withdraw only.~~ All twenty
  ledger-writing commands take one, defined once as `RefOpt` in
  `commands/_shared.py`. Each writes at most one row per account per
  invocation, so one `--ref` stays unambiguous under the uniqueness constraint
  below.
- ~~`TradingService` hardcodes `source = MANUAL`.~~ `TradeIntent.source`,
  `record_cash(source=)` and `record_income(source=)` carry it; the CLI still
  defaults to `manual`, and `pt trade show` reports it (`PORT-GIPS-J03`).
- ~~`pt reconcile` compares quantities only.~~ It now compares **per account**
  and includes **cash**, resolves an identifier by symbol, CUSIP or ISIN, and
  folds the custodian's sweep positions into its cash line (ADR 0013). The
  comparison lives in `services/reconciliation.py`; the command parses and
  renders. It refuses rather than guessing when a statement line cannot be
  attributed to an account, when a statement names an account not being
  reconciled, and when cash is stated both by `--cash` and by a line in the
  file.

- ~~No uniqueness constraint on `external_ref`.~~ Migration 0002 adds
  `UNIQUE (account_id, external_ref)` where a reference is present. Scoped per
  account because `pt ca split --ref X` legitimately writes one row per holding
  account under one reference; partial because a row with no reference is the
  ordinary hand-entered case. `TransactionRepository.append` refuses a duplicate
  with `PT-E-DUPLICATE-REF` naming the colliding transaction, so every writer is
  covered including the corporate-action and options commands that build rows
  directly. `pt import` scans an export's ledger before its first insert.

**Open.**

- **Fund capital-gain distributions have no transaction type.** Income for flow
  purposes, taxed by character.
- **Wash sales are not detected** until `v0.2`; `pt tax` says so on its face.

---

## 11. Runbook

1. `pt init`; `pt account add` per account with its true `opened_date`, `--type`,
   relief method, and `--allows-fractional` where funds are held.
2. Tax rate schedules on taxable accounts, and a `return_policy`
   (`PORT-GIPS-B03`).
3. Extract with `--dry-run` first and **read the declared capability set**. It
   tells you what this portfolio will and will not be able to claim.
4. Run the cutover reconstruction; review its exceptions and any dispositions
   falling within a year of the cutover, then seed each opening position as a
   `transfer_in` with its `basis_source`.
5. Import the transaction history, oldest period first, one account at a time.
6. `pt reconcile` per account. Do not proceed past a break.
7. Backfill prices, then `pt value` across the period. Watch for snapshots marked
   incomplete: a position that cannot be priced makes the return unanswerable
   rather than approximate.
8. `pt validate`, `pt rebuild`, `pt validate` again.

Step 7 stalls most often. A complete daily valuation history needs prices for
every instrument ever held, including under symbols that no longer exist. Because
portfolio accounting uses **unadjusted** prices with explicit corporate-action
rows, split history must be complete or lot quantities go wrong in a way that
reconciles at the position level and is wrong at the lot level.

---

## 12. Worked example — a wrap-fee adviser account

The reference custodian: an adviser with a taxable brokerage account, a
traditional IRA, and a Roth. Three exports — a holdings snapshot, a transaction
history, and a capital-flows report. It declares **four of the nine
capabilities**, which is what makes it a useful first adapter: an interface
justified only by a source supporting everything would not have been exercised.

| Capability | | Why |
|---|---|---|
| `COST_BASIS` | ✅ | position-level on the snapshot |
| `ACQUISITION_DATE` | ✅ | `Open date`, the block's earliest |
| `CORPORATE_ACTIONS` | ✅ | splits and renames appear as rows |
| `EXTERNAL_FLOWS` | ✅ | the capital-flows report |
| `LOT_DETAIL` | ❌ | one row per position, blended unit cost |
| `TRANSACTION_ID` | ❌ | no confirm number anywhere |
| `INSTRUMENT_SYMBOL` | ❌ | description only on transaction rows |
| `SETTLEMENT_DATE` | ❌ | **column present, data invalid** — see below |
| `HISTORY_TO_INCEPTION` | ❌ | history begins ~28 months after funding |

### What the reconstruction recovered

Over 106 account-and-instrument pairs: 70 positions held at the cutover, 33
opened after it, and three discrepancies — one symbol-change modelling artifact
and two sub-share differences between a two-decimal transaction quantity and a
three-decimal holdings quantity. Nothing rolled back negative.

Basis, under the account's FIFO assumption:

| | positions | `basis_source` |
|---|---|---|
| No disposal or transformation since cutover | 38 | `reconstructed` |
| Block partly survives — FIFO anchors the solve | 6 | `estimated` |
| Block fully consumed, position still held | 3 | `unavailable` |
| Position fully liquidated since the cutover | 23 | `unavailable` |

The 26 `unavailable` positions are all closed or fully turned over, so current
holdings, current basis, and every future decision are untouched. Of 42
dispositions, 28 fall more than a year past the cutover and their long-term
character is certain whatever the seeded date; the other 14 depend on it and the
reconstruction enumerates them for review.

### The traps, and which generalise

**Sweep echoes — 35% of the file.** Every `MoneyTransfer` row moves money between
the cash ledger and two cash-equivalent vehicles, in pairs with the event that
caused it. Recording both halves counts every dividend twice. *Generalises:* the
drop rule is a **whitelist** of the account's declared cash-equivalent
identifiers, and a transfer naming anything outside that set is a refusal — the
one thing that must never happen is a real movement discarded by a rule written
for bookkeeping noise.

**Cross-account fee settlement.** The brokerage pays the IRA's and Roth's
advisory fees; each fee therefore appears twice, once as the funding transfer and
once as the fee, both under an activity word meaning *Expense*. Two further
settlements go to accounts outside the portfolio and are **withdrawals**, not
fees. The amount's sign inverts between paying and receiving accounts while every
other row in the export is unsigned. *Custodian-specific* — a declarative pairing
rule most custodians will not need (ADR 0014).

**Settlement dates corrupted.** 304 of 529 populated cells hold a date earlier
than their own trade date; the column mixes text and spreadsheet-date cells, so a
subset was coerced under an ambiguous day/month reading. *Generalises:* read every
cell as text, never let a spreadsheet library infer a date (invariant 6), and
validate before declaring the capability.

**One activity word, several events.** `Credit` is a symbol change in three rows
and a share-class conversion in a fourth, both lot-preserving, both with **no
matching debit anywhere in the file**, both emitted one row per existing lot.
*Generalises:* the activity map keys on the activity **and** a note pattern, and
the adapter matches incoming quantities against open lots, refusing when they do
not reconcile.

**Split ratios in prose.** `… SHARE-RATIO: 1:4.0`, with `Quantity` holding the
shares *added*. Parsed from the note and cross-checked: `held × (ratio − 1)` must
equal the stated quantity. *Custodian-specific.*

**The capital-flows report contains rows that are not capital flows.** Of 46 rows,
13 are genuine external flows; the rest are in-kind funding at inception and
share-class conversions — a transfer out of one class paired with a receipt into
another, same day, same money. Treating those as flows writes phantom
contributions into the track record (`PORT-GIPS-B02`). *Generalises as a
warning:* a document labelled "capital flows" is not authority that its rows are
capital flows.

For this custodian the report contributes **no ledger rows at all** — its
post-cutover rows duplicate the transaction history exactly, and its earlier rows
describe flows into a period with no valuations, which would produce a division
that looks like a return. They become `portfolio_event` documentation.

**Fractional shares are routine** — 186 rows, including mutual fund buys and
sells. `allows_fractional` must be set or every one is refused, correctly, with
the remedy being an account setting rather than a rounding.

**A zero basis is sometimes correct.** One position is a contra/CVR security from
an acquisition, with a genuine basis of zero.
`BasisAdjustmentReason.FORCED_ZERO_BASIS` exists for it, and that zero is
`derived`, not `estimated`.

---

## 13. Open questions

- **The cutover date**, where a cutover is needed at all. The transaction
  history's first row is the obvious choice. A later cutover shrinks the
  reconstructed portion at the cost of discarding exact history, which is the
  wrong trade — but it is the owner's to make.
- **Covered versus non-covered status** on seeded lots. Rarely stated in an
  export. It does not affect what `portable` computes; it affects what the
  custodian is obliged to report, and is worth carrying where a 1099-B
  establishes it.
- **The second adapter.** Adding a custodian should be a fixture plus two TOML
  files plus a reconciliation. That is the test of whether §§2–6 worked, and it is
  untested until a second custodian exists — the first one always fits.
