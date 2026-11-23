# ADR 0044 — A return over unpriced days is estimated only on request, marked, and never linked into the track record

- **Status:** Accepted (owner, 2026-11-23), with the recommended answers to questions 1–7 as amended in review, and a working limit of 5% (question 8). See Owner's answers. Implemented so far: §2's marker. `ReturnValue.is_estimated` is rendered `ESTIMATED` by the formatter, and `twr-2.0` requires `is_estimated` and `estimate`, which are `false` and `null` until the tier exists. The tier itself is not built. `twr-2.0` is shared with [ADR 0045](0045-multi-period-returns.md), accepted the same day
- **Date:** 2026-11-17; accepted 2026-11-23
- **Milestone:** v0.2, issue 60 (P1)
- **Governs:** `PORT-GIPS-A02`, `A03`, `A09`, `B04`, `B05`, `H04`, `H08`, `J03`, `J04`; `CLAUDE.md` invariants 6, 9, 10
- **Depends on:** [ADR 0024](0024-valuation-snapshots-are-valued-state.md), [ADR 0028](0028-instrument-with-no-market-price.md), [ADR 0031](0031-price-acceptance-against-statements.md), [ADR 0040](0040-time-weighted-return.md)
- **Relates:** issue 58 (reconcile `pert twr` on the real portfolio), issue 5 (multi-period reporting), issue 7 (risk figures)

## Context

### What is refused today

`pert twr` uses a day when every snapshot it needs is complete. A snapshot is
incomplete when a holding has no price on or before the day, or only one
older than `staleness_tolerance_days` (`PT-E-PRICE-MISSING`,
`PT-E-PRICE-STALE`). A bond's accrual failing or a coupon left unrecorded
makes it incomplete too (ADR 0041 §3). The snapshot stores `is_complete`
and `uses_estimates`, but not which holding failed or why.

Unusable days inside a month with no large flow are bridged by Modified Dietz
(ADR 0040 §6, `PORT-GIPS-B06`). The return is refused when:

- the first or last day of the period cannot be valued;
- a gap spans a month end, or a large flow falls inside it;
- a snapshot is missing or no longer re-derives;
- an unresolved statement line blocks the account (ADR 0031).

So the issue's "refused unless every instrument has a price at every month
end" is close, but it is not the whole rule.

### What already produces a return over a day without a current price

1. **The staleness tolerance.** A price up to `staleness_tolerance_days` old
   (default 5) is used as the day's price. The snapshot is complete. The
   return is measured, and each such day is listed in
   `disclosures.stale_price_days`. That list holds every weekend and holiday:
   261 days on the sample. It appears in `data` only, never in a footnote.
2. **A judged estimate.** `pt price set --estimate` records a price with
   `valuation_basis = estimate`. The snapshot is complete and
   `uses_estimates`. The return is measured, and the days are listed in
   `estimated_days` with a footnote. When the final price replaces it,
   `pt validate` names the snapshots that used it (ADR 0024).
3. **ADR 0028's zero**, for an instrument nothing will ever quote.

The second is what `PORT-GIPS-A09` describes. Firms 2.A.21 and Asset Owners
22.A.18 allow the last available price, or an estimated value, to be used as
fair value. It must be *considered* the best approximation of current fair
value, then assessed against the final value, and adjusted when that
arrives. GIPS does not keep estimates out of the track record. It requires
that someone judged the estimate, and that it is corrected.

### The flaw in the framing, and a hole

Issue 60 reads as though any estimate were outside the track record. The
real line falls between two kinds of estimate:

- **a judged estimate**, where someone considered a value the best
  approximation and recorded it. This belongs in the track record, and
  `pt price set --estimate` already provides it.
- **a carried price**, where no one judged anything and a price is simply
  missing. Most of the real portfolio's gaps are of this kind (ADR 0020 R4,
  R5): a fund `duk` does not carry, or a symbol that stopped trading. A
  market price existed on the day. `portable` does not have it.

A carried price is not `PORT-GIPS-A09`'s best approximation. It is the
absence of one. Only this kind needs the new tier.

The hole is that a carried price can already enter the measured series
unmarked. Raise `staleness_tolerance_days` in a config file, and `pt value`
builds complete snapshots on prices months old. `pert twr` then presents a
measured return over them. The `PT-E-PRICE-STALE` remedy and
`docs/market-data.md` both invite this, naming a `--staleness-tolerance` flag
that does not exist. A marked tier is pointless while an unmarked one sits
beside it. `pert twr` also reads that same config value to decide
ADR 0031's price ties. So one `.port` file can give different answers on two
machines.

## Decision

### 1. What may be estimated, from what, and for how long

**`pert twr --estimate MAX_SHARE`**, opt-in, with no default (§4).

- **The estimate is a carried price.** It is the newest of these on or
  before the day:
  - a stored price for the instrument;
  - a fill recorded in the ledger for it in any account (`buy`, `sell`,
    `short`, `cover`). For a bond, a fill is a clean price on the
    instrument's quote basis, like a stored price (ADR 0041), so it carries
    the same way;
  - a reinvested distribution (`dividend_reinvest`), which is a purchase at
    the fund's price on its pay-date. Its price is the recorded amount
    divided by the units it bought. A row with tax withheld is not used
    until it is confirmed that the recorded amount is still what bought the
    units.

  A stored price wins a tie with a ledger row, and between ledger rows on
  one day the later in ledger order wins. The ledger rows are included
  because the instruments that motivate this tier often have no price row
  at all, only the trades and reinvestments that bought and sold them.
  With none of the three, the holding cannot be estimated and the day is
  refused (`PT-E-ESTIMATE-UNAVAILABLE`).
- **Until the next price, with no age cap.** §4 governs materiality. Every
  carried holding is named with the date and age of the price carried, so
  a two-year-old price is visible, not hidden.
- **Never across a corporate action of the instrument.** Prices are
  unadjusted (`CLAUDE.md`). A pre-split price carried past a 2-for-1 split
  values the position at twice its worth. A carry across a split, spinoff,
  conversion or merger ex-date is refused, naming the action.
- **A cash distribution lowers the carried price.** Otherwise the
  distribution counts twice: the snapshot holds it, first as a receivable
  and then as cash, while the carried price still includes it. See
  question 3.
  - **From the first day the row's value enters a snapshot**, the row's
    `values_from`: its ex-date when the snapshot accrues it from there
    (ADR 0040 §13.2), its pay-date otherwise. A row recorded with no
    ex-date takes its pay-date as its ex-date and accrues nothing, so its
    value arrives as cash on the pay-date. Lowering the price from a
    recorded ex-date only would count every such distribution twice from
    its pay-date on. The rule reads the same property the snapshot reads,
    so the two cannot drift apart.
  - **By the gross amount per unit held**, since the price falls by the
    gross. Withholding is the holder's cost, not the fund's.
  - **For every distribution the snapshot accrues except a coupon**:
    dividends, reinvested dividends, capital-gain distributions and return
    of capital. A bond's quoted price is clean and its accrued interest is
    held separately (ADR 0041), so a coupon never lowers it.
  - **A reduction that would take the price to zero or below is refused**,
    naming the distribution and the price carried.
- **It covers one cause only.** It estimates a holding that valuation could
  not price (`PT-E-PRICE-MISSING`, `PT-E-PRICE-STALE`) and nothing else.
  The following still refuse with the flag:
  - a missing snapshot, since `pt value` makes it;
  - a failed accrual or an unrecorded coupon, which is a missing ledger row;
  - an ADR 0031 block, since a break is evidence that a price is wrong, not
    that it is missing;
  - ADR 0040 §4's undefined days and §12's unpriced trades.
- **Only where the measured path refuses.** A gap the Modified Dietz bridge
  can cross (ADR 0040 §6, `PORT-GIPS-B06`) is bridged, exactly as without the
  flag, and stays measured. A carried price values only the days the
  measured path cannot use: the first or last day of the period, a gap that
  spans a month end, and a gap with a large flow inside. Otherwise the flag
  would turn a return that is measurable without it into an estimate, which
  is the one thing it must never do (§2).
- **Within such a gap, only the days the bridge needs are estimated.** These
  are:
  - the first or last day of the period;
  - each month end inside the gap;
  - the valuation a large flow inside the gap requires (ADR 0040 §6): the
    day before a large inflow, which counts from the start of its day, and
    the day of a large outflow.

  What is left of the gap is bridged by Modified Dietz exactly as without
  the flag. So the estimated share is as small as it can be, and every day
  that can be measured is.
- **The hole is closed in `pert`, not in `pt`.** `pert` treats a stored
  snapshot price older than **five calendar days** as carried, whatever
  `staleness_tolerance_days` was when the snapshot was built. Five is
  today's default: it covers weekends and market holidays, and nothing
  changes for a file built on the default. Without `--estimate`, such a day
  is unusable. With it, the day is estimated. ADR 0031's tie check in `pert`
  uses the same fixed value. `pt value` and `pt holdings` keep reading the
  config. See question 7.
  - When a day is unusable only because of this, the refusal says so. It
    names the price's age, the five days `pert` applies, and the
    `staleness_tolerance_days` in force. Otherwise `pt holdings` calling a
    snapshot complete while `pert` refuses it would go unexplained.
- **Computed on demand and stored nowhere.** `pert` stays read-only (ADR
  0040 §2, §10). It already re-derives every snapshot in the period to check
  it (ADR 0024), so that walk supplies the holdings of an unusable day, and
  which of them failed and why. The estimate depends only on the file and
  the command line (invariant 6).

### 2. How an estimate is marked

The marker travels with the value, as `is_supplemental` does
(`PORT-GIPS-H08`). The formatter renders it: a call site cannot omit it.

- **`ReturnValue.is_estimated`.** `render_return` appends `ESTIMATED` in
  `table` and `markdown`, for example
  `36.68% (TWR, net-of-fees, ESTIMATED)`.
- **`TimeWeightedReturn.estimate`** holds:
  - the limit;
  - the peak estimated share, and its day;
  - every estimated day, listing each carried holding: account, symbol,
    quantity, the price carried and its date, its source (a price id or a
    fill's `txn_id`), its value, and its share.

  Each `SubPeriod` carries `is_estimated`.
- **Table.** The title begins `ESTIMATED`. A footnote states the number of
  days and holdings, the peak share and the limit, the age of the oldest
  price carried, and that the figure is neither measured nor part of the
  track record. The same sentence goes to stderr as a warning. The age is
  in the sentence, not only in JSON, because the share limit bounds how
  much is estimated and only the age shows how long it had to drift.
- **JSON: `twr-2.0`, a major version**, shared with ADR 0045, which changes
  the same schema (`requested_end`, two refusals, `idle_days` dropped). Its
  `periods-1.0` carries `is_estimated` from its first version, so neither
  schema needs a second major version. Every return object requires
  `is_estimated`, which is `false` when measured. The top level requires
  `estimate`, which is `null` when nothing was estimated. The version is
  major because a `1.0` consumer reading `returns[].value` would take an
  estimate for a measurement. Consumers pin `schema_version`
  (`docs/output-formats.md`), so the bump is what makes them notice. Measured
  output moves to `2.0` too: one schema per command, as `tax-2.0` did.
- **The flag permits an estimate. It never marks a measurement.** With
  `--estimate` and nothing to estimate, `estimate` is `null` and the output
  is byte-identical to the run without the flag.

### 3. An estimate is never linked into the track record

- **One rule, in one place.** A result is estimated if any of its
  sub-periods is. That is decided where sub-periods are linked
  (`PORT-GIPS-B04`), never at a call site. `PORT-GIPS-J04` sets the same
  guard between theoretical and actual results.
- **Nothing estimated is stored**: no price, no snapshot, and later no
  issued report (`PORT-GIPS-J01`). Nothing downstream can consume an
  estimate except by running with the flag again.
- **Multi-period reporting (issue 5).** Each period row is computed on its
  own. Under `--estimate`, a row with no estimated day is measured and shown
  plainly. A row with one is `ESTIMATED`. Without the flag, an unmeasurable
  row is `null` with its reason, and never estimated.
- **Since inception is estimated whenever any day since inception is.**
  Until issue 58 prices the history, every since-inception and long trailing
  figure on the real portfolio is an estimate. This tier makes the portfolio
  usable for managing in the meantime. It does not shorten issue 58, which
  remains the only route to a measured track record.
- **Risk figures (issue 7) and MWR (issue 4) inherit the rule.** A
  three-year standard deviation using any estimated month is estimated. So
  is an MWR whose beginning or ending value is. `PORT-GIPS-F02`'s
  disclosure of an absence applies to the measured figure, and
  `PORT-GIPS-B07` applies to both.

### 4. How much may be estimated

- **The measure.** On each day, the carried value is divided by the ending
  value of the level being computed (the account or the portfolio). If any
  day exceeds the limit, the return is refused, naming the day, the share
  and the limit (`PT-E-ESTIMATE-LIMIT`). A day whose ending value is zero or
  negative has no share and is refused. This follows `level5_share`, which
  is `null` there.
- **Where the limit lives.** Four options:
  - **(a) A stored, effective-dated `return_policy` column.** This matches
    the large-flow threshold (`PORT-GIPS-B03`), but the two are different
    things. The large-flow threshold is valuation practice on the day
    measured. The limit is the reader's tolerance for the question asked
    today. Dated by the measured day, estimating 2022 would need a policy
    row dated 2022. ADR 0025 would treat writing it as restating history,
    on rows whose large-flow values stored snapshots depend on, for a figure
    that is never stored.
  - **(b) One stored setting, undated.** The draft said this needs a schema
    change. It does not: the `.port` file has had a key/value `settings`
    table since schema 0001, and nothing reads it yet. It is rejected for a
    different reason. The same command would answer differently after the
    setting was edited, with nothing on the command line to show why.
  - **(c) A required value of the flag** (chosen). `--estimate 0.05`
    has no default, so `CLAUDE.md`'s rule that a missing threshold is an
    error, not a zero, holds. The value is echoed on the result, so an
    estimate is reproducible from the file and the command line.
  - **(d) The layered config.** Rejected: the same file would give
    different answers on different machines, the defect §1 removes.

## Test obligations

- **Without the flag, nothing changes at the default tolerance.** Every
  existing `pert twr` test passes unmodified. One case is a deliberate
  change, so the issue's criterion is narrowed: with
  `staleness_tolerance_days` raised, a day priced more than five days stale
  is now refused rather than measured. A test asserts that.
- **The flag alone changes nothing.** With `--estimate` on a period that is
  measurable without it, the output is byte-identical to the run without the
  flag. That includes a period whose gaps the Modified Dietz bridge crosses:
  they are bridged, not estimated.
- **Every estimated sub-period is named.** Every day valued with a carried
  holding appears in `estimate.days`, with each holding.
- **The marker cannot be dropped:**
  - an estimated `ReturnValue` renders `ESTIMATED` in `table` and `markdown`;
  - `twr-2.0` rejects an estimated document missing `is_estimated` or
    `estimate`;
  - a property test shows that linking any estimated sub-period gives an
    estimated result.
- **The refusals:**
  - a day over the limit;
  - a non-positive day;
  - a carry across a split;
  - a holding with no price and no fill;
  - an ADR 0031 block, and an accrual failure, each with the flag set.
- **The cash-distribution reduction** is checked against hand arithmetic:
  - from the ex-date for a row that accrues;
  - from the pay-date for a row recorded with no ex-date;
  - never for a coupon;
  - refused when it would take the price to zero.
- **A reinvested distribution is a last known price.** A row with tax
  withheld is not used.
- **Only the days the bridge needs are estimated.** Take a gap that spans a
  month end with one unpriced month-end day. Only that day is estimated,
  and the rest of the gap is bridged.
- **The table footnote states the oldest carried age.**
- **A day unusable only because of `pert`'s fixed five days** has a refusal
  that names its age, the five days, and the configured tolerance.

## Open questions for the owner

1. Accept §§1–4 as recommended?
2. **The limit:** a required flag value (recommended), or a stored setting?
3. **A cash distribution inside a carry:** reduce the carried price by the
   per-unit amount (recommended), or refuse the carry? Refusing makes a
   monthly-distributing fund impossible to estimate past one month.
4. **Fills as a last known price:** recommended yes.
5. **The bridge first:** a gap the Modified Dietz bridge can cross stays
   measured, and only what it cannot cross is estimated. Recommended, so that
   the flag never makes a measurable return an estimate.
6. **An age cap on a carried price:** recommended none, with every age shown.
7. **The fixed five-day tolerance in `pert`:** recommended. This changes
   results for anyone who raised the config.
8. **The limit you expect to use.** That number is yours, and no
   recommendation is made.

## Owner's answers (2026-11-23)

1. **§§1–4:** accepted. Review made five amendments, all folded into the
   text above:
   - answers 3 and 4 below;
   - the bridge's days (answer 5);
   - the oldest carried age in the footnote (answer 6);
   - the refusal naming both tolerances (answer 7).

   Review also corrected §4 (b): a stored setting needs no schema change.
2. **The limit:** a required value of the flag, with no default.
3. **A cash distribution inside a carry:** lower the carried price rather
   than refuse. The draft lowered it from the ex-date. Review found that a
   row recorded with no ex-date takes its pay-date as its ex-date, so the
   reduction runs from the row's `values_from`, the day the snapshot first
   holds the distribution. It applies to every distribution the snapshot
   accrues except a coupon, at the gross amount per unit, and is refused if
   it would take the price to zero or below.
4. **Fills as a last known price:** yes. Reinvested distributions count as
   well, except a row with tax withheld until its amount is confirmed as the
   cost of the units. A bond fill is a clean price on its quote basis, so
   it carries like a stored price.
5. **The bridge first:** yes. Within a gap the bridge cannot cross, only the
   days it needs are estimated.
6. **No age cap.** Every age is shown, and the oldest is stated in the table
   footnote and the warning.
7. **The fixed five days in `pert`:** yes. A day refused only for this says
   so, naming both numbers.
8. **The limit the owner expects to use: 5%**, `--estimate 0.05`. This is a
   working value, not a default. The flag still requires it (answer 2), and
   every estimated result echoes it.

## Consequences

- No `.port` schema change and no migration, under §4 (c).
  - `schemas/twr-2.0.json` is added, shared with ADR 0045. `ReturnValue`
    gains a field, and the three error codes above are added.
  - `PORT-GIPS-A09` in `docs/gips-standard.md` now says a mechanically
    carried price is not its best approximation.
- The `PT-E-PRICE-STALE` remedy and `docs/market-data.md` stop naming a flag
  that does not exist.
- The judged estimate (`pt price set --estimate`) is unchanged. It stays in
  the measured series, as `PORT-GIPS-A09` permits.

## Alternatives considered

- **Write carried prices into the price table** (a bulk
  `pt price estimate`). Snapshots would become complete, and the carry would
  enter the track record marked only as a judged estimate would be. That
  is the hole above, made routine. Rejected.
- **Store estimated snapshots** (`pt value --estimate`). This would add a
  derived class to replay and `pt validate` (invariant 3), and stored
  state that reads as complete. Rejected.
- **Interpolate between the prices either side of the gap.** It invents a
  path, reads prices from after the day, and makes a gap look smooth.
  `PORT-GIPS-A03`'s test refuses interpolation for measured returns. It is
  no better as an estimate. Rejected.
- **Value an unpriced holding at zero.** ADR 0028 §3 refuses that for an
  instrument that has ever had a price, and zero claims a total loss.
  Rejected.
- **Leave the raised staleness tolerance as the estimate tier.** It is
  unmarked. Rejected.
- **Price the gaps from the custodian's statements.** These are observed
  values, not estimates. Loaded with `pt price set` at the level they
  deserve, they make the day measured. That is issue 58's work, not this
  tier's.
