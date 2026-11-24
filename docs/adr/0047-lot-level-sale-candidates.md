# ADR 0047 — Lot-level sale candidates: each open lot priced, taxed and checked for a wash as if sold alone today, and nothing proposed

- **Status:** Accepted (owner, 2026-11-24), with the recommended answer to each of its four questions. See Owner's answers. **Implemented**: `portable_core/services/sale_candidates.py`, `pt lot candidates`, `schemas/lot-candidates-1.0.json`. See As built
- **Date:** 2026-11-24; accepted 2026-11-24
- **Milestone:** v0.2, issue 62 (P1)
- **Governs:** `PORT-GIPS-A09` (the price a lot is valued at); `CLAUDE.md` "Holding period", "Wash sales cross accounts", "Nothing here is tax advice", invariants 6, 9 and 10
- **Depends on:** [ADR 0011](0011-tax-estimation-boundary.md) (what the tax estimate is), [ADR 0017](0017-cutover-reconstruction-and-basis-provenance.md) (basis provenance), [ADR 0021](0021-derived-state-as-of-a-past-date.md) (state as of a date), [ADR 0026](0026-dry-run-rehearses-the-write.md) (dry runs), [ADR 0036](0036-a-lot-is-designated-by-its-lineage.md) (lots designated by key), [ADR 0037](0037-wash-sales.md) (wash sales), [ADR 0041](0041-bonds-at-face-with-accrued-interest.md) (bonds)
- **Relates:** issue 16 (`po`'s tax-aware optimizer, which will select from these rows), issue 36 (rebalancing trades)

## Context

### What is missing

The owner's tax-aware decisions are made lot by lot. "Which lots should I sell?"
needs, for every open lot:
- what it would realize at today's price;
- whether that would be short- or long-term, and when it turns long-term;
- what tax the sale would cost or save;
- for a loss, whether a recent purchase would wash it.

Each piece exists and is tested, but nothing puts them together:
- **`pt lot list`** shows each lot's basis, holding period and days held. It has
  no price, no gain, no tax and no wash check.
- **`pt tax`** estimates the tax on sales already made.
- **The wash-sale matcher** (ADR 0037) pairs a loss with its replacements when a
  sale is applied. It cannot be asked about a sale not yet made.

### The traps

Each of these produces a plausible, wrong number if the report gets it wrong.

- **A stale price.** `pt pnl` and `pt holdings` valued holdings at the newest
  cached price however old, until #74. A sale estimate at a months-old price
  is the same mistake with a tax figure attached.
- **Counting purchases instead of unmatched shares.** Each replacement share
  washes at most one sold share (ADR 0037 decision 3). Shares that already
  washed an earlier loss have been split into their own lots and wash
  nothing more. A report that counted every purchase in the window would
  call washed a loss that is not.
- **Adding up rows.** Two loss lots can each look washed by the same 10
  recent shares. Sold together, only 10 shares wash in all. Each row answers
  "this lot, sold alone", and their sum answers nothing.
- **Average cost.** Under average cost, each share sold carries the averaged
  basis, but the holding period is still decided lot by lot, oldest first
  (`LotEngine._select_average`). Average cost and specific identification
  cannot be mixed for one instrument (`PT-E-TAX-METHOD-CONFLICT`, ADR 0011).
  So for an averaged holding, "sell this lot" is not a sale the owner can
  make.
- **A basis that is not a basis.** A lot seeded at cutover with no derivable
  basis (`basis_source = unavailable`) has a value, not a cost. A gain against
  it is a gain from an arbitrary date (ADR 0017 §2b).
- **A replacement in an IRA.** It does not defer the loss: it loses it for good
  (ADR 0037 decision 5). That is the outcome the owner most needs to see
  before it happens, and it is not the same warning as a deferral.

## Decision

### 1. What it is

**`pt lot candidates`**, beside `pt lot list`. It is read-only and writes
nothing. It proposes nothing. It lists the lots a person, and later `po`
(issue 16), selects from.

- **One row per open long lot in a taxable account**, as of `--as-of`, as
  derived state stood at the end of that day (`state_on`, ADR 0021).
- **Each row is the lot sold alone**, all of it, on `--as-of`, at the price
  §2 allows. It answers what `pt sell --lots @key:qty` would record that day.
  Each row carries that designation (ADR 0036), so the owner can act on it
  directly.
- **Not listed:**
  - short lots, since closing one is a purchase;
  - lots in sheltered accounts, which owe no tax on a sale (but their
    purchases count as replacements, §5);
  - averaged holdings, §6.

  What is not listed is counted in the footnotes, so nothing vanishes.
- **No totals** of tax or wash effects, since the rows are alternatives, not
  parts of one sale (Context). The table says so.

### 2. The price (`PORT-GIPS-A09`)

- **The newest price on or before `--as-of`, valued by `Instrument.value_of`.**
  That handles a bond's quote basis and an option's contract (ADR 0041).
- **It must pass `ValuationEngine.price_status`**, the rule `pt value`,
  `pt holdings` and `pt pnl` share: no older than `staleness_tolerance_days`.
  See question 1.
- **Every row shows the price's date, source and age.** A missing or stale
  price gives a row with its lot facts and null proceeds, gain and tax, and a
  reason. Never a zero.
- **Selling costs are not deducted**, since none is known before the trade.
  The footnote says so.
- **A bond's accrued interest is not proceeds** (ADR 0041). Every row for a
  bond carries `BOND_TAX_NOT_COMPUTED`, as `pt tax` does.

### 3. The holding period

- **The holding period it would have if sold on `--as-of`**, by
  `holding_period()` from the lot's `holding_period_start`. That start already
  carries a wash sale's absorbed days (ADR 0037) and an inherited spinoff
  period.
- **The first day it is long-term**: the day after the anniversary of its
  holding-period start. 29 February clamps as `domain/dates.py` clamps it, so
  a lot from 2024-02-29 turns long-term on 2025-03-01. This is a new public
  function there, `long_term_from(start)`, beside `is_long_term`, and the
  boundary tests hold the two to each other.
- **Days to long-term**: zero once it is long-term.

### 4. The tax (ADR 0011)

- **The estimate `pt tax` would make for the same sale.**
  `TaxEngine.estimate`, on a disposition built and never stored: the allowed
  gain × the account's rate in force on `--as-of` for that holding period.
  It is labelled an estimate. No other arithmetic is written.
- **A missing rate schedule** gives a null estimate, with
  `PT-E-TAX-NO-RATE-SCHEDULE` as the reason, for that account's rows. The rest
  of the report stands.
- **Basis provenance (ADR 0017):**
  - `unavailable` gives a null gain and tax, with the reason;
  - `reconstructed` and `estimated` are computed and marked approximate, as
    `pt tax` marks them.
- **Tax per dollar raised**: the estimate over the proceeds. It is negative
  for a loss, which saves tax.
- **For a short-term lot, the change from waiting until it turns long-term**,
  at today's price, at the rate in force on that day. Positive means waiting
  saves tax. For a loss it is negative, since a short-term loss offsets more.
  See question 3.

### 5. The wash-sale check (ADR 0037)

For a lot that would be sold at a loss:

- **Replacements already made.** These are the unmatched shares, in any
  account of the portfolio, acquired from 30 days before `--as-of` through it,
  of the lot's instrument or one declared substantially identical. They are
  exactly the shares the matcher would pair the sale with. So the selection is
  moved out of `WashSaleMatcher.after_disposals` into one public function,
  which the matcher and the report both call, and the rule is stated once.
  - The row gives the number of shares washed, the loss disallowed, and the
    dates and accounts of the replacements.
  - **A replacement in a sheltered account is marked as losing the loss for
    good**, and that loss is shown apart from a deferral.
  - **A replacement since disposed of** is reported for manual handling, as
    the matcher reports it.
- **The date the window clears**: 31 days after the latest of those
  replacements, provided nothing more is bought.
- **Replacements still to come.** Every loss row says that buying the
  instrument in any account within 30 days after the sale would wash it. That
  includes a dividend reinvestment.
- **Not detected**, as in every tax report: the same `WASH_SALES_NOT_DETECTED`
  list. For a long option lot, options are named as not detected.

### 6. Averaged holdings

An instrument in an account whose sales have used average cost, or whose
default relief method is average cost, is not listed lot by lot. A lot cannot
be chosen from it. It appears once, as a holding the report does not cover,
with the reason. See question 2.

### 7. Output, filters and where it lives

- **`--losses`** shows only lots at a loss.
- **`--sort`** takes one of:
  - `tax-per-dollar`;
  - `days-to-long-term`;
  - `gain`;
  - `acquired`, the default, oldest first, as `pt lot list` sorts.
- **`--account`** and a symbol narrow it, as in `pt lot list`.
- **The disclaimers.** `--format json` is `lot-candidates-1.0`, with
  `TAX_DISCLAIMER` as the envelope's disclaimer. `wash_sales: {detected,
  not_detected}` is carried as in `tax-2.0`.
- **Where it lives.** The logic is in `services/sale_candidates.py`. The
  command is in `portable_pt/commands/positions.py`, and is thin. Nothing in
  `.port` changes.

## Test obligations

- **`test_a_lot_one_day_short_of_long_term_says_so`**: one day short, with
  the first long-term date the day after. The 29 February case is held to
  `is_long_term`.
- **`test_a_loss_with_a_recent_purchase_in_an_ira_is_flagged`**: marked as
  lost for good, not deferred.
- **`test_estimated_tax_matches_pt_tax_for_the_same_sale`**: every row of a
  portfolio built to reach each case is compared with the same sale made by
  `pt --dry-run sell --lots @key:qty` (ADR 0026), on the same day at the same
  price. The cases are a gain, a loss, short-term, long-term, a wash in a
  taxable account, and one in an IRA. The proceeds, gain, holding period,
  estimated tax and disallowed loss must agree. This is what holds the
  report to the sale path it does not run.
- **Replacement shares already matched to an earlier loss wash nothing**:
  a lot whose only recent replacement already washed another loss is not
  flagged.
- **Two loss lots and one replacement**: each row alone shows the wash, and
  the footnote says the rows are not additive.
- **A stale or missing price, an `unavailable` basis, a missing rate**: each
  gives nulls with its reason, never a zero.
- **An averaged holding** is not listed lot by lot, and is named.
- **A bond** is priced through its quote basis and carries
  `BOND_TAX_NOT_COMPUTED`.
- **Determinism**: the same file and arguments give the same bytes.
- **JSON** validates against `lot-candidates-1.0`, and the schema requires the
  disclaimer and the wash-sale lists.

## Open questions for the owner

1. **The price rule.** Recommended: the configured `staleness_tolerance_days`,
   through `ValuationEngine.price_status`. That is the rule `pt value`,
   `pt holdings` and `pt pnl` now share, so the report agrees with the
   holdings beside it, and each row shows its price's age. The alternative is
   `pert`'s fixed five days (ADR 0044). That is right for a measured return,
   which must not change with configuration. A trade decision is made by the
   owner, reading the date beside the price.
2. **Averaged holdings.** Recommended: name them and leave them out (§6),
   rather than show a figure for a sale that cannot be made. Mutual funds
   often default to average cost at a custodian.
   - **Do any of your accounts use average cost?**
   - **If one does,** a later ADR could add a row per averaged holding:
     the gain on its oldest shares at the average basis, split by holding
     period as `_select_average` splits it.
3. **The change from waiting until long-term** (§4). Recommended: include it.
   The issue's "days to long-term" sort is there for this decision, and the
   figure is still gain × rate. It is at today's price, and labelled so. The
   alternative is the days alone.
4. **The command's name.** Recommended: `pt lot candidates`, beside
   `pt lot list`. Alternatives are `pt tax candidates`, which is easily
   confused with `pt tax wash-candidates`, or columns added to `pt lot list`,
   which would make it the slow report.

## Owner's answers (2026-11-24)

1. **The price rule:** the configured `staleness_tolerance_days`, through
   `ValuationEngine.price_status`.
2. **Averaged holdings:** named and left out (§6). Whether any account uses
   average cost is still to be said. It decides only whether a later ADR adds
   a row per averaged holding.
3. **The change from waiting until long-term:** included, at today's price.
4. **The command's name:** `pt lot candidates`.

## As built

- **The equality test makes each sale for real, on a copy of the file.** A
  dry run of `pt sell` shows the basis relieved, the gross amount and the
  holding period, but no gain, tax or wash. So the test does two things:
  - it compares those three with `pt --dry-run sell --lots @key:qty`;
  - it makes the same sale on a copy of the file and compares the proceeds,
    gain, holding period, estimated tax, disallowed loss and allowed gain
    with the row `pt tax` then reports.

  A dry run is that write, rolled back (ADR 0026), so this tests what the
  obligation names, with the figures the dry run does not show. The cases
  are a gain and a loss, short- and long-term, a wash in a taxable account
  and in an IRA, two lots washed by the same shares, an already matched
  replacement, and a reconstructed basis.
- **Every null carries its reason in `unstated`**, as `{reason, message,
  code}`. The code is the error's own where one was raised:
  `PT-E-TAX-NO-RATE-SCHEDULE`.
  - One reason is not named in §2: **`no_market_price`**. ADR 0028 values an
    instrument nothing quotes at zero, at level 5. That zero is a valuation
    convention, not a price a sale would raise, so it gives null proceeds.
- **What waiting changes is taken before any wash sale** (§4), on both
  days. Whether a sale on the later day is washed depends on purchases not
  yet made. The footnote says so.
- **"Whose sales have used average cost" is read across every account**
  (§6). `LotEngine.check_method_consistency` refuses spec-ID for an
  instrument averaged anywhere, so no lot of it can be designated in any
  account.
- **The option and bond statements are per row**, in `notes`, as well as
  in the footnotes.
- **The sort orders.** Unknown figures sort last, and ties go by
  acquisition, then account, symbol and lot key.
  - `tax-per-dollar` puts the most tax saved per dollar first.
  - `gain` puts the largest loss first.
  - `days-to-long-term` counts up from zero, so lots already long-term
    come first.
- **A footnote explains a `Long from` date earlier than the acquisition**:
  a spinoff's shares inherit their parent's holding period, and a wash
  sale's replacement absorbs the days of the shares sold.

## Consequences

- A new command, `pt lot candidates`, and a new schema, `lot-candidates-1.0`.
- **One refactor**, with no change of behaviour: the matcher's choice of
  replacements becomes a public function in `services/wash_sales.py` that both
  callers share. Every wash-sale test must pass unchanged, and the replay test
  is the check.
- `domain/dates.py` gains `long_term_from`.
- No `.port` change, no migration, and nothing written.

## Alternatives considered

- **Dry-run every lot's sale for real** (ADR 0026), and read back what it
  records. It would be exact by construction. But it works only when
  `--as-of` is the ledger's last day, since an earlier date makes the sale an
  out-of-order append and forces a rebuild per lot (ADR 0016). It also opens
  the file for writing to produce a report. Kept as the test, not the
  implementation.
- **Count every purchase in the window as a replacement.** That overstates the
  wash, since matched shares wash nothing more. Rejected.
- **Totals across rows.** They describe no sale the owner could make.
  Rejected.
- **Rank the lots, or propose a sale.** That is `po`'s (issue 16), with an
  objective the owner states. This report gives the inputs and decides
  nothing. Rejected.
- **Value an averaged holding's lots at their own basis.** That basis no
  longer exists under an average-cost election. Rejected.
