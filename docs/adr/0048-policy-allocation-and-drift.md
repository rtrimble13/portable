# ADR 0048 — Policy allocation and drift: declared classes, an effective-dated target, and drift at the market value a snapshot records

- **Status:** Implemented (2026-11-25). Accepted by the owner the same day, with the recommended answer to each of its five questions: see Owner's answers, and As built for where the implementation differs from the text above it
- **Date:** 2026-11-24; accepted and implemented 2026-11-25
- **Milestone:** v0.2, issue 61 (P1)
- **Governs:** `PORT-GIPS-A06` (accrued income is part of value), `PORT-GIPS-A07` (cash is in the total), `PORT-GIPS-A09` (the price a holding is valued at); `CLAUDE.md` invariants 3, 6, 9 and 10
- **Depends on:** [ADR 0010](0010-derived-state-and-replay.md) (config rows are effective-dated, not edited), [ADR 0013](0013-cash-sweep-is-cash.md) (a sweep is cash), [ADR 0021](0021-derived-state-as-of-a-past-date.md) (state as of a date), [ADR 0024](0024-valuation-snapshots-are-valued-state.md) (what a snapshot values), [ADR 0025](0025-config-rows-dated-into-history.md) (corrections on a row's own date), [ADR 0028](0028-instrument-with-no-market-price.md) (zero at level 5), [ADR 0041](0041-bonds-at-face-with-accrued-interest.md) (accrued interest), [ADR 0044](0044-estimated-returns-over-unpriced-days.md) (carried prices, on request)
- **Relates:** issue 36 (rebalancing trades, which will read this), issue 16 (`po`'s tax-aware optimizer)

## Context

### What is missing

Nothing answers "am I where I meant to be?"
- **`pt holdings --by asset-class`** groups holdings by
  `instrument.asset_class`. That is free text, typed by hand, optional, and
  read by nothing that computes (`reference_data.INSTRUMENT_FIELDS`).
  Issue 61 found 83.68% of the sample's market value without one. The
  sample's instruments now carry `us_equity` and `us_treasury`, but nothing
  checks a value, dates it, or requires one.
- **There is no target** to compare a holding against, so there is no drift
  and no band to be outside of.

### The traps

Each of these produces a plausible, wrong weight.

- **A weight over part of the total.** `pt holdings` divided each holding by
  the total of the holdings it could price, until `holdings-2.0`. A missing
  or stale price left its holding out of the total, so every other weight
  rose, and the column still summed to 100%. A drift report built that way
  would show drift that is not there, and hide drift that is.
- **A total that is not the market value.** Until `holdings-2.0`,
  `pt holdings` valued a bond at its clean price, without its accrued
  interest, and left out accrued dividends. The snapshot `pt value` records
  includes both (`PORT-GIPS-A06`, ADR 0041). So the two totals differed by
  the accrued income, and a weight over the smaller one overstated
  everything else. On the sample at 2025-12-31, `pt holdings` totalled
  819,411.48 and `pt mark` 819,681.51. The 270.03 between them is the
  Treasury's accrued interest.
- **Unclassified folded into a class.** A holding with no class put into
  "other", or into the largest class, makes drift look settled when it is not
  known.
- **A blended fund counted as one class.** A 60/40 balanced fund or a
  target-date fund counted as all equity overstates equity by 40% of its
  value.
- **A policy that is not reproducible.** Targets in `~/.portablerc` could not
  be recovered for a report as of two years ago. That is why `return_policy`
  lives in the `.port` file, effective-dated.
- **A policy change that restates the past.** Drift on 2025-06-30 must be
  judged against the policy in force that day, not the one adopted since.
- **An internal transfer read as drift.** Moving cash from the brokerage
  account to the IRA changes each account's mix and not the portfolio's.

## Decision

### 1. The classes

- **The owner's list, stored in the file**: a name and a description for
  each class. Nothing is inferred from a sector, a provider's label or an
  instrument type.
- **`cash` is built in.** Every portfolio has it, and it cannot be removed.
  It holds each account's cash balance less its margin loan, and a sweep
  vehicle is cash (ADR 0013). An instrument such as a money-market fund held
  as a security can be classified into it.
- **"Unclassified" is not a class.** It is a gap, named in every report
  (§5).

### 2. The classification

- **Every instrument the portfolio holds is classified by declaration**,
  like a substantially identical pair (ADR 0037): into one class, or across
  several by stated weights that sum to exactly 1. See question 3.
- **Effective-dated**, one set per instrument per date, with `--corrects`
  for a row entered wrongly on its own date (ADR 0025 decision 5). Drift as
  of a date uses the classification in force that day.
- **An option is classified like any instrument**: by the owner, not from
  its underlying.

### 3. The policy

- **A stored, effective-dated object**, the pattern `return_policy` follows.
  For each class it holds a target weight and a band (question 2).
  - **Targets sum to exactly 1**, cash included, or the row is refused. A
    class may have a target of zero.
  - **One policy per date**, and `--corrects` for a row entered wrongly
    (ADR 0025 decision 5).
  - **A missing policy is an error, not a zero**:
    `PT-E-ALLOCATION-NO-POLICY`, exit 4.
- **A change of policy does not restate past drift.** Drift as of a date
  uses the policy in force that day.
- **No recorded figure is derived from a policy**, so ADR 0025's refusal
  does not apply: a policy dated into the past changes no stored row. Every
  drift report names the policy row it read, so a re-run after a backdated
  entry can be told apart.
- **Portfolio level.** See question 1.

### 4. The market value (`PORT-GIPS-A06`, `A07`, `A09`)

- **Each account is valued as `pt mark` values it on `--as-of`**:
  `snapshots.value_range`, the builder `pt value` stores from, run against
  state as of that day (ADR 0021) and never stored. The report's total for
  each account is the snapshot's ending market value: securities, plus cash,
  less the margin loan, plus accrued income.
  - **A bond's accrued interest goes with the bond's class.** It is part of
    the bond's value, and its clean price omits it.
  - **Accrued dividends go to `cash`.** A dividend receivable is money owed
    on a known date, not exposure to the paying security, whose price has
    already fallen by it.
  - **Operating cash** (an account whose `cash_treatment` excludes it) is
    excluded, as the snapshot excludes it, and named.
- **Market value, not exposure.** An option is held at its market value,
  and a short position at its negative market value, in its own class.
  Notional and delta exposure are `risky`'s, and the report says it uses
  neither.
- **An unpriced holding makes every weight unknown.** See question 4.
- **An instrument with no market price** (ADR 0028) is valued at zero, at
  level 5, as the snapshot values it. That is complete, not unpriced.

### 5. The drift report

**`pt allocation drift`**, as of `--as-of`, read-only.

- **Portfolio level.** For each class in the policy:
  - the target, the band, the market value and the actual weight;
  - the difference in percentage points;
  - whether it is outside its band, and on which side. A weight exactly on
    the edge of its band is inside.
- **Per account.** Each account's own mix, and its contribution to each
  class's portfolio weight. The contributions sum to the portfolio weight. An
  internal transfer moves contributions between accounts, and no portfolio
  weight.
- **Unclassified holdings are named** with their market value and their
  share of the total. They count in the total, so every class weight is a
  share of the whole portfolio, and the report is marked incomplete
  (`is_complete: false`) while any is held.
- **A class held but not in the policy** is listed with its weight and no
  target, and marks the report incomplete.
- **The policy row, and each classification date it read**, are named.
- **JSON** is `allocation-drift-1.0`. Weights are full precision in `json` and
  `csv`, and percentages in `table` and `markdown`.
- **No trade is proposed.** That is issue 36.

### 6. Commands

The noun is `pt allocation`, since `pt policy` is the return policy.

- `pt allocation class add NAME --description TEXT`, and `pt allocation class list`.
- `pt allocation classify SYMBOL CLASS[=WEIGHT] ... --effective-from DATE [--corrects ID]`.
- `pt allocation target set CLASS=WEIGHT:BAND ... --effective-from DATE [--corrects ID]`, and `pt allocation target show`.
- `pt allocation drift [--account A]`.

Every writer supports `--dry-run` and `--yes`.

### 7. Where it lives

- **The logic** is in `services/allocation.py`, and the SQL in
  `persistence/`.
- **Schema 13** (`0013_policy_allocation.sql`) adds three tables:
  - `asset_class`, with the built-in `cash` row;
  - `instrument_classification` (instrument, class, weight, effective date,
    `corrects_id`);
  - `allocation_policy` and its rows per class (target, band, effective date,
    `corrects_id`).

  CHECK constraints hold each weight between 0 and 1. A trigger refuses a
  classification set or a policy whose weights do not sum to 1. The
  migration is tested up and down, and the version bump and its changelog
  entry are made in the same commit.
- **The sample** gets a classification for every instrument and a policy, so
  `pt allocation drift` runs on the committed file. A test checks one of its
  weights against hand arithmetic from `sample_prices.csv`.

## Test obligations

From issue 61:
- **`test_policy_allocation_is_effective_dated`**: a policy from 2025-07-01
  does not change drift as of 2025-06-30.
- **`test_unclassified_holdings_are_named_not_bucketed`**.
- **`test_drift_as_of_a_past_date_uses_the_holdings_and_policy_of_that_date`**.
- **A missing policy is an error, not a zero.**
- **The JSON schema** is published and validated.

And from this ADR:
- **`test_the_total_is_the_snapshot_market_value`**: each account's total
  equals the ending market value `pt mark` gives for that day, with a
  bond's accrued interest and an accrued dividend in it, and each in the
  class §4 puts it in.
- **`test_an_internal_transfer_moves_no_portfolio_weight`**.
- **A split classification**: a 60/40 fund contributes 60% of its value to
  one class and 40% to the other.
- **Weights that do not sum to 1 are refused**, for a policy and for a
  classification.
- **The band edge**: a weight exactly on the edge is inside, and one cent
  beyond it is outside.
- **An unpriced holding**: as question 4 decides.
- **The migration**, up and down, on a file with free-text classes (question
  5).
- **Determinism**: the same file and arguments give the same bytes.

## Open questions for the owner

1. **Where targets live.** Recommended: **one target for the portfolio**,
   with each account shown against it by its contribution. Asset location
   deliberately makes accounts differ: bonds in the IRA, equities where gains
   are taxed at long-term rates. Per-account targets would flag that as
   drift. The alternative is a target per account as well. It can be added
   later without changing this schema's shape.
2. **Bands.** Recommended: **an absolute band per class, in percentage
   points** (for example, equity 60% ± 5). It is the simplest to read, and a
   small class can be given a tighter band. The alternatives:
   - a relative band (± 20% of target);
   - the common "5/25" rule: the lesser of 5 points and 25% of target.
3. **Funds that hold more than one class.** Recommended: **allow a split by
   stated weights** that sum to 1, and **date the classification**, so a
   target-date fund's glide path can be followed without restating the past.
   The alternative, one class per instrument, overstates one class by the
   rest of the fund.
   - **Do you hold balanced, target-date or other multi-asset funds?** If
     not, splits cost nothing and wait unused.
4. **An unpriced holding.** Recommended: **refuse by default**, naming the
   holdings and their newest prices (`PT-E-ALLOCATION-UNPRICED`, exit 5),
   because its value is in no weight and so every weight is wrong. With
   `--estimate MAX_SHARE` the holding is valued at a carried price (ADR 0044),
   only if the carried value is at most that share of the total, and the
   report is marked estimated. Drift is a decision aid, and you work with 0.05
   for returns. The alternative is to show weights over the priced holdings,
   marked incomplete, which is the trap in the Context.
5. **The existing free-text `asset_class`.** Recommended: **seed the
   classification from it**. Each distinct value becomes a class, and each
   instrument that has one is classified into it at weight 1 from the
   portfolio's inception. You typed those values, so this records a
   declaration you already made rather than inferring one. After that,
   `pt instrument update --asset-class` is refused with a pointer to
   `pt allocation classify`, so there is one meaning of "asset class", and
   `pt holdings --by asset-class` reads the classification. The alternative
   is to start empty and leave the column as a description. That gives two
   different answers to "what class is this?".

## Owner's answers (2026-11-25)

1. **Where targets live:** one target for the portfolio. Each account is shown
   by its contribution to each class's portfolio weight, and has no target of
   its own.
2. **Bands:** an absolute band per class, in percentage points.
3. **Funds that hold more than one class:** split by stated weights that sum
   to exactly 1, and the classification is dated. Whether any such fund is
   held was not said. Splits cost nothing unused.
4. **An unpriced holding:** refused by default (`PT-E-ALLOCATION-UNPRICED`,
   exit 5), naming each holding and its newest price. `--estimate MAX_SHARE`
   values it at a carried price (ADR 0044), only within that share of the
   total, and marks the report estimated.
5. **The existing free-text `asset_class`:**
   - **The classification is seeded from it.** Each distinct value becomes a
     class, and each instrument with one is classified into it at weight 1
     from the portfolio's inception.
   - **One meaning of "asset class".** `pt instrument update --asset-class` is
     then refused, with a pointer to `pt allocation classify`.
     `pt holdings --by asset-class` reads the classification.
   - **Holdings follow drift.** With that, holdings follows §4 for a declared
     dividend: its `accrued_dividend` line groups under `cash`, not under the
     paying security, so the two reports agree.

## As built (2026-11-25)

Released in 0.3.0. Where the implementation differs from the decision as
written, or settles what it left open:

1. **Five tables, not three.** A classification is a header row
   (`classification`) with one row per class (`classification_part`), and a
   policy likewise (`allocation_policy`, `allocation_target`). A split is
   then rows, not a repeated date, and a correction replaces the whole set.
2. **Weights are checked in `Decimal`, in the service, not by SQL.** §7 asked
   for CHECK constraints and a trigger on the sum. SQLite would compare the
   canonical decimal `TEXT` as text, or sum it as `REAL`, which invariant 1
   forbids. So `services/allocation.py` checks each share, target and band,
   and that they sum to exactly 1, when it writes and again when it reads.
   The triggers hold what SQL can hold exactly: rows are never edited, a
   correction names a row of the same key, `cash` stays, a name is fixed.
3. **`--corrects` is a flag, not an id.** One row per instrument and date (one
   per date for the policy), so the row to replace is the one on the date
   given, as `pt policy set` and tax rates have it (ADR 0025).
4. **The seed is the migration's own SQL**, stamped with `:applied_at`, which
   the runner now binds from its injected clock (ADR 0035). The migration is
   additive, so down is dropping its five tables; the test holds every
   existing row unchanged, which is what that direction needs.
5. **An export written before schema 13** imports with nothing classified.
   Drift then names every holding as unclassified rather than refusing,
   since the gap is the owner's to close with `pt allocation classify`.
6. **`pt allocation drift --estimate MAX_SHARE`** is answer 4. It carries a
   price only for a missing or stale price. A bond's accrued interest after
   an unrecorded coupon is not a price, so that refusal stands with
   `--estimate`.
7. **A class the policy names and nothing holds** is a row at zero, outside
   its band if the band does not reach zero. A class held and named by no
   target is a `no_target` row.
8. **`pt instrument add` refuses `--asset-class` too**, as `update` does
   (answer 5), with a usage error (exit 2) pointing to
   `pt allocation classify`. `pt instrument show` shows the classification
   in force on `--as-of`. The free-text column is kept for files written
   before schema 13, and nothing writes it.
9. **`pt holdings --by asset-class` splits a holding** by its weights: each
   class gets its weight of the market value, the cost and the unrealized
   gain. An unclassified holding groups as `(unclassified)`, so the groups
   still sum to the total.
10. **The sample** declares `us_equity` and `us_treasury` through the
    service, classifies every instrument from inception, and sets a policy.
    `tests/integration/test_sample_allocation.py` works two of its weights
    on 2025-12-31 by hand.

## Consequences

- **Schema 13**: three new tables and a migration, tested in both directions
  (ADR 0019).
- **New commands** under `pt allocation`, and a new schema,
  `allocation-drift-1.0`.
- **`pt holdings --by asset-class`** reads the declared classification
  (answer 5). An `accrued_dividend` line groups under `cash`, as §4 places it
  in drift, and an `accrued_interest` line under its bond's class.
- **The two traps in `pt holdings`** recorded in the Context (weights over a
  partial total, accrued income left out) were fixed in their own change,
  `holdings-2.0`, before this ADR was decided. `pt holdings` now values
  through the snapshot's functions, as §4 asks of drift.
- **Issue 36's rebalancing** reads this report's figures and policy, and
  proposes trades against them.

## Alternatives considered

- **Use `instrument.asset_class` as it is.** It is free text, undated and
  optional, and read by nothing that computes. It cannot follow a fund that
  changes, and a typo makes a new class. Rejected, though its values seed the
  classification (question 5).
- **Infer classes from sector or instrument type.** A bond fund is a
  `mutual_fund` and an ETF can hold anything. Classification is declared,
  never inferred, as substantially identical securities are. Rejected.
- **Targets in configuration.** Not reproducible for a past date. Rejected,
  for the reason `return_policy` lives in the file.
- **Value holdings as `pt holdings` does.** That gives a total that omits
  accrued income and a weight over the priced holdings only. Rejected: the
  report values through the snapshot builder.
- **Fold unclassified holdings into "other".** That hides the gap the report
  exists to show. Rejected.
- **Weight by exposure** (delta, notional). That is a risk measure, and
  belongs to `risky`. Rejected here.
