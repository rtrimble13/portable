# Broker import

How transactions from a custodian become ledger rows, what `portable` refuses to
guess, and what a real onboarding actually requires.

**Status:** designed, not built. The decisions are ADRs
[0012](adr/0012-broker-import-pipeline.md) (the pipeline),
[0013](adr/0013-cash-sweep-is-cash.md) (sweep vehicles),
[0014](adr/0014-advisory-fees-paid-across-accounts.md) (adviser fees), and
[0015](adr/0015-in-kind-transfers-and-opening-positions.md) (in-kind transfers),
and [0017](adr/0017-cutover-reconstruction-and-basis-provenance.md) (how the
opening position set is reconstructed, and how an approximate basis is kept
distinguishable from an exact one).
[ADR 0016](adr/0016-out-of-order-appends-and-validation.md) fixes a replay defect
that blocks all of it. Nothing here is implemented yet; the milestone is `v0.2`.

---

## 1. The shape

```
broker export ──[extract]──▶ batch file ──[review]──▶ ──[commit]──▶ ledger
   .xlsx / .csv              .json, diffable          validate      + rebuild
```

```bash
pt import broker statement.xlsx --broker wb --account Brokerage -o batch.json
$EDITOR batch.json                       # the review is the point
pt import batch batch.json --dry-run
pt import batch batch.json
pt reconcile --account Brokerage --against holdings.csv --cash 12345.67
```

Three commands rather than one, deliberately. The ledger is append-only: a wrong
row is corrected with a reversing entry that stays visible for the life of the
portfolio, so the cheap place to catch a mistake is the batch file, where fixing
one costs a text edit.

The batch file carries every row the adapter produced **and** every row it
deliberately discarded, each with the source row verbatim and the rule that
handled it. A silently dropped row and a deliberately dropped row are the same
absence in the ledger and completely different claims about the import.

---

## 2. The canonical batch format

`schemas/import-batch-1.0.json`. One object per prospective ledger row:

```json
{
  "format": "portable-import-batch",
  "format_version": 1,
  "source": {
    "broker": "wb",
    "file": "Transactions_By_Date.xlsx",
    "sha256": "…",
    "extracted_at": "2026-09-08T00:00:00Z",
    "period": {"from": "2024-09-25", "to": "2026-08-25"}
  },
  "rows": [
    {
      "action": "append",
      "external_ref": "wb:9f2c1a04d3e88b71",
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
      "source_row": {"Type": "MoneyTransfer", "Notes": "BANK DEPOSIT SWEEP …"}
    }
  ]
}
```

Rows are ordered by `(trade_date, source ordinal)` so that re-extracting after an
adapter change produces a clean diff. Money and quantities are strings, never
JSON numbers — `docs/output-formats.md` and ADR 0005.

The format is the interface. A future OFX adapter, or a batch written by hand for
a handful of corrections, is a first-class input on the same footing as any
shipped adapter.

### Identity, since these exports carry none

The exports have no confirm number, so `external_ref` is synthesized:
`sha256` over the source row's raw text — account, date, activity, description,
quantity, amount — plus an ordinal distinguishing otherwise-identical rows in the
same export. Components are the *source* text, not mapped values, so revising the
activity map never changes the identity of a row already committed.
`UNIQUE (account_id, external_ref)` enforces it; a collision is a refusal, never
a silent skip.

---

## 3. Source documents

The reference adapter targets the three exports the owner's adviser produces.
Each is authoritative for something different, and none is sufficient alone.

| Export | Shape | Authoritative for |
|---|---|---|
| **Transactions By Date** | one row per event; `Date, Type, Account, Quantity, Description, Price, Net amount, Notes, Settlement date, Amount, Commissions` | trades, income, fees, corporate actions |
| **Capital Flows Transactions** | `Date, Account, Activity, Description, Quantity, Amount` | the pre-cutover record: when the accounts were funded and how. Its four post-cutover rows duplicate the transaction export exactly, so it contributes **no ledger rows** — see §4 |
| **holdingsManaged** | one row per position with `Open date, Shares, Unit cost, Cost basis, Market value` | the reconciliation target, and the anchor the cutover reconstruction rolls back from |

Two properties of that table drive everything below. The holdings export is
**position-level, not lot-level** — one row per account and symbol, with a blended
`Unit cost` — so it states a position exactly and cannot describe the lots inside
it. And the capital-flows export reaches back further than the transaction
export, which turns out to document a period that cannot be reconstructed rather
than to extend the one that can.

---

## 4. The cutover, and what it costs

**These three exports are the whole of the evidence, permanently.** No further
report is available — not the transaction export re-run from inception, not a
lot-level cost basis report. Everything below follows from that.

The transaction history begins roughly 28 months after the accounts were funded.
The accounts turned over completely in that gap, with no record of what was held
or traded, so no valuation series can exist for it and no return can be computed
across it. **The portfolio's reporting inception is therefore the transaction
file's first date, not the date the accounts opened** ([ADR 0017](adr/0017-cutover-reconstruction-and-basis-provenance.md)).

### Reconstructing the opening position set

The opening state is not taken from any single document. It is derived by
applying the transaction file **in reverse** to the dated holdings snapshot,
which yields the holding of every instrument on the day before the ledger begins.
Run against the sample exports, over 106 account-and-instrument pairs:

| | |
|---|---|
| positions held at cutover | 70 |
| positions opened after it | 33 |
| discrepancies | 3, all explained |

The three are one symbol-change modelling artifact (§5) and two sub-share
differences between a two-decimal transaction quantity and a three-decimal
holdings quantity. Nothing rolled back to an impossible negative holding, which
is the check that would have failed had the transaction export been incomplete.

### Basis at the cutover

Where a position saw no disposal and no share-class transformation after the
cutover, its cutover basis is today's basis less the cost of every subsequent
addition — exact arithmetic on two exact numbers. That covers **38 of the 70
positions and about 83% of the pre-cutover cost basis**, with no negative or
implausible unit cost anywhere in the result.

The other 32 were sold into after the cutover, and which lots the custodian
relieved is recorded nowhere available. **FIFO is the assumption** — disposals
consume the pre-cutover block first — and it reaches only part of them:

| | positions | `basis_source` |
|---|---|---|
| No disposal or transformation since cutover | 38 | `reconstructed` |
| Block partly survives — FIFO anchors the solve | 6 | `estimated` |
| Block fully consumed, position still held | 3 | `unavailable` |
| Position fully liquidated since the cutover | 23 | `unavailable` |

A position contributing nothing to the present holding gives the solve **no
anchor**: today's basis constrains the block only through what survives of it,
and where nothing survives there is no equation, under FIFO or any other method.
No relief-method choice reaches those 26.

They are all closed or fully turned over, so current holdings, current basis, and
every future decision are untouched. What they touch is the reported realized
gain for the periods they were sold in — and there `pt tax` excludes them from
every total, reports them separately, and marks the year incomplete rather than
printing a gain measured from an arbitrary date. The custodian's 1099-B is the
authority for those years and always was.
[ADR 0017](adr/0017-cutover-reconstruction-and-basis-provenance.md) §2a–2b has
the reasoning. The rule throughout is not that approximation is forbidden; it is
that an approximate number must never be mistakable for an exact one.

### What this costs, precisely

- **A pre-cutover block is one lot.** Specific identification within it is not
  available, whatever the account's default relief method. The custodian's
  records support spec-ID; this reconstruction of them does not.
- **Holding-period character is certain for any disposition more than a year
  after the cutover** — every seeded lot was acquired on or before the cutover, so
  even the latest possible true acquisition date is more than a year prior. 28 of
  the 42 dispositions in the file fall in that window.
- **The other 14 depend on the seeded acquisition date.** The reconstruction
  enumerates them for individual review rather than reporting a count. All are in
  the taxable account and in tax years already filed from the 1099-B, so the
  exposure is to `portable`'s reporting of past gains rather than to a filing.
- **Nothing else is degraded.** Current holdings and current basis are exact,
  because the reconstruction is anchored to the custodian's stated present
  position — and that is the basis every future tax-aware decision runs on. Cash,
  income, fees, and external flows after the cutover come from the transaction
  file directly.

### The capital-flows export contributes no ledger rows

Its four post-cutover rows duplicate the transaction export exactly, so it adds
nothing importable and is instead a **cross-check** on those four. Its
pre-cutover rows describe flows into a period with no valuations on either side;
loading them would produce a division that looks like a return. They become
`portfolio_event` rows, which exist to help a reader interpret a report and are
the right home for "these accounts were funded in kind two years before this
record begins".

---

## 5. Defects in the exports

These are properties of the files, not of the adapter, and each is checked
rather than assumed.

**Settlement dates are unusable as written.** In the sample, 304 of 529 populated
cells hold a date earlier than their own trade date. The column mixes text and
spreadsheet-date cells, so a subset was coerced under an ambiguous day/month
reading — a 29 May trade settling "06/01" reads back as 6 January. The adapter
reads every cell as text and never lets a spreadsheet library infer a date
(`CLAUDE.md` invariant 6), and **refuses** any settlement date earlier than its
trade date rather than storing it. `portable` is trade-date accounting
(invariant 7), so this is recoverable; storing the corrupted value would not be.

On income rows the same column holds something else — it looks like the ex-date.
It is worth capturing, because dividend accrual runs from the ex-date
(`PORT-GIPS-A06`), but it is captured as an ex-date and not as a settlement date.

**There is no symbol column on transaction rows.** Only a `Description` naming
the issuer and a free-text `Notes`. Of 97 distinct descriptions in the sample, 31
do not appear in the holdings export at all — they are closed positions. `TICKER:`
appears in 4 rows of 1,163; an ISIN in 17. Resolution therefore runs through a
hand-curated crosswalk (§7), not a heuristic.

**Amounts are unsigned except where they are not.** Direction is implied by the
activity for trades, income, and deposits, but the fee-transfer rows carry a
genuine sign that inverts between the paying and receiving accounts. The activity
map records, per activity, whether the sign is authoritative or derived. Cash
reconciliation is what catches a mistake here; nothing else does.

**One activity word covers several events.** `Credit` is a symbol change in three
sample rows (`Notes` = `Rename`) and a mutual-fund share-class conversion in a
fourth (`SHR CLASS CONVERSION`). Both are lot-preserving transformations, both
appear with **no matching debit anywhere in the file**, and both were emitted
one row per existing lot. The adapter matches the incoming quantities against
open lots and refuses when they do not reconcile.

**Split ratios live in prose.** `PROCESSED STOCK SPLIT OF CRWD. TYPE: SPLIT;
TICKER: CRWD; SHARE-RATIO: 1:4.0`, with `Quantity` holding the shares *added*
rather than the new total. The ratio is parsed from `Notes` and cross-checked:
`held × (ratio − 1)` must equal the stated quantity, or the row is refused.

**The capital-flows export contains rows that are not capital flows.** Of 46
sample rows, only 13 are genuine external flows. The rest are in-kind funding
events at inception and mutual-fund share-class conversions — a
`Transfer of Securities` out of one share class paired with a
`Receipt of Securities` into another, on the same day, for the same money.
Treating those as external flows writes phantom contributions and withdrawals
into the track record, which is the `PORT-GIPS-B02` failure ADR 0007 exists to
prevent. §4 is why the file is not imported at all; this is why it would have
been dangerous to import naively even if it were.

---

## 6. Instruments, lots, and basis

**Fractional shares are routine** — 186 sample rows, including mutual fund buys
and sells. `allows_fractional` must be set on any account holding funds or the
import refuses every one of them (which is the correct behaviour, and the remedy
is an account setting, not a rounding).

**A seeded block is one lot, and it says so.** No lot-detail report exists for
these accounts, so a pre-cutover position built from several purchases enters the
ledger as a single block with an aggregate basis. What is refused is not the
approximation but its concealment: every lot carries a `basis_source`
(`derived` · `reconstructed` · `estimated` · `custodian_asserted`) that is
`NOT NULL` with no default, and `pt tax` and `pt pnl` disclose any figure
resting on a lot that is not `derived`. [ADR 0017](adr/0017-cutover-reconstruction-and-basis-provenance.md)
sets out the ladder and what each rung costs.

Consequently **specific identification is unavailable within a pre-cutover
block**. `pt sell --lots` can name the block; it cannot name shares inside it.
For an account whose default is spec-ID that is a real reduction in capability,
and the honest description is that the custodian's records support spec-ID and
this reconstruction of them does not.

**A zero basis is sometimes correct.** One sample position is a contra/CVR
security from an acquisition, with a genuine basis of zero.
`BasisAdjustmentReason.FORCED_ZERO_BASIS` already exists for it, and that zero is
`derived`, not `estimated`. Covered versus non-covered status is the broker's to
state and `portable`'s to carry.

---

## 7. The maps

Two data files per adapter, under `src/portable_core/importers/<broker>/`,
reviewed as data rather than buried in Python. A test asserts the adapter's
fixtures are fully covered by both, and any string not in them is a refusal.

### `activity_map.toml`

The reference adapter's vocabulary, as observed. Counts are from the sample
export and are indicative only — the map is exhaustive over what appears, and an
unrecognised string stops the import.

| Broker activity | `portable` | Notes |
|---|---|---|
| `Buy` | `buy` | |
| `Sell` | `sell` | relief method from the account default |
| `Deposit` | `deposit` | genuine external contribution |
| `Income (Dividend)` | `dividend` | `Settlement date` column read as the ex-date |
| `Income (Reinvested Dividend)` | `dividend_reinvest`, **or** `dividend` to cash | cash-equivalent instrument ⇒ income to cash (ADR 0013) |
| `Income (Reinvested Interest)` | `interest` | sweep interest; income to cash |
| `Income (Long Term Gain)` | fund capital-gain distribution, long | see §8 |
| `Income (Short Term Gain)` | fund capital-gain distribution, short | see §8 |
| `Income (Reinvested Long Term Gain)` | distribution + `dividend_reinvest` | two events, one row |
| `Expense (Management Fee)` | `fee`, `external_mgmt_fee` | ADR 0014 |
| `Expense (Transfer to Cover Management Fee)` | `transfer` **or** `withdrawal` | paired; ADR 0014 |
| `Expense (Foreign Tax Paid)` | `taxes_withheld` on the related dividend | **not** a fee |
| `MoneyTransfer` | dropped | cash ↔ cash-equivalent only; see below |
| `Split` | `split` | ratio parsed from `Notes` |
| `Credit` + `Notes: Rename` | `symbol_change` | one row per existing lot |
| `Credit` + `Notes: … SHR CLASS CONVERSION …` | share-class conversion | lot-preserving |
| `Receipt` | share-class conversion / remediation | pairs with the capital-flows export |

Two rules in that table carry the most risk and are stated precisely:

**The `MoneyTransfer` drop rule is a whitelist, not a pattern match.** A row is
dropped only when both endpoints are in the account's declared set of cash and
cash-equivalent identifiers. In the sample all 404 rows qualify — they move money
between the cash ledger, a bank deposit sweep, and a government money market
fund. A `MoneyTransfer` naming anything outside that set is a **refusal**, because
the one thing that must never happen is a real movement discarded by a rule
written for bookkeeping noise.

**The reinvestment rows split on the instrument.** In the sample, 139 of 164
reinvestment rows are on the two sweep vehicles and become plain income to cash;
the remaining ~25 are genuine fund distributions reinvested into new shares and
create a lot.

### `instruments.toml`

Description-to-symbol, with `cusip`/`isin` where the export supplies one. Roughly
97 entries for the sample, of which about 31 name positions no longer held and
must be researched by hand. It is a maintained artifact: a new holding means a
new entry, and the import says so rather than guessing.

---

## 8. Known gaps this work depends on

Tracked here because the import cannot be honest without them.

- **`taxes_withheld` has no write path.** The column exists on `transaction` and
  the field exists on the domain model; no service or CLI sets it. Foreign
  dividend withholding needs it, and so will any retirement-account
  distribution. Whether withholding is reclaimable is a separate stored fact
  (`PORT-GIPS-A06`).
- **Fund capital-gain distributions have no transaction type.** They are income
  for flow purposes and taxed by character. In the sample they occur only in the
  retirement accounts, where character does not bite — which stops being true the
  moment a fund is held in the taxable account.
- **`--ref` is exposed on trades, deposit, and withdraw only.** Every other
  mutating command — transfer, interest, fee, margin-interest, dividend, coupon,
  return of capital, corporate actions — takes no external reference, so imported
  rows of those types cannot be deduplicated.
- **`TradingService` hardcodes `source = MANUAL`.** There is no way to write
  `source = 'import'`, so imported rows would claim to be hand-entered
  (`PORT-GIPS-J03`).
- **`pt reconcile` compares quantities only.** It has no cash comparison, keys on
  symbol rather than CUSIP, and merges every account into one namespace when
  `--account` is omitted, so two accounts holding the same fund reconcile as a
  sum. Per-account scoping and a cash comparison are prerequisites, not
  follow-ons — they are the only check that catches a sign error or a
  double-counted transfer.
- **Wash sales are not detected** until `v0.2`. For a taxable account with real
  activity this matters, and `pt tax` states it on its face.

---

## 9. Acceptance

An import is accepted when it reconciles, not when it parses.

1. **Per statement period, per account:** ending cash and every position quantity
   match the broker. A break exits **6**.
2. **Per closed tax year:** realized gains tie to the 1099-B.
3. `pt validate` passes — which, after ADR 0016, means stored derived state
   actually equals replayed state.
4. `pt export` → `pt import` → `pt export` is byte-identical.

Parser tests establish that the adapter does what it claims. Only reconciliation
establishes that what it claims is right.

Check 1 carries extra weight here. The cutover reconstruction (§4) works backwards
from the custodian's stated present position, so agreement with that position is
not a coincidence — it is the arithmetic closing. What the check actually proves
is that the transaction file is complete enough to bridge the two ends, and that
is the whole of the evidence the reconstruction rests on. Run it per account and
per period, not once at the end.

---

## 10. Onboarding runbook

1. `pt init`, then `pt account add` for each account with its true `opened_date`,
   `--type`, relief method, and `--allows-fractional` where funds are held.
2. Set tax rate schedules on taxable accounts (`pt tax` refuses without them) and
   a `return_policy` (`pert` refuses without one, `PORT-GIPS-B03`).
3. Record the pre-cutover funding events from the capital-flows export as
   `portfolio_event` rows — documentation, not ledger rows (§4).
4. Run the cutover reconstruction: roll the transaction file back from the
   holdings snapshot, review the enumerated exceptions and the dispositions
   falling within a year of the cutover, then seed each opening position as a
   `transfer_in` with its `basis_source` (ADRs 0015 and 0017).
5. Import the transaction history, oldest period first, one account at a time.
6. `pt reconcile` against the holdings export. Do not proceed past a break.
7. Backfill prices, then `pt value` across the period. Watch for snapshots marked
   incomplete: a position that cannot be priced makes the return unanswerable
   rather than approximate.
8. `pt validate`, `pt rebuild`, `pt validate` again.

Step 7 is the one most likely to stall. A complete daily valuation history needs
prices for every instrument ever held, including funds that no longer exist under
that symbol — and mutual fund NAV history is the part that quietly is not there.
Because portfolio accounting uses **unadjusted** prices with explicit
corporate-action rows, split history must be complete or lot quantities go wrong
in a way that reconciles at the position level and is wrong at the lot level.

---

## 11. Open questions

Three questions are now closed. More history **cannot** be obtained, and §4 is
the consequence. The relief-method assumption is **FIFO**, recorded on every lot
it touches. The two fee settlements to accounts outside the portfolio are
**withdrawals**, and those accounts stay outside — a scope decision rather than a
bookkeeping one (ADR 0014). What remains open is smaller.

- **The cutover date itself.** The transaction file's first row is the obvious
  choice and the one §4 assumes. A later cutover would shrink the reconstructed
  portion at the cost of discarding exact history, which is the wrong trade — but
  it is the owner's trade to make.
- **Covered versus non-covered status** on the seeded lots. The exports do not
  state it. It does not affect what `portable` computes; it affects what the
  custodian is obliged to report, and is worth carrying if it can be established
  from a 1099-B.
