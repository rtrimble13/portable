---
name: broker-import
description: Bring a custodian's exports into a `pt` portfolio, or bring an existing one up to date, and price its history. Use when the owner has new statement exports (xlsx or csv), wants to add a custodian or account, asks to reconcile the book against the custodian, or needs the history priced for valuation. Authors the adapter's mapping files by interview, never by guessing; runs the segmented first import and the incremental update loop; accepts on reconciliation; prices through duk, with files for the gaps.
---

# Broker import

You are working on a real portfolio whose owner files taxes from these numbers.
The exports contain real amounts, positions and account numbers. Read them
locally; **never** write an amount, a position, or an account number into any
file under version control, any commit message, or any chat reply the owner did
not explicitly ask for. Row counts, column names, activity strings, symbols and
dates are fine.

Read `docs/broker-import.md` once before starting. §6 is the grammar you author
in; §8 lists what the pipeline refuses and why; §9 says when an import is
accepted. `examples/importers/example-brokerage/` is a complete adapter to copy.
`src/portable_core/importers/william-blair/` is the first real one.

The whole pipeline is data and typed commands. If you find yourself writing
Python to make a custodian fit, stop: either the grammar in §6 is missing
something general, which is a change to `portable_core` with a test and a docs
entry, or you are about to encode a judgment that belongs to the owner.

## 1. Which loop

| the owner has | run |
|---|---|
| exports from a custodian with no adapter directory yet | §2 then §3 then §4 |
| an adapter and a portfolio with these accounts already in it | §5 |
| an adapter and a new account at the same custodian | §4 for that account only (`--cutover`), then §5 |
| an accepted ledger with days not yet priced | §7 |

## 2. Get the files to the CSV boundary

`pt` reads delimited text. A spreadsheet export goes through
`scripts/xlsx_to_csv.py` first, which renders every cell as text and refuses a
number that looks like binary noise:

```bash
python scripts/xlsx_to_csv.py holdings.xlsx  --numeric "Shares,Market value,Cost basis" -o adapter/holdings.csv
python scripts/xlsx_to_csv.py tx-2024.xlsx tx-2025.xlsx --numeric "Quantity,Amount" -o adapter/transactions.csv
```

Several files with identical headers concatenate into one CSV. Name the
numeric columns explicitly: those are the ones the adapter will read as
`Decimal`, and the script holds only those to the no-noise rule. `--reverse`
for a custodian that lists newest first; `--require <column>` to drop a
report's total line, which the script counts on stderr. A custodian that
spells an account differently across exports is handled by
`[account_aliases]` in `source.toml`, not by editing the CSV. Keep the raw
exports outside the repository.

## 3. Author the adapter by interview

Work in a directory outside the repo, or in a directory under
`src/portable_core/importers/<custodian>/` if the owner has said the mapping
files may be committed (they carry no amounts; check with the test in
`tests/integration/test_william_blair_adapter.py` before committing).

Write `source.toml` from the CSV headers: which file is which, the date
formats actually present, the blank markers (`-`, empty), the cash-equivalent
identifiers per account, and the `[[capability]]` checks the data justifies.
Then run

```bash
pt import inspect <adapter>
```

and read the capability table with the owner. Every unmapped activity string
is listed. For each one, ask the owner what the row *is* -- not what to call
it -- and write the rule that says so in `activity_map.toml`. The questions
that recur:

- **One activity word, several events.** Key the rule on the note
  (`note = '<regex>'`) or on the identifier class
  (`identifiers = "cash_equivalents" | "securities"`). All-or-none: once one
  rule for an activity is keyed, every rule for it is.
- **Sweep bookkeeping.** `skip` with `identifiers = "cash_equivalents"` and a
  `reason`. Never a blanket skip.
- **Both legs of an internal transfer**, reported once per account. One
  `transfer` rule with `[activity.pair]`: `counterpart` reads the other account
  from the note (`(?P<account>...)`, with `[account_aliases]` in `source.toml`
  when the note carries account numbers), `window_days` if the legs' dates
  differ, `unpaired_out` / `unpaired_in` for a leg whose other side is outside
  the portfolio. Ask the owner which household accounts are *in* the
  portfolio; a leg to one that is not is a withdrawal or a deposit.
- **A reinvested distribution.** `value = "positive"` and the reinvesting type
  (`dividend_reinvest`, `capital_gain_lt` / `capital_gain_st` with units).
- **Foreign tax on a dividend, on its own row.** `attach = "taxes_withheld"`.
  Ask whether the owner reclaims it.
- **Names instead of symbols.** `instruments.toml`, one `[[instrument]]` per
  description. Propose symbols only where you are sure; mark the rest
  `note = "symbol to verify"` and give the owner the list to confirm against
  fafnir. Never fuzzy-match.
- **Corporate actions** (splits, share-class conversions, spinoffs, tenders,
  CVRs). Map them to `split` / `merger_stock` / `spinoff` so the batch names
  them as `skip` rows; they are recorded with typed commands between segments
  (§4). Write down, for each, what the owner says happened.

Every assumption you make goes in the rule's `reason` or in a `# ASSUMPTION`
comment and into your summary to the owner. Re-run `pt import inspect` until it
reports every row handled.

If the custodian provides a **lot-level realized gain report**, add it as
`[documents.realized]`. It does three things: seeds the basis of blocks sold
after the cutover as `custodian_asserted`, designates the lots every sale
relieves (`lots` on the batch row, specific identification), and is what
`pt reconcile --realized` ties to. Ask for it.

## 4. The first import, in segments

```bash
pt init; pt account add ... --allows-fractional; pt account tax-rates set ...
pt instrument add <symbol> --type <type>   # every symbol the crosswalk and holdings name
pt import reconstruct <adapter> --cutover <date>
```

Agree the cutover with the owner (the ACAT date is usual). Read the
reconstruction: the basis ladder per block, the findings (`vanished_without_
disposal` is a conversion the export shows one side of; `after_snapshot` rows
are set aside), and the dispositions within a year of the cutover. Then:

1. Sort the corporate-action rows by date. For each date *D*, in order:
   `pt import broker <adapter> --cutover <date> --until D-1 -o seg.json`
   (`--incremental` after the first segment), `pt import batch seg.json
   --dry-run`, read it, `pt import batch seg.json`.
2. Record the day's actions with typed commands, cross-checking each against
   the ledger first: a split's added units against `pt holdings` the day
   before; a conversion's outgoing units against what the ledger holds
   (`pt ca split`, `pt ca convert SRC --to DST --units N`, `pt ca spinoff`,
   `pt transfer in ... --basis-source custodian_asserted` for a receipt with
   no source class). Refuse to proceed on a mismatch; report it.
3. Continue to the end. `pt validate`.

A refusal is information. `PT-E-LOT-SELECTION-INVALID` on a designated sale
means the lot the custodian names is not in the ledger on that day -- usually a
corporate action not yet recorded, never a reason to drop the designation.

## 5. Every update after that

```bash
python scripts/xlsx_to_csv.py <new exports> ... -o <adapter>/transactions.csv   # and holdings, realized
pt import broker <adapter> --incremental -o update.json
pt import batch update.json --dry-run
pt import batch update.json
pt reconcile --against stated.csv --realized <adapter>
pt -S fafnir price load --from <first date of the new export>   # then §7 step 3 for any gaps
```

The overlap with the last export shows as `ledger:already-recorded` skips.
New activity strings stop the extract by name: add the rule (§3), re-run.
Corporate actions in the window are `skip` rows: record them as in §4 step 2,
then re-extract `--incremental`.

## 6. Acceptance, and what a break means

An import is accepted when it reconciles, not when it parses (§9). Run
`pt reconcile` per account against the custodian's positions and cash, and
`--realized` against the lot report. Explain every break before proceeding:

- **Cash off by one row's amount** dated after the snapshot: the ledger runs
  past the statement date. Not a defect; say so.
- **Fund units off by hundredths** over years of reinvestment: the export's
  precision. Not a defect; say so, and give the owner the count.
- **A realized break with the lots agreeing**: a basis the custodian adjusted
  without a row -- a distribution reclassified as return of capital is the
  usual one. The owner confirms against the 1099 and records it with
  `pt income roc`; you never infer it.
- **A `custodian_only` sale**: a row the history is missing. Find it.

## 7. Price the history

A return needs a price for every instrument held, on every day it was held,
including every position since sold. Prices come from fafnir through `duk`, and
from price files for what fafnir does not hold (ADR 0029; `docs/market-data.md`
"Filling what fafnir does not hold"). Price only a ledger that §6 accepted:
what was held, and when, comes from the ledger.

**Before anything, check the route** from the owner's shell. The SSH tunnel to
the warehouse is up, and `duk -S db ph AAPL -n 1` prints a price: that is duk
reading the warehouse as the owner's role. `pt` needs the same DSN duk uses
(`FAFNIR_DSN` or `~/.dukrc`) and `psycopg` (`pip install 'portable[fafnir]'`).
If `duk` is not on `PATH`, set `duk_path` in `~/.portablerc`.

1. **Instruments.** `pt -S fafnir instrument sync` compares every instrument
   ever held with fafnir, as of the last day it was held. Go through each
   difference with the owner, and settle the §3 "symbol to verify" list here.
   An instrument fafnir does not know goes on the file list (step 3).
2. **Load.** Run `pt -S fafnir price load --from <cutover> --to <as-of>
   --dry-run`, read every warning, then run it without `--dry-run`. It is
   idempotent; re-run it after any fix. What each warning means:
   - `no ISIN, CUSIP or CIK`: fafnir cannot name the security to duk. Those
     dates go on the file list.
   - `matches N securities`: the identifier is ambiguous in fafnir. That is a
     fafnir data question for the owner. Use a file meanwhile.
   - `does not name a security ... on every date`: the ticker was not in use
     for the whole stretch. Usually it is a rename the ledger has not recorded.
     Ask the owner, record it with `pt ca symbol-change`, and re-run. Never
     guess the old ticker.
   - `no fafnir prices for X`: the warehouse does not hold it. Backfilling
     fafnir is the owner's call; otherwise it goes on the file list.
   - `had no price in the days before they began`: informational.

   `PT-E-PROVIDER-UNAVAILABLE` stops the run. The cause is the tunnel, the
   role, or duk itself, and the message says which. Fix it and re-run.
3. **Fill the gaps from files.** The owner supplies the source: custodian
   statements, a fund company's NAV history. Never invent or interpolate a
   price. Write `symbol,date,price,source,valuation_level`. `source` says where
   each figure came from, and it becomes the price's lineage. Keep the file
   with the raw exports, outside the repository. Run `pt price load --file
   gaps.csv --dry-run` and read what it did not load: a ticker not held under
   that name (a typo, or a pre-rename ticker) and rows outside the days held.
   Then load it.
   - `PT-E-PRICE-CONFLICT`: the file and fafnir, or a hand-set price, disagree
     on a day. Do not pick. Give the owner the list. `--on-conflict keep` or
     `--on-conflict replace` is their decision. A wrong warehouse price is
     corrected in fafnir, not here.
   - An instrument nothing quotes (a CVR) is not given a price.
     `pt instrument market-price SYMBOL --none` (ADR 0028) is the owner's
     decision, offered, never taken for them.
4. **Corporate actions.** `pt -S fafnir ca sync --from <cutover>` lists what
   fafnir records against what the ledger does. It needs
   `mart.v_corporate_action`, fafnir migration 0033. On a warehouse not yet
   migrated to it, the refusal says so: the operator runs `fafnir db migrate`.
   Tell the owner and move on.
5. **Value, and check.** Run `pt value --from <cutover> --to <as-of>`. An
   incomplete snapshot names what it could not price: back to step 3. Then
   `pt validate`.
6. **Accept the history against the statements** (ADR 0031). The anchors are
   the holdings export and each account's year-end statement since 2022; ask
   the owner for the ones not yet in hand. Convert each to a CSV with
   `account,symbol,quantity,market_value` (and the `cash` line), kept with the
   raw exports, then for each:
   `pt reconcile --against <statement.csv> --as-of <its date> --record`.
   `pt price check` re-checks every recorded line whenever prices change.
   - A **price break** is a price from the wrong day or instrument until shown
     otherwise: look at the price's source and date first, and reload it.
   - Only the owner accepts a difference, with a reason in their words:
     `pt price accept SYMBOL -a ACCOUNT --date D --reason "..."`. Propose it
     with the evidence (an evaluated bond price, a different closing print);
     never run it for them.
   - An **unpriced** line needs a price, not an acceptance.
   Report counts and symbols per status, never the amounts.

## 8. Report to the owner

One memo, no amounts: what imported (counts), the reconciliation result and the
explanation of every break, every assumption you made, every symbol to verify,
and every treatment you chose that the owner should confirm. When §7 ran:
which instruments came from fafnir, which from files and from what source,
which are still unpriced, and every conflict left for the owner. The owner's
answers become rules, not code.
