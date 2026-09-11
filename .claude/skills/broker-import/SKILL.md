---
name: broker-import
description: Bring a custodian's exports into a `pt` portfolio, or bring an existing one up to date. Use when the owner has new statement exports (xlsx or csv), wants to add a custodian or account, or asks to reconcile the book against the custodian. Authors the adapter's mapping files by interview, never by guessing; runs the segmented first import and the incremental update loop; accepts on reconciliation.
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

## 7. Report to the owner

One memo, no amounts: what imported (counts), the reconciliation result and the
explanation of every break, every assumption you made, every symbol to verify,
and every treatment you chose that the owner should confirm. The owner's
answers become rules, not code.
