# Broker import

How transactions from a custodian become ledger rows, what `portable` refuses to
guess, and what a real onboarding actually requires.

**Status:** designed, not built. The decisions are ADRs
[0012](adr/0012-broker-import-pipeline.md) (the pipeline),
[0013](adr/0013-cash-sweep-is-cash.md) (sweep vehicles),
[0014](adr/0014-advisory-fees-paid-across-accounts.md) (adviser fees), and
[0015](adr/0015-in-kind-transfers-and-opening-positions.md) (in-kind transfers).
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
| **Capital Flows Transactions** | `Date, Account, Activity, Description, Quantity, Amount` | external contributions and withdrawals, and the in-kind funding events — **it reaches back to account inception, which the transaction export does not** |
| **holdingsManaged** | one row per position with `Open date, Shares, Unit cost, Cost basis, Market value` | the reconciliation target |

Two properties of that table drive everything below. The capital-flows export
covers a longer period than the transaction export, so the two must be
cross-checked where they overlap and the older one trusted where they do not.
And the holdings export is **position-level, not lot-level** — one row per
account and symbol, with a blended `Unit cost` — so it can confirm a quantity and
cannot seed a lot.

---

## 4. What these exports cannot supply

In the sample export set, the transaction history began roughly 28 months after
the accounts were funded. Measured against the holdings snapshot, **44 of 75
current security positions, holding the majority of the portfolio's cost basis,
were opened before the transaction file's first row.** One lot predates the
adviser relationship entirely: it arrived by in-kind transfer from a previous
custodian carrying its original acquisition date, and no transaction the adviser
can produce will ever describe its purchase.

There is no way to close that gap from these three files, and no amount of
adapter work substitutes for the missing rows. Two requests do close most of it:

1. **The transaction export re-run from the account opening date.** The sample
   looks like a defaulted window rather than a retention limit.
2. **A lot-level cost basis report** — usually "Realized/Unrealized Gain & Loss"
   or "Tax Lot Detail". This is the only source of per-lot acquisition dates and
   basis, and the only possible source for lots that arrived in kind.

Whatever the first request yields, the second is required: transferred-in lots
carry basis established at a prior custodian that no adviser record contains.

Until both are in hand the portfolio can be built to a **cutover** — lots seeded
by `transfer_in` (ADR 0015) at an agreed date, full activity after it — which
gives exact tax lots and a track record that honestly begins at the cutover
rather than at inception. What it must not do is average. See §6.

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
sample rows, 13 are genuine external flows. The rest are in-kind funding events
at inception and mutual-fund share-class conversions — a `Transfer of Securities`
out of one share class paired with a `Receipt of Securities` into another, on the
same day, for the same money. Treating those as external flows writes phantom
contributions and withdrawals into the track record, which is the
`PORT-GIPS-B02` failure that ADR 0007 exists to prevent.

---

## 6. Instruments, lots, and basis

**Fractional shares are routine** — 186 sample rows, including mutual fund buys
and sells. `allows_fractional` must be set on any account holding funds or the
import refuses every one of them (which is the correct behaviour, and the remedy
is an account setting, not a rounding).

**Position-level average cost is never accepted as a lot.** Where only the
holdings export is available, a position built from several purchases appears as
one row with a blended unit cost. Seeding a lot from it silently imposes
average-cost relief on an account whose method is spec-ID or FIFO, changing both
the gain and its holding-period character on every subsequent sale. The import
refuses and asks for the lot detail report. ADR 0015 §"Seeding a position"
records why an explicit override, if ever added, is neither a default nor a
fallback.

**Basis asserted by a delivering custodian is marked as such** on the lot, so a
tax report can distinguish a basis `portable` derived from one it was told.
Covered versus non-covered status is the broker's to state and `portable`'s to
carry.

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

---

## 10. Onboarding runbook

1. `pt init`, then `pt account add` for each account with its true `opened_date`,
   `--type`, relief method, and `--allows-fractional` where funds are held.
2. Set tax rate schedules on taxable accounts (`pt tax` refuses without them) and
   a `return_policy` (`pert` refuses without one, `PORT-GIPS-B03`).
3. Import the **capital-flows** export first. It is short, it reaches inception,
   and it establishes the external-flow spine every later number hangs from.
4. Seed pre-ledger lots from the lot detail report as `transfer_in` (ADR 0015).
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

- Whether the transaction export can be re-run from inception (§4). This decides
  full reconstruction versus a cutover, and nothing else can be settled first.
- Which accounts the two out-of-portfolio fee payments belong to (ADR 0014), and
  whether they should instead be modelled by bringing those accounts into the
  portfolio.
- Whether the pre-2022 in-kind lots have covered or non-covered status, and
  whether the delivering custodian's basis is available at lot granularity.
