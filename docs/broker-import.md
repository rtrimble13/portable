# Broker import

How a custodian's exports become ledger rows: what `portable` requires of any
custodian, what it does with more when more is available, and what it refuses to
guess.

**Status:** built, end to end, for the initial load and for every update after
it. The prerequisites, the batch format and its commit stage, the generic
tabular adapter with its capability model, the cutover reconstruction, the
extract stage, and the incremental extract are all implemented; the mapping
grammar covers every trap the reference custodian (§12) presented without a
line of custodian-specific Python. §1 says which command is which. The
decisions are ADRs
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
pt import reconstruct adapters/<name>/   # what was held before the history begins
pt import broker adapters/<name>/ -o batch.json
$EDITOR batch.json                       # the review is the point
pt import batch batch.json --dry-run
pt import batch batch.json
pt reconcile --account <acct> --against holdings.csv --as-of <date>

pt import broker adapters/<name>/ -o update.json --incremental   # every import after the first
```

**The pipeline is complete.** `inspect` and `reconstruct` read and report;
`broker` writes a batch; `batch` validates and commits; `reconcile` is the
acceptance test. Nothing before `pt import batch` writes to the portfolio.

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

A third document is optional and worth asking for: a **realized gain and loss
report at lot level** (`[documents.realized]` — account · instrument ·
acquired · disposed · quantity · cost basis, optionally proceeds and term).
For every lot the custodian closed since the cutover it states when the lot
was acquired and what it cost, which is exactly the basis a reconstruction
cannot otherwise recover for a block disposed of after the cutover (§7). With
it, such a block is `custodian_asserted` rather than `unavailable`, and no
relief method is assumed anywhere, because the report says which lots went —
and every sale the report covers relieves exactly those lots, by designation
rather than by an assumed method (§5, *the lots a sale consumed*).

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
knowledge of spreadsheets, custodians, or column names. They live in
`portable_core.domain.import_records`, not in `importers`: a service reaching
into an adapter package for its input type would put the dependency the wrong
way round and make the adapter, rather than the record, the thing everything
depends on. A layering test enforces it.

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
the ledger are written to the batch as `action: "skip"` under the rule
`ledger:already-recorded`, naming the reference the ledger carries, so the
overlap is visible in the artifact you review. It is deliberately *not* a
`--skip-duplicates` flag at commit: a commit-time skip makes the decision
invisible, and cannot be told apart from an adapter that failed to emit the row.
A duplicate reaching commit is therefore unexpected, and refuses.

**Every import after the first is `--incremental`.** The initial extract seeds
each account's opening positions (§7) and then appends the history; run again
on the same portfolio it would seed them twice, so it refuses by name once an
account already carries ledger rows. `--incremental` is the other shape: no
seed, the overlap skipped as above, and any row dated on or before the
account's first ledger date skipped under `ledger:before-inception`, because
such a row is inside the seeded position already. Run against an account with
no rows, it refuses too — there is nothing to extend, and leaving the opening
positions out would be a silently short portfolio. Neither shape is inferred.

**Corporate actions are imported in segments.** Format version 1 refuses a
split or a conversion, and a sale after one depends on it, so the history is
extracted `--until` the day before each such action and committed, the action
is recorded with its typed command (`pt ca split`, `pt ca convert`,
`pt ca spinoff`), and the next segment is extracted `--incremental`. Rows left
for a later segment are counted in the extract's report, never silently
absent, and every segment is its own reviewed batch.

**Identity.** Where the custodian supplies `TRANSACTION_ID`, that is the
`external_ref`. Where it does not, `external_ref` is `sha256` over the source
row's raw text plus an ordinal distinguishing otherwise-identical rows in the
same export. Components are the *source* text, not mapped values, so revising the
activity map never changes the identity of a row already committed; and the
ordinal counts identical rows, not batch positions, so the same source row gets
the same reference in the initial extract and in every incremental one after it
— which is what lets an overlap be recognised as one.
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
`src/portable_core/importers/<name>/`; your own can live anywhere), and a third
where the custodian writes names instead of symbols:

**`source.toml`** — which file is which, the column map per document, date and
number formats, the account's cash-equivalent identifier set (ADR 0013), and the
declared capabilities with the checks that justify each.

**`activity_map.toml`** — every activity string the custodian emits, with its
`TransactionType`, its effect on quantity and cash, its `fee_class` where it is a
fee, and whether the amount's sign is authoritative or derived. No default arm:
an unmapped string stops the import and names the row.

**`instruments.toml`** — the name-to-symbol crosswalk, where `INSTRUMENT_SYMBOL`
is absent. Declared per document (`crosswalk = "instruments.toml"` under
`[documents.transactions]`); every identifier in that document then resolves
through it and a name it does not carry is a refusal naming the row. No fuzzy
matching: a name resolved to a *plausible* wrong symbol — one share class for
another — reconciles at the quantity level and is wrong at every other. The
cash-equivalent set is checked against the *resolved* identifier, so a sweep
vehicle is declared once, by symbol, whatever the custodian calls it.

A custodian whose exports are plain tabular files needs **no Python** — the
mapping files and a fixture. That is the common case and the point of the
design, and the reference custodian of §12 is now fully expressed in it.

Python is written only where the data needs logic a mapping cannot express: a
split ratio embedded in prose, a reorganisation whose outgoing side is missing
from the file. Those are corporate actions, which the batch refuses by name in
any case (§5), and the typed commands record them with the position context
they need. A per-custodian post-pass remains a documented deviation, not the
norm, and nothing currently needs one.

### Six things a rule can say beyond its type

Each was forced by the reference custodian and each is general, so each is
grammar rather than a post-pass.

**A second key on the note.** One activity word frequently names several events
— *Credit* for a symbol change and for a share-class conversion, *Expense* for a
fee and for the transfer that funds another account's fee. A rule may carry
`note = "<regex>"`, searched case-insensitively against the row's mapped `note`
column, and then applies only where it matches. The no-default-arm principle
holds one level down: once any rule for an activity is keyed on the note,
**every** rule for it must be, so a row matching none of the patterns is a
refusal naming the patterns tried, never a fall-through; a row matching two is a
refusal naming the map. A map that keys on notes over a source that maps no
`note` column is refused when the adapter loads.

**A third key on the identifier's class.** `identifiers = "cash_equivalents"`
or `"securities"` restricts a rule to rows whose identifier is, or is not, in
the account's declared cash-equivalent set, and the same all-or-none rule
binds: once one rule for an activity and note is class-keyed, its siblings
must be. Two uses. A `skip` rule for the custodian's *MoneyTransfer* keyed to
`cash_equivalents` discards a movement between the cash ledger and the sweep
fund and **stops the import** on a *MoneyTransfer* naming anything else — the
one thing that must never happen is a real movement discarded by a rule
written for bookkeeping noise, and a blanket skip on the activity word is
exactly that. And one word for two events: a distribution *reinvested* into
the sweep is income into cash, while the same word on a fund is income and a
lot (`dividend_reinvest`), and only the identifier's class tells them apart.

**A value, where no cash moved.** `amount` on a record is the cash the
account's balance moved. A reinvested distribution and an in-kind receipt move
none, and are still worth something: `value = "positive"` reads the amount
column as the event's stated value instead (a magnitude), and a rule may read
the column as one or the other, never both. The cash roll-back reads the cash;
the batch carries the value as the row's amount and derives the unit price
from it.

**A pairing rule.** A custodian that reports both legs of an internal transfer,
once per account, has reported one event twice; `portable` records a transfer
once, as one row with a counter account (ADR 0007). A rule with
`txn_type = "transfer"` must carry an `[activity.pair]` table, and the adapter
pairs its rows on magnitude, opposite direction, different accounts, and a
trade date within `window_days` of each other (default zero, meaning the same
day; the reference custodian dates the receiving leg three days before the
paying one). The nearest date wins and a tie refuses. The outbound leg becomes
the `transfer`; the inbound leg is carried as a skip naming it.
`counterpart = '<regex with (?P<account>...)>'` reads the other account from
the note, so two IRAs funded with the same amount in the same week are not a
guess — and a leg naming an account that *is* in the export but has no
matching row is a hole in the history and refuses, whatever else the rule
says. Where the note names accounts by number, capture only the part that
tells them apart and resolve it through `[account_aliases]` in `source.toml`
(`"48" = "IRA"`), so the number is in no mapping file. The same table resolves
the account column of every document, so a custodian that upper-cases the
account in one export and not in another (`"BROKERAGE" = "Brokerage"`) needs
no edit to its files. `unpaired_out =
"withdrawal"` and `unpaired_in = "deposit"` declare what a leg with no
counterpart becomes when the other side is outside the portfolio; absent, an
unpaired leg is a refusal. Direction is the leg's direction and the fallbacks
are checked against it at load.

**An attachment.** A custodian that reports tax withheld at source as its own
line — one per foreign dividend, same day, same security — has reported one
event in two rows. Withholding is tax, not a fee, and the return is earned on
the gross while the cash moves by the net (`PORT-GIPS-A06`), so the ledger wants
the income row with `taxes_withheld` on it. A rule with
`attach = "taxes_withheld"` (and no `txn_type`) folds its row's amount into the
one income row on the same day, in the same account, on the same instrument,
and is carried as a skip naming it; none or two such rows refuse. Whether any
of the withholding is reclaimable is a separate fact the batch carries per row.

**An inverted sign.** `cash = "inverted"` for a column whose sign is
authoritative and backwards — the custodian signs from its own side of the
ledger, so a positive is money out. The reference custodian's transfer-to-cover
rows are the case: positive in the paying account, negative in the receiving
one, unsigned everywhere else in the export.

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
| `inverted` | the column's sign is authoritative and backwards: positive is money out |
| `positive` | a magnitude that always means an increase for this activity |
| `negative` | a magnitude that always means a decrease for this activity |
| `none` | this activity carries no value in that column at all — a stated absence, not a zero |

### What is refused, and when

Everything wrong with a *mapping* is refused when the file loads, because a map
is reviewed once and used for every row after: an unmapped activity string, two
rules for one string, a fee with no `fee_class`, a skip with no reason, an
unknown transaction type or check name, a capability declared twice, a date
pattern that is invalid *or that carries only part of a date* (`%Y-%m` parses
happily and silently returns the first of the month), a note pattern or
counterpart pattern that is not a valid regex, an un-keyed rule beside
note-keyed or class-keyed rules for the same activity, an unknown identifier
class, a rule reading the amount column as both `cash` and `value`, a transfer
with no pairing table, a pairing table on anything but a transfer, a
counterpart pattern with no `account` group, a negative window, a fallback
pointing the wrong way, an attachment that also names a type or a skip, a
crosswalk name mapped twice, and a map that reads a column (`note`,
`identifier`) the source does not map.

Everything wrong with the *data* is refused with the row quoted: a mapped column
the file does not have (the error lists the headers it does have), a cell that is
not a number in the declared format, a date matching no declared pattern, a blank
where the map expects a value, a note matching none or two of an activity's
patterns, an identifier of a class no rule for its activity names, an
instrument name the crosswalk does not carry, a transfer leg with two candidate
counterparts at the same distance or with a named counterpart that is in the
export but has no matching row, a withholding line with no single same-day
income row to attach to, a snapshot carrying two as-of dates, and a snapshot
stating no cash for an account — which is refusable rather than tolerable
because cash reconciliation is the only check that catches a sign error or a
dropped row. An account whose custodian genuinely reports no cash line is listed
in `allow_missing_cash`, so the exception is on the record.

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

**Implemented as `pt import reconstruct`**, which reports and writes nothing,
and as `pt import broker`, which turns the same result into batch rows. Keeping
the two apart is what makes the reconstruction re-runnable — the correct
response to finding a mapping error is to re-derive the cutover state and
rebuild, never to patch lots.

Three things the seed needs that the roll-back does not supply:

**A market value at the cutover.** Each `transfer_in` carries two numbers that
must not be conflated: `amount` is what the shares were worth on the cutover
day, and `original_basis` is what was paid. `pt import broker` **refuses** when
a cutover price is missing and names the instruments. There is no honest
substitute, and the basis — the number to hand — is exactly the one that must
not be used: the value establishes the account's beginning market value, so
substituting the basis would make the first period's return wrong by the whole
unrealized gain at cutover. One source other than the price table is accepted:
an account funded in kind on the day the history begins has, for each position
received, a row on the cutover date stating the units and what they were worth,
and that valuation — the custodian's own — is used where the table has nothing,
and recorded on the seed row as where the price came from.

**A snapshot dated before the history ends** is ordinary: the position
statement is pulled one day and the history the next. Rows dated after the
snapshot are not in the state being rolled back and are set aside, named in a
finding, and appended as history like any other; reconcile as of the snapshot
date rather than the history's end.

**The cash held at the cutover.** Seeded as a `deposit` on the cutover date —
or a `withdrawal` where the rolled-back balance is negative, which is a margin
loan and not a contribution. Without it the account starts from zero cash and
every purchase drives it negative by exactly the opening balance, which the
reconciler reports as a break. Recording it as a flow needs the justification
ADR 0015 already gives: a flow on the account's opening date establishes its
beginning market value rather than a flow into the period, and the cutover
*is* the reporting inception (§7, ADR 0017 §1). That exception belongs to the
return engine and is not yet implemented; until it is, no first-period return
is computed and nothing reads the classification.

**A relief method on every closing trade.** Without the custodian's word on
which lots a sale consumed, the seeded basis was solved under an assumed FIFO
relief (ADR 0017 §2a), so the ledger has to relieve the same way: a block
solved for FIFO and then relieved spec-ID yields a basis the solve never
computed. `pt import broker` writes `relief_method: "fifo"` on each closing
row rather than leaving it to the account default — visible in the file,
changeable by the reviewer, and understood to invalidate the solve if changed.

**The lots a sale consumed, where the custodian states them.** With the
realized document (§2), every closing row it covers carries `lots`: the
acquired date, units and cost of each lot the custodian says that sale
consumed, and `relief_method: "spec"`. That is specific identification in
everything but the lot id, which only the ledger knows. At commit each entry
resolves to the ledger's lot opened on that day — where two were, to the one
whose original basis is what the custodian says the lot cost — and a lot
acquired before the ledger begins resolves to the seed carrying its block.
Anything else is refused as `PT-E-LOT-SELECTION-INVALID` naming the lots that
do exist: designating a lot the ledger cannot find and relieving something
else instead is the substitution specific identification exists to prevent.
The report's lots for a sale must total the sale's units; where they do not,
the row falls back to the assumed method with the discrepancy written on it.

One limit to know. A block seeded at the cutover is **one** ledger lot at the
block's aggregate basis, however many lots the custodian holds inside it, so a
sale that takes part of such a block relieves the block's average and the
custodian's report relieves the specific lot. The totals agree; the per-sale
split can differ until the block is gone. Seeding one lot per custodian lot
would close that gap and is not done yet.

**The opening position set is derived, not read.** Apply the transaction history
**in reverse** to the holdings snapshot to obtain the holding of every instrument
on the day before the ledger begins. Each becomes a `transfer_in` (ADR 0015).

Cash rolls back the same way and is reported per account, because it is the
reconciliation anchor's other half: quantities that reconcile and cash that does
not is the signature of a sign error or a dropped row.

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
| The custodian's realized report closes the block's lots and states their cost (§2, the optional third document) | `custodian_asserted` |

One formula serves the first two rows. The custodian's present basis is
`surviving_block * unit_cost + cost of every surviving addition`, so the block's
unit cost is what is left when the additions are taken out, divided by what
survives. With no disposals the whole block survives and it degenerates to
"today's basis less what was added since".

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
2. **Per sale:** realized gains tie to the custodian's own lot-level report.
   `pt reconcile --realized <adapter>` compares proceeds, basis and gain of
   every disposition against the report's lots for the same account,
   instrument and day, and exits **6** on a break. Per sale rather than per
   year, because a year that ties can hide two sales wrong by offsetting
   amounts. A disposition of a lot whose basis is `unavailable` is shown as
   unreportable, not a break; a disposition the report lacks that realized
   nothing (a sweep redemption) is shown, not a break. A break with the lots
   agreeing is a basis the custodian adjusted and the activity export never
   showed — a distribution reclassified as return of capital is the usual
   one — and is the owner's to record with `pt income roc`, not the importer's
   to infer.
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

- ~~Fund capital-gain distributions have no transaction type.~~ `capital_gain_lt`
  and `capital_gain_st` (schema 0004): income for flow purposes, the character
  is the type, and either may carry reinvested units. `pt income capital-gain`.

**Open.**

- **Wash sales are not detected** until `v0.2`; `pt tax` says so on its face.

---

## 11. Runbook

`.claude/skills/broker-import/SKILL.md` is this section written for an agent
running it with the owner: the interview that produces the mapping files, the
segmented first import, the update loop, and what each kind of break means.

0. A spreadsheet export goes through `scripts/xlsx_to_csv.py` first: every cell
   as text, numeric columns named and held to a no-noise rule, files with one
   header concatenated, a report's total line dropped only when asked
   (`--require`). `pt` reads delimited text and nothing else (§2).
1. `pt init`; `pt account add` per account with its true `opened_date`, `--type`,
   relief method, and `--allows-fractional` where funds are held;
   `pt instrument add` for every symbol the crosswalk and the holdings name.
2. Tax rate schedules on taxable accounts, and a `return_policy`
   (`PORT-GIPS-B03`).
3. `pt import inspect` and **read the declared capability set**. It tells you
   what this portfolio will and will not be able to claim. Every unmapped
   activity string is a rule to write (§6), never a default arm.
4. `pt import reconstruct --cutover`; review its findings, the basis ladder per
   block, and any dispositions falling within a year of the cutover.
5. Import the history **in segments around its corporate actions**: extract
   `--until` the day before each one (`--cutover` the first time,
   `--incremental` after), `pt import batch --dry-run`, read it, commit; record
   the action with its typed command, cross-checked against the ledger (a
   split's added units against the holding the day before, a conversion's
   outgoing units against what is held); continue. A sale designated to a lot
   the ledger does not yet have refuses as `PT-E-LOT-SELECTION-INVALID`, and
   the usual reason is an action not yet recorded.
6. `pt reconcile --against <positions.csv> --realized <adapter>`. Do not
   proceed past a break you cannot explain (§9).
7. Backfill prices, then `pt value` across the period. Watch for snapshots marked
   incomplete: a position that cannot be priced makes the return unanswerable
   rather than approximate.
8. `pt validate`, `pt rebuild`, `pt validate` again.

**Every update after that** is the same loop with one flag: convert the
custodian's current exports, `pt import broker <adapter> -o update.json
--incremental`, read the batch — the overlap with the last export appears as
`ledger:already-recorded` skips and the new rows as appends — then `pt import
batch --dry-run`, `pt import batch`, and `pt reconcile` against the new
snapshot and the lot report. A corporate action in the window is a `skip` row:
record it as in step 5 and re-extract. A second custodian is its own adapter
directory and its own initial extract; its accounts seed with their own
cutover, and reconcile on their own.

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
caused it. Recording both halves counts every dividend twice. *Generalises, and is
grammar:* the drop rule is a **whitelist** of the account's declared
cash-equivalent identifiers (`identifiers = "cash_equivalents"`, §6), and a transfer naming
anything outside that set is a refusal — the one thing that must never happen is
a real movement discarded by a rule written for bookkeeping noise.

**Cross-account fee settlement.** The brokerage pays the IRA's and Roth's
advisory fees; each fee therefore appears twice, once as the funding transfer and
once as the fee, both under an activity word meaning *Expense*. Two further
settlements go to accounts outside the portfolio and are **withdrawals**, not
fees. The amount's sign inverts between paying and receiving accounts while every
other row in the export is unsigned. *Custodian-specific in its
arrangement, general as a primitive* — the pairing rule of §6 with
`cash = "inverted"`, a `counterpart` pattern reading the account from the note,
and `unpaired_out = "withdrawal"` for the two outside accounts (ADR 0014).

**Settlement dates corrupted.** 304 of 529 populated cells hold a date earlier
than their own trade date; the column mixes text and spreadsheet-date cells, so a
subset was coerced under an ambiguous day/month reading. *Generalises:* read every
cell as text, never let a spreadsheet library infer a date (invariant 6), and
validate before declaring the capability.

**One activity word, several events.** `Credit` is a per-lot basis memo, a
rename, the incoming side of a share-class conversion, a tender, and a contra
receipt — the conversions with **no matching debit anywhere in the file**, and
the memos emitted one row per existing lot beside the row that carries the
total. *Generalises, and is grammar:* the activity map keys on the activity
**and** a note pattern (§6); the memo rows are skips with a reason and the
renames are skips because the crosswalk already resolves both names to one
symbol. The conversions themselves are corporate actions, refused by the batch
and recorded with `pt ca convert`, which exchanges every open lot for the
stated units with basis, acquisition date and holding period carried.

**Split ratios in prose.** `… SHARE-RATIO: 1:4.0`, with `Quantity` holding the
shares *added*. A split is a corporate action, which the batch refuses by name
and lists as `unsupported`; it is recorded with `pt ca split`, which has the
position context to cross-check `held × (ratio − 1)` against the stated
quantity. The ratio is read by a person, not parsed. *Custodian-specific in
form, and deliberately not automated.*

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
- **The second adapter.** Adding a custodian should be a fixture plus the
  mapping files plus a reconciliation. The grammar of §6 was widened until the
  first custodian fit without Python; whether it was widened in the right
  directions is untested until a second custodian exists — the first one always
  fits.
