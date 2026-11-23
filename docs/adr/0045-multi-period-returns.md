# ADR 0045 — Multi-period returns: one walk, rows that stand alone, and a record that breaks where nothing was held

- **Status:** Accepted (owner, 2026-11-23), with the recommended answers to all five questions. Implemented so far:
  - §1, `walk` and `window` in `portable_core/services/returns.py`, held to `pert twr` by `tests/integration/test_twr_windows.py`;
  - §4, the break, in the same module, tested by `tests/integration/test_track_record_breaks.py`;
  - §7's `twr-2.0`, published;
  - §5, in `portable_core/domain/periods.py` (the anniversary, the year count) and `services/periods.py` (`annualize`), with the formatter's guard reading the same rule;
  - §2's fiscal year end, `FiscalYearEnd`, refused by `pt init` unless it is a month end.

  The periods themselves (§2's table, §3, §6, §8), `pert periods`, and `pert`'s own check of a stored fiscal year end are not yet. Accepted with [ADR 0044](0044-estimated-returns-over-unpriced-days.md), so the two share `twr-2.0` (§7)
- **Date:** 2026-11-23; accepted 2026-11-23
- **Milestone:** v0.2, issue 5 (P1)
- **Governs:** `PORT-GIPS-A04`, `B04`, `B07`, `H01`, `H02`, `H03`, `H04`, `H06`; `CLAUDE.md` "Do not annualize sub-one-year returns", invariants 6, 9, 10
- **Amends:** [ADR 0040](0040-time-weighted-return.md) §4 (idle days), and settles its §11 (annualisation, deferred to this issue)
- **Depends on:** [ADR 0005](0005-decimal-representation-and-rounding.md), [ADR 0024](0024-valuation-snapshots-are-valued-state.md), [ADR 0031](0031-price-acceptance-against-statements.md), [ADR 0040](0040-time-weighted-return.md)
- **Relates:** [ADR 0044](0044-estimated-returns-over-unpriced-days.md) (its §3 already says how a period row treats an estimate), issue 58 (the custodian reports by period), issue 7 (its 36 monthly returns are windows of the same walk)

## Context

### What exists

`pert twr --from --to` gives one cumulative return per basis over one
period. `time_weighted_return` walks every day from the period's anchor (the
day before its first day) to its end. It checks that each snapshot exists
and still re-derives (ADR 0024), that no ADR 0031 block applies, and that no
unpriced trade falls inside (ADR 0040 §12). Then it cuts a sub-period at
every usable day and links them. Any failure refuses the whole period.

Already in place for issue 5:

- **`meta.fiscal_year_end`**, `MM-DD`, set by `pt init --fiscal-year-end`
  (default `12-31`). Nothing reads it and nothing validates it: `pt init`
  stores any string. No command changes it afterwards.
- **`ReturnValue`** carries method, basis, period and `is_annualized`.
  `render_return` refuses an annualised value under a year
  (`PT-E-GIPS-ANNUALIZE-SUB-YEAR`).
- **`Disclosures`** on every result, with `level5_share` at the period end
  (`PORT-GIPS-H05`).

### Why one call per row will not do

On the committed sample, `pert twr` over 2025 takes 0.5 s and since
inception (five years) 1.8 s. Under a profiler, 86% of the since-inception
run is `_require_checked`, the walk that re-derives every snapshot. The
arithmetic takes under 0.1 s. A table of period-to-date, trailing and annual
rows covers the history about four times over. Called once per row, it would
repeat that walk about four times.

The stronger reason is that a row and `pert twr` computed by different code
paths could disagree, and nothing would say which one was right.

### What reading the code turned up

ADR 0040 §4 skips a day on which the level held nothing and nothing moved,
and links across it. It names the case: "an account emptied and later
re-funded". It was decided without `PORT-GIPS-H06`, which says a record that
goes to nothing and restarts must not be linked across the break.

Each case below was reproduced on a one-account file:

1. 10,000 deposited 01-02 and invested at 100.
2. Sold at 110 on 01-04, and all 11,000 withdrawn 01-05.
3. Nothing held until 10,000 is deposited and invested at 100 on 01-22.
4. Closing at 90 on 01-31.

| `pert twr` | Prints | What it is |
|---|---|---|
| `--from 2024-01-02 --to 2024-01-31` | −1.00%, 30 days | Two stints, +10% and −10%, linked across 16 days with nothing held (`PORT-GIPS-H06`) |
| `--from 2024-01-08 --to 2024-01-19` | 0.00%, 12 days | No sub-period at all. The empty product prints as a zero return (invariant 10) |
| `--from 2024-01-02 --to 2024-01-19` | +10.00%, 18 days | The return of 01-02 to 01-05, labelled as running to 01-19. A record that terminates is reported through its termination date (`PORT-GIPS-H02`) |

The arithmetic is right in all three. The label, or the presence of a
number at all, is wrong, which is the same failure.

Separately, the annualisation guard tests `period_days < 365`. The period
from the end of 2023-12-31 to the end of 2024-12-30 is 365 days and one day
short of a year, because 2024 has a 29 February. The guard would let it be
annualised. Nothing annualises today; issue 5 is the first thing that will.

## Decision

### 1. One walk, many windows, and `pert twr` is one window

`services/returns.py` splits in two.

- **The walk** runs once, from inception to `--to`, at the level asked. It
  checks every snapshot once. For each day it records whether the day is
  usable, and if not, why: missing, stale, incomplete, blocked (ADR 0031),
  an unpriced trade (ADR 0040 §12), or no policy in force. It cuts
  sub-periods exactly as today and finds the track records (§4). It raises
  only what refuses every window.
- **A window** links the sub-periods inside it. Otherwise it returns the
  refusal that applies to it, taking the checks in `pert twr`'s order.

`time_weighted_return` becomes the walk plus one window, raising that
window's refusal. `pert twr` and every row of the new command run the same
code.

**The contract.** Every row equals `pert twr --from F --to T [--account A]`
for the row's dates, value for value. If that command would refuse, the row
carries the same refusal, code for code. A differential test asserts this
for every row, against separate `pert twr` runs. Sub-periods are cut at
every usable day, so a window between two usable days holds whole
sub-periods only. A window whose anchor or end is unusable is refused, as
`pert twr` refuses it today.

### 2. The periods

Every row ends at `--to` (T), which defaults to `--as-of`, explicitly
(invariant 6). Y is the fiscal year end.

| Row | From the end of | Through | If inception is later |
|---|---|---|---|
| Month to date | the last month end before T | T | starts at inception, labelled partial |
| Quarter to date | the last fiscal quarter end before T | T | the same |
| Year to date | the last fiscal year end before T | T | the same |
| 1, 3, 5, 10 years | T less N years (§5's anniversary) | T | **null**: "the record is shorter than N years" |
| Since inception | the day before the current record's inception | T, or the record's termination (§4) | — |
| Each fiscal year | the fiscal year end before it | its fiscal year end | the first runs from inception and is labelled partial, and so does the last to a termination (`PORT-GIPS-H02`) |

- **Annual rows are complete fiscal years only**, plus a record's
  terminating stub. The year that contains T is the year-to-date row, not an
  annual row. So once a year is complete, its row never changes with T
  (`PORT-GIPS-A04`). When T is itself a fiscal year end, the year-to-date
  row and the last annual row are the same window, with the same number.
- **A period-to-date row starts at inception, a fixed-length row never
  does.** "Year to date" from a mid-year inception is still year to date,
  and the start moves as it does in `pert twr`, with both dates carried. A
  "3-year" figure over two years is a different number under a false label.
  It is null with its reason, never the shorter figure.
- **The fiscal year end must be a month end**: `MM-DD`, where `DD` is the
  last day of `MM` and `02-28` means the last day of February. Quarters are
  the three-month steps back from it. Anything else is refused
  (`PT-E-FISCAL-YEAR-END`), naming the stored value, by `pert` and from now
  on by `pt init`. `pt init` treats it as a bad flag value and exits 2, as
  it does for a malformed date. A stored value `pert` cannot read is a
  validation failure, exit 4. Weeks-based fiscal years (52/53) are not supported. The
  owner's file is `12-31`, so nothing changes for it.
- **Custom ranges stay `pert twr --from --to`.** Issue 7's monthly series
  is a list of windows on the same walk.

### 3. Each row stands alone

- **A row is measured or null.** A null row carries its refusal (code,
  message, remedy), the same one `pert twr` gives for that window. In JSON,
  `returns` is `null` and `refusal` is set. In `table` and `markdown`, its
  cells are `—`, with a footnote giving the reason. On stderr, each null row
  gets a warning. It is never zero and never blank (invariant 10,
  `docs/output-formats.md` "Null is explicit").
- **Refused before any row:** no ledger rows through T, an invalid fiscal
  year end, or a `--to` before inception.
- **Exit status.** The command exits 0 when at least one row is measured.
  When none is, it exits with the first row's refusal, as `pert twr` would
  (question 4).
- ADR 0044 §3 already assumes rows stand alone. Nothing here waits on it.

### 4. A track record ends where nothing was held, and is never linked across (`PORT-GIPS-H06`)

This replaces the first bullet of ADR 0040 §4. Its second bullet stands:
undefined days are refused.

- **An idle day**, at the level measured, has beginning value zero, ending
  value zero, no flow and no fee. That is ADR 0040 §4's definition,
  unchanged. At portfolio level, a day is idle only when every account past
  inception is idle.
- **A track record** runs from its inception, the first day that is not
  idle, to its termination: the last day that is not idle before a run of
  idle days.
  - A run of idle days followed by a day that is not idle is a **break**.
    That day starts a new record.
  - A run that reaches T means the record has terminated and has not
    restarted.
- **A window that spans a break is refused** (`PT-E-RETURN-BREAK`), in
  `pert twr` too. Spanning means it holds one record's termination and the
  next record's inception. The message names both records' dates and the
  idle run, and the remedy names the window on each side.
- **A window that starts inside an idle run starts at the next inception**,
  exactly as a window starting before inception does today, with both dates
  carried. A fixed-length row whose start falls there is null instead
  (§2).
- **A window entirely inside an idle run is refused**
  (`PT-E-RETURN-NOTHING-HELD`). It no longer prints 0.00%.
- **A window that runs past a termination ends at the termination.** It
  carries the requested end beside the actual one, just as the start already
  carries the requested start and the inception. `pert twr`'s `period` gains
  `requested_end` (§7). The annual row for that year ends there too, and is
  labelled partial (`PORT-GIPS-H02`).
- **Since inception means the current record.** The annual table shows the
  earlier records with rows of their own and the break between them as a
  row ("nothing held from … to …"). The two records are never linked.
- **The same rule holds at both levels** (question 1). At account level, an
  account emptied and re-funded starts a new record. ADR 0040 §4 linked it.
  Linking treats the idle run as a 0% return the account earned; in fact the
  money was not in the account at all.

After this, a measured window never contains an idle day: one would be a
break, or past a termination. `twr-2.0` drops `idle_days` (§7).

### 5. Annualising a period of a year or more (`PORT-GIPS-B07`)

This settles ADR 0040 §11.

- **"At least a year" is a calendar test, not a day count.** A period from
  the end of day A to the end of day T is at least a year when T is on or
  after A's first anniversary. The anniversary is the same month and day N
  years on. When A is the last day of its month, it is the last day of that
  month instead: 2024-02-29 goes to 2025-02-28, and 2023-02-28 to
  2024-02-29.
- **One function decides it**, in `domain/periods.py` (pure, importable by
  the formatter). The guard in `formatters/numbers.py` reads it, and
  `_DAYS_IN_YEAR` is retired, because it admits the 2024 example above.
- **The number of years, N**, is the whole anniversaries on or before T,
  plus the days left over, divided by the length of the year they fall in
  (actual/actual). A 3-year period is exactly 3 however many 29 Februaries
  it holds.
- **The formula is `exp(ln(1 + R) / N) − 1`**, in ADR 0040 §9's
  `RETURN_CONTEXT`. `Decimal.ln` and `Decimal.exp` are correctly rounded. A
  non-integral `**` is not guaranteed to be, so its last digit could differ
  between decimal implementations. Written this way, the rounding of every
  step is defined, and so are the bytes (invariant 6).
- **No annualised figure exists when 1 + R ≤ 0.** It is null with its
  reason (`PT-E-RETURN-TOTAL-LOSS`).
- **Over exactly one year, the annualised figure is the cumulative return
  itself** (as built). Computed through `exp` and `ln`, it could differ in
  the last digit, and a one-year row would then show two numbers for one.
- **Presented:** every row of a year or more shows cumulative and
  annualised, each labelled as such (question 3). Under a year, the
  annualised figure is null with the reason `PORT-GIPS-B07`.

### 6. What each row carries (`PORT-GIPS-H03`, `H04`, `H05`)

- its kind and label, its requested and actual dates, and whether it is
  partial and why (inception or termination);
- per basis, the cumulative and annualised `ReturnValue`s, each with
  method, basis and period (`PORT-GIPS-H04`);
- **ending market value** at its actual end, summed over accounts at
  portfolio level, and **the number of accounts holding value** on that day
  (`PORT-GIPS-H03`: "as of each annual period end");
- **its own disclosures**, the `twr-1.0` object for its own window, so a
  year's row reads the same whatever else is in the table. `level5_share`
  (`PORT-GIPS-H05`) is already in it.

The table has columns Period · From · To · cumulative · annualized ·
Ending value · Accounts. The basis is named in the return column headers
(`PORT-GIPS-H04`). Net-of-fees is shown by default (ADR 0040, answer 4), and
`--basis` picks another. JSON always carries every basis.

### 7. The command, and the schemas

```
pert periods [--to DATE] [--account NAME] [--basis BASIS]
             [--format table|json|csv|markdown] [--as-of DATE] [-v|-vv] [--quiet] [--no-color]
```

- **`schemas/periods-1.0.json`**, with the disclaimer in the envelope
  (`CLAUDE.md`, Output). It carries ADR 0044's `is_estimated` on every
  return and its `estimate` block from its first version.
- **`twr-2.0`, shared with ADR 0044.** Both ADRs were accepted the same
  day, so `pert twr`'s schema takes one major version. From this ADR it
  takes:
  - `requested_end` in `period`;
  - the two refusals of §4;
  - no `idle_days`.

  From ADR 0044 it takes `is_estimated` and `estimate`. Whichever is built
  first publishes `twr-2.0` with all of these. Until the estimate tier
  exists, `is_estimated` is `false` and `estimate` is `null`. That is true,
  not a placeholder: nothing is estimated without the flag.
- **The draft offered a `twr-1.1`**, additive, for this ADR alone. It is
  not published: one version change is cheaper for a consumer than two.
- **As built**, `period` also names the days set aside, as
  `nothing_held_before` and `nothing_held_after`, each a `{from, to}` or
  `null`. These replace `idle_days`. Without them a reader sees that a date
  moved but not why. `estimate` is typed as `null` or an object. Its fields
  are specified when the estimate tier is built, and no output ever had any
  other shape.

### 8. Stability (`PORT-GIPS-A04`)

On the same file, an annual row is byte-identical whatever `--to` and
`--as-of` are:

- its dates come from the fiscal year end alone;
- its disclosures are its own window's;
- whether a day is usable depends on that day's snapshot, ledger, policy
  and blocks, never on where the walk ends.

A termination later followed by a restart leaves the terminating record's
rows as they were, because the termination date does not move.

### 9. Where it lives

- `domain/periods.py`: the anniversary and year-count functions, `Window`
  (kind, label, requested and actual dates, why it is partial) and
  `TrackRecord`. No I/O.
- `services/returns.py`: the walk and the window (§1).
- `services/periods.py`: resolves the windows from the fiscal year end, T
  and the records, and annualises.
- `formatters/numbers.py`: the guard reads `domain/periods.py`.
- `portable_pert/commands/periods.py`: thin.

## Test obligations

- `test_annual_boundaries_are_stable` (`PORT-GIPS-A04`): the 2024 row from
  `--to 2025-03-31` and from `--to 2025-12-31` is byte-identical in `json`.
- `test_partial_first_year_labelled_and_unannualized` (`PORT-GIPS-H02`).
- `test_subyear_return_never_annualized` (`PORT-GIPS-B07`), parametrised
  over:
  - 1 day and 1 month;
  - 364 days;
  - 365 days across a 29 February, which is refused;
  - 365 days not across one, which is allowed;
  - 366 days.

  `docs/gips-standard.md` currently parametrises it as though 365 days were
  always a year. It is amended on acceptance.
- `test_track_record_break_not_linked` (`PORT-GIPS-H06`): the three cases of
  the Context table, in `pert twr` and `pert periods`. The first two are
  refused, and the third ends at the termination.
- `test_a_row_is_pert_twr_over_its_window` (`PORT-GIPS-B04`): the
  differential test of §1.
- `test_fixed_length_row_never_shortened`: a 3-year row on a 2-year record
  is null, never the 2-year figure.
- `test_annualised_years_count_anniversaries`: a 3-year period holding a 29
  February annualises with N = 3, checked by hand.
- `test_fiscal_year_end_must_be_a_month_end`:
  - `06-15` is refused by `pt init`, and by `pert` on an existing file;
  - `06-30` gives quarters ending 09-30, 12-31, 03-31 and 06-30.
- `test_every_annual_row_carries_its_assets` (`PORT-GIPS-H03`).
- `test_a_null_row_is_never_zero` (invariant 10), in all four formats.
- Golden files for the sample's table, in all four formats (issue 5).

## Open questions for the owner

1. **The break at account level.** Break at both levels (recommended), or
   only at the portfolio, linking an account across its idle days as ADR
   0040 §4 does? Under the recommendation, an account withdrawn in full on
   Monday and re-funded on Wednesday starts a new record. Re-funded on
   Tuesday, it does not, because no full day was idle.
2. **`pert twr` past a termination.** End at the termination and carry
   both dates (recommended, the mirror of the start), or refuse and name the
   termination?
3. **Annualised beside cumulative** on every row of a year or more
   (recommended), or cumulative only unless asked?
4. **Exit status with some rows null.** Exit 0 with a warning per null row
   (recommended), or non-zero whenever any row is null? Non-zero makes a
   script notice. It also fails the whole table on the real portfolio until
   issue 58 has priced the history.
5. **Trailing rows end at `--to`** (recommended: one end date for the
   table), or at the last month end on or before it, as most statements
   report? On a portfolio priced only at month ends, a mid-month `--to`
   leaves every row null under the recommendation, each with its reason.

## Owner's answers (2026-11-23)

1. **The break applies at both levels.** An account emptied and later
   re-funded starts a new record.
2. **`pert twr` past a termination** ends at the termination and carries
   both dates.
3. **Annualised beside cumulative** on every row of a year or more.
4. **Exit 0 when some rows are null**, with a warning per null row. When
   no row is measured, the command exits with the first row's refusal.
5. **Every row ends at `--to`.**

## Consequences

- **`pert twr` changes for a period that holds an idle run.** It is now
  refused, or ends at the termination, where it used to print a number. The
  change is deliberate, and `CHANGELOG.md` says so. No existing test covers
  an idle run.
- **`pt init` refuses a fiscal year end that is not a month end.** For an
  existing file, `pert` refuses it when it reads it.
- **No `.port` schema change and no migration.** `schemas/periods-1.0.json`
  is added, and `twr-1.0` becomes `twr-2.0`, shared with ADR 0044.
- **Recorded on acceptance:**
  - `docs/gips-standard.md` amends the `PORT-GIPS-B07` test line (§5),
    records the account-level answer under `PORT-GIPS-H06`, and says under
    `PORT-GIPS-A04` that the fiscal year ends at a month end;
  - ADR 0040's status line records §4 as superseded in part, and §11 as
    settled here.
- **`meta.inception_date` is still not read.** A record's inception comes
  from the ledger (ADR 0040 §2) and from §4.
- **Issue 7's 36 monthly returns** are windows of the same walk, and inherit
  §3 and §4.

## Alternatives considered

- **Call `time_weighted_return` once per row.** The numbers are the same,
  but the walk is repeated about four times, and nothing structural stops a
  later edit from making a row and `pert twr` diverge. Rejected as the
  implementation. It is kept as the differential test.
- **Refuse the whole table at the first unmeasurable row.** One bad month
  anywhere since inception would blank every row. ADR 0044 §3 assumes rows
  stand alone. Rejected.
- **Keep linking across idle runs** (ADR 0040 §4 as written). Rejected:
  `PORT-GIPS-H06`.
- **A year as 365 days, or 365.25.** The first admits a sub-year period in a
  leap year. With either, a 3-year period is not 3 years. Rejected.
- **The current partial year as an annual row.** Its row would change
  every day, against `PORT-GIPS-A04`. It is the year-to-date row instead.
  Rejected.
- **Trailing rows from inception when the record is shorter.** That
  mislabels the figure. Rejected.
- **Store period returns.** ADR 0040 §10 deferred a `return_result` table,
  and it stays deferred until issued reports need held figures
  (`PORT-GIPS-J01`).
