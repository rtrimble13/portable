# ADR 0050 — Benchmarks are built, not only read: a `.bmk` file holds units of securities, and its total return is computed from their prices and every distribution

- **Status:** Accepted (owner, 2026-11-27): questions 1–4 and 6–10 as recommended, question 5 from fafnir's daily market cap, `duk mc`. Phases 1 and 2 implemented 2026-11-27, released in 0.4.0; phase 3 to come. See Owner's answers and As built
- **Date:** 2026-11-27
- **Milestone:** v0.2, issue 6 (P1)
- **Governs:** `PORT-GIPS-G01` (total return only), `G02` (a benchmark or a stated reason), `G03` (a blend is a stored object with an explicit rebalancing rule), `G04` (changes disclosed), `G05` (periodicity and withholding), `J04` (theoretical is never linked to actual); `CLAUDE.md` invariants 1, 3, 6, 9 and 10
- **Amends:** [ADR 0006](0006-fafnir-access-path.md) §1 (no total return from fafnir), if question 3 is answered as recommended
- **Depends on:** [ADR 0005](0005-decimal-representation-and-rounding.md) (Decimal), [ADR 0010](0010-derived-state-and-replay.md) (derived state is rebuilt from its source), [ADR 0030](0030-default-benchmark-is-cash-at-zero.md) (cash at 0% until a benchmark is assigned), [ADR 0032](0032-fafnir-through-duk-alone.md) (fafnir through `duk` alone), [ADR 0035](0035-every-stored-stamp-is-read-from-an-injected-clock.md) (stamps), [ADR 0048](0048-policy-allocation-and-drift.md) (policy targets)
- **Relates:** issue 7 (risk figures, which need a benchmark leg), issue 11 (the summary), `po` and `risky` (factor indexes)

## Context

### What exists

- **Storage.** Schema 1 already has the tables:
  - `benchmark`, whose `return_type` is `NOT NULL` with no default;
  - `benchmark_component`, `benchmark_level` and `portfolio_benchmark`.
- **One resolver.** `services/benchmarks.py` resolves the benchmark in effect
  on a date (ADR 0030). With none assigned it is `Cash (0%)`, labelled as
  exactly that. `pt benchmark show` prints it.
- **One reader.** `FileProvider.benchmark_series` reads a series in the
  format of [docs/benchmark-series.md](../benchmark-series.md), refusing one
  that does not declare its return type.

### What is missing

- **No command assigns a benchmark.** ADR 0030 left that, and the import of
  levels, to "the owner's benchmark tool". The tool does not exist.
- **`pert` shows no benchmark.** Neither `pert twr` nor `pert periods`
  puts a benchmark beside its return, so `PORT-GIPS-G02` is met only by
  `pt benchmark show`, not by the reports that state a return.
- **Nothing can build a series.** A blended policy benchmark (`G03`), an
  equal-weighted basket, or a synthetic index for factor work all need
  levels computed from constituents. Today they can only arrive as a finished
  file from outside.

### The owner's direction (2026-11-27)

The owner wants a benchmark tool inside `portable`:
- **Its output is a portfolio object**, as `pt` produces. It tracks the
  weights of securities rather than real positions, without `pt`'s
  transactional accounting.
- **Any number of securities.**
- **Many weighting and rebalancing methods**: price-weighted, market-cap
  weighted, equal-weighted, custom-weighted, and others.
- **The file is a `.bmk`.** The tool was suggested as `bt`.
- **It will also build synthetic market indexes for factor modelling.**

### The traps

1. **A total return built from prices plus dividends is the trap ADR 0006
   names.** Leave out one dividend and the index understates itself, quietly,
   for every period after. A price-only series used as a benchmark flatters
   the portfolio by roughly the dividend yield every year (`G01`).
2. **Weights alone cannot be held between rebalances.** A 60/40 blend that is
   not rebalanced is not 60/40 a month later. What an index holds between
   rebalances is a number of units of each security, and its weights follow
   from their prices. A file of weights that is read as constant has
   rebalanced daily without saying so (`G03`).
3. **A split must not move a price-weighted index.** A price-weighted level
   is the sum of prices over a divisor. A 2-for-1 split halves one price, so
   the divisor must change on the ex-date, or the index falls by half that
   security's weight.
4. **A delisting needs a last price.** A constituent that stops trading
   leaves the index at some price. Valuing it at the last close seen, or at
   zero, without saying so, is a guess.
5. **Rebalancing and reconstitution are different events.** A rebalance
   resets weights among the same constituents. A reconstitution changes the
   constituents. A file that confuses the two cannot be audited.
6. **A synthetic index is theoretical.** It is a benchmark, or a factor, not
   performance anyone earned. It must never be chained onto the portfolio's
   track record (`J04`).

## Decision

### 1. A new CLI, sharing `portable_core`

- **A standalone CLI, `bmk`**, alongside `pt`, `pert`, `po` and `risky`
  (answer 1).
- **Thin**, as `pt` is. The logic is in `portable_core`, so `pt` and
  `pert` can read a `.bmk` file without importing the CLI (`CLAUDE.md`: no
  CLI imports another).
  - `domain/index.py` for the objects;
  - `services/index_construction.py` for the arithmetic;
  - `persistence/` for the SQL;
  - `schema/bmk/` for its DDL and migrations.

### 2. A `.bmk` file is SQLite, like a `.port`, with its own schema

It follows every convention of the `.port` format:
- Decimal `TEXT`;
- stamps from the injected clock;
- versioned migrations, with its own `schema_version`;
- a generated sample, `examples/sample.bmk`, held to a fresh build.

It holds three kinds of rows:
- **The definition**, entered by the owner and never edited, with
  corrections on a row's own date ([ADR 0025](0025-config-rows-dated-into-history.md)):
  - the constituents, named as `pt` names instruments, through symbol
    history;
  - the weighting method and its parameters;
  - the rebalancing rule;
  - every reconstitution, dated;
  - the base date and base level.
- **The inputs**, each with its source and as-of:
  - unadjusted closes, splits and cash dividends;
  - for `cap`, each constituent's market cap on each rebalance date, with
    the count, the share factor and the balance sheet behind it.

  The file is self-contained: it rebuilds without a network, and two
  builds of the same file give the same bytes.
- **Derived state**: the units held each day, the divisor, the price level
  and the total-return level. A rebuild reproduces it from the definition and
  the inputs, and a test holds every fixture to that, as invariant 3 holds
  the `.port`.

### 3. An index holds units, and its weights follow from them

- **On a rebalance date**, at that day's close, the method sets each
  constituent's target weight. The tool converts the weights to units at the
  close: units = weight × level ÷ price.
- **Between rebalances the units are constant.** The weights drift with
  prices, which is what an unrebalanced index does.
- **The methods**:

  | Method | Target at each rebalance | Needs |
  |---|---|---|
  | `custom` | the declared weights, effective-dated | weights summing to 1 |
  | `equal` | 1/N each | nothing more |
  | `cap` | each constituent's market cap that day, over the total | fafnir's daily market cap, `duk mc` (§6) |
  | `price` | one unit of each security, and a divisor | nothing more; the divisor is §5's |

  These four are what the owner named. Capped, inverse-volatility and
  fundamental weights can each be added later as a method with its own
  parameters. None is needed for issue 6.
- **The rebalancing rules**:
  - calendar: daily, monthly, quarterly, semi-annual or annual, on the last
    trading day of the period;
  - drift band: rebalance at a close where any weight is further from its
    target than an absolute band;
  - never: buy and hold from the base date.

  A rule is stored, and every report names it (`G03`). Reconstitution is
  only ever a dated definition row, never a rule.

### 4. The total return is computed from prices and every distribution

If question 3 is answered as recommended:
- **The price level** comes from unadjusted closes and units. It is labelled
  `price_only` and can never be assigned as a benchmark (`G01`). It is kept
  because a factor model may want it, and because the difference between the
  two levels is the distribution return, which can be audited.
- **The total-return level** reinvests each cash dividend at the ex-date
  close, across the whole index (question 4).
- **A split** multiplies the units by its ratio on its ex-date. Neither
  level moves.
- **Dividends are gross of withholding**, so the series is recorded as not
  net of withholding (`G05`).
- **Every dividend reinvested is a row in the file**, with its source, so
  the distribution return can be traced to each payment.

### 5. Events the method cannot assume

- **A split in a price-weighted index** changes the divisor on the ex-date,
  so the level is continuous (trap 3).
- **A reconstitution** changes the divisor, or for the other methods the
  units, at the close of its date, so the level is continuous.
- **A delisting, merger or spinoff** is refused unless the definition states
  it: a last price, or a cash or stock consideration, dated (trap 4). fafnir
  carries splits and cash dividends and nothing else (ADR 0032), so anything
  else has to be declared.
- **A constituent with no close on a trading day** stops the build, naming
  it and the day. There is no carried price here. `--estimate` (ADR 0044) is
  for a portfolio's return, and an index has no measured path to fall back
  from.

### 6. Where the inputs come from

- **Prices, splits and dividends** through `duk`, in `providers/fafnir.py`
  and nowhere else (ADR 0032). Files fill any gaps, as for `pt`.
- **Market caps** through `duk mc`, fafnir's daily market cap, built for
  cap-weighted index construction. On each day it is the raw close × the
  shares outstanding of the latest balance sheet known that day × SF1's share
  factor (an ADR's ratio, a multi-class issuer's conversion).
  - **Point in time.** The count steps on the day a filing became known, and
    only splits move it between filings, so no later filing or restatement
    reaches an earlier day. A backtest of the index is not looking ahead.
  - **Read at each rebalance.** The method sets weights from that day's caps.
    Between rebalances the units hold, so the index drifts with prices and a
    new filing moves nothing until the next rebalance, as a published
    cap-weighted index does.
  - **Stored with its basis.** Each cap read is stored with its count,
    factor, `fiscal_date` and `known_from`, so a weight traces to the filing
    behind it.
  - **No cap stops the build.** `duk mc` gives none for a fund or an ETF, a
    security Sharadar does not feed, a day before the first filing or more
    than two years after the last, or a secondary share class. The build
    names the constituent and the reason. A file can supply the cap instead,
    marked as such.
  - **One issuer, one line.** `duk mc` counts every class of a multi-class
    issuer under its primary class, so a definition holding two classes of
    one issuer is refused, rather than holding it twice.
  - **Full shares, no float.** SF1 carries no float. A float factor can be
    declared per constituent, dated, with its source, and the report says
    which.
  - `providers/fafnir.py` gains the read, through `duk` alone (ADR 0032).
- **The trading calendar** through `duk`, as `pt` reads it.

### 7. How a portfolio uses a `.bmk`

- **`pt benchmark assign`** copies into the `.port`:
  - the benchmark's description, return type, periodicity, withholding
    status and rebalancing rule;
  - its components and weights as of each annual period end (`G03`);
  - its total-return levels.

  It also records the `.bmk` file's content hash as the source. The `.port`
  stays self-contained (question 8).
- **Effective-dated**, prospective or retroactive, with a reason, and the
  change disclosed for as long as `G04` requires.
- **Refreshing an assigned benchmark** adds the levels for new dates. A
  change to a level already imported is refused unless restated, since
  returns already reported depend on it.
- **`pert twr` and `pert periods`** show the benchmark's return over each
  row's window, and the active return, beside the portfolio's. A row with no
  benchmark return says why (`G02`).
- **A `.bmk` series is marked theoretical**, and the chain-linking code
  refuses to link it onto the portfolio's own record (`J04`).

### 8. What this does not do

- **No transaction costs, fees or taxes** in a synthetic index. The levels
  are gross, and reports say so.
- **Tracking error, up/down capture, beta** and the other measures that need
  a monthly series and matched periodicity (`PORT-GIPS-F03`) belong to the
  risk figures, issue 7. They are moved there from issue 6.
- **No optimisation.** Choosing weights to minimise anything is `po`'s.

## Test obligations

From issue 6:
- **`test_price_only_benchmark_refused`**: assigning the price level exits 4
  with `PT-E-GIPS-PRICE-ONLY-BENCHMARK`.
- **`test_blend_rebalancing_is_explicit`**: a monthly-rebalanced and a never-
  rebalanced blend over the same days give different returns, and each report
  names its rule.
- **`test_benchmark_change_disclosed_for_required_duration`**.
- **`test_no_benchmark_requires_reason`**.
- **A golden file of the disclosure block.**

And from this ADR:
- **Each method by hand**: a three-security index over a week, under each of
  the four methods, against arithmetic in the test's docstring.
- **A split does not move either level**, under every method. One forced
  without its divisor change fails.
- **The distribution return reconciles**: total-return level ÷ price level
  equals the compounded reinvested dividends, to the last digit.
- **A missing close stops the build**, naming the constituent and the day.
- **A delisting, merger or spinoff not declared is refused.**
- **Rebuild reproduces the derived state** of every fixture, byte for byte.
- **Determinism**: two builds of the sample give identical bytes.
- **A `.bmk` series cannot be chained onto a portfolio's return.**

## Open questions for the owner

1. **The CLI's name.** You suggested `bt`.
   - **Recommendation: `bmk`**, the same word as the file. In investment
     tooling `bt` reads as "backtest", and the most-used Python backtesting
     library is called `bt`. That invites the very confusion `J04` guards
     against: a backtest presented beside a track record.
2. **Units or weights.**
   - **Recommendation: units**, with weights derived from them (§3). A file
     of weights cannot say what happens between rebalances (trap 2).
3. **Building a total return from fafnir's prices and dividends.** ADR 0006
   §1 forbids it, because a missing dividend silently understates the index.
   - **Recommendation: allow it, with three safeguards, and amend ADR 0006
     §1.** What ADR 0006 guards against is a price series plus partial
     dividends passed off as a published index's total return. An index
     defined by its own constituents, computed from every distribution they
     paid, *is* that index's total return. The safeguards:
     - **Every dividend reinvested is a stored row**, so the gap between the
       total-return and price levels can be audited payment by payment.
     - **Each constituent declares its distribution schedule**: none,
       monthly, quarterly, semi-annual or annual. A constituent that declares
       one, and has a gap in the source longer than it allows, stops the
       build, naming the gap.
     - **The source is named on the benchmark**, so a report reads
       "synthetic, from fafnir closes and dividends", never as a published
       index.
4. **Where a dividend is reinvested.**
   - **Recommendation: across the whole index, at the ex-date close.** That
     is the convention of the major index providers, and it keeps each
     constituent's weight what the method says. Reinvesting in the paying
     security instead tilts the index toward high payers between rebalances.
5. **Market caps for cap weighting.** The draft said fafnir did not carry
   shares outstanding and proposed a file. The owner pointed out that fafnir
   stores fundamentals. It does more: `duk mc` serves a point-in-time daily
   market cap built for this. Answered in §6.
6. **Rebalancing timing.**
   - **Recommendation: at the close of the last trading day of the period**,
     with the targets set from that close. Some providers fix the weights from
     a reference date days before. That convention can be added later as a
     parameter.
7. **Long-short and leveraged factor portfolios.** A factor such as small
   minus big holds negative weights, and a dollar-neutral one sums to zero,
   which a level built from weights summing to 1 cannot represent.
   - **Recommendation: version 1 is long-only and fully invested.** A
     long-short factor is the difference of two `.bmk` legs' returns,
     computed where the factor model lives (`risky` or `po`). Do you need
     long-short inside one file sooner?
8. **Copy into the `.port`, or reference the `.bmk`?**
   - **Recommendation: copy**, with the `.bmk` file's content hash as the
     source (§7). A `.port` "contains everything needed for analysis", and a
     path to another file breaks when either moves, or when the `.bmk` is
     rebuilt with different inputs.
9. **Does the policy benchmark follow the ADR 0048 targets?**
   - **Recommendation: no.** The policy benchmark is its own `.bmk`, a
     `custom` blend of one benchmark per class, which the tool can seed from
     the `.port`'s policy. A later change of target does not change the
     benchmark. `pt allocation drift` warns when the two differ, since a
     benchmark change is a disclosed event (`G04`) and a target change is
     not.
10. **Active return, and a benchmark published less often than daily.**
    - **Recommendation:**
      - active return is arithmetic, portfolio minus benchmark, labelled as
        such;
      - a row whose window does not start and end on a date the benchmark
        has a level for shows no benchmark return, with that reason;
      - the benchmark's periodicity is disclosed when it is less frequent
        than monthly (`G05`).

## Owner's answers (2026-11-27)

1. **The name:** `bmk`, the same word as the file.
2. **Units or weights:** units, with weights derived from them.
3. **A total return from fafnir's closes and dividends:** allowed, with the
   three safeguards, and ADR 0006 §1 is amended when this is built.
4. **Reinvestment:** across the whole index at the ex-date close.
5. **Market caps:** from fafnir through `duk mc`, point in time, read at
   each rebalance, with no float unless one is declared (§6). A file supplies
   a cap only where `duk mc` has none, and is marked.
6. **Rebalancing:** at the close of the period's last trading day.
7. **Long-short:** version 1 is long-only and fully invested. A long-short
   factor is the difference of two legs, computed where the factor model
   lives.
8. **Copy into the `.port`:** yes, with the `.bmk` file's content hash.
9. **The policy benchmark:** its own `.bmk`, seeded from the policy, and not
   tied to later target changes. `pt allocation drift` warns when they
   differ.
10. **Active return:** arithmetic. A row whose window lacks a benchmark level
    at either end shows none, with the reason. The periodicity is disclosed
    when less frequent than monthly.

## As built (2026-11-27)

Phases 1 and 2 together: `cap` came with the rest, since `duk mc` made its
inputs as easy to read as a close. Where the code settles what the text above
left open, or differs from it:

1. **The schema, as the owner reviewed it** (`docs/bmk-schema.md`), with the owner's
   three answers:
   - what a spinoff or a stock merger delivers, and is not a member, is held
     until the next rebalance or reconstitution;
   - schedules are proposed from each security's dividend history and
     declared only when accepted, marked as proposed and accepted;
   - one index per file.
2. **`free_float`, not `float_factor`.** The no-float lint reads an attribute
   named `float*` as binary floating point and may not be silenced, so the
   name changed rather than the rule.
3. **A departure is valued at what it paid.** On its last day a delisted or
   merged member is priced at its consideration, not its close, so settling
   it at the close moves no level. Under `custom`, the weights a departure
   leaves short of 1 stop the next rebalance rather than being spread pro
   rata. Under `equal` and `cap` the method sets weights over the members
   left.
4. **The schedule's tolerances** are 45, 120, 210 and 400 days. A proposal
   takes the period nearest the median gap, if it is within a quarter of it
   and every gap fits. Otherwise it is `irregular`, allowing the longest
   gap seen and half again.
5. **The last built day never rebalances by rule.** Whether it ends its
   period depends on the next session, which the file may not hold yet. A
   rebalance at a close moves only later levels, so a later build, holding
   that session, decides it without moving any level already built.
6. **The issuer key is declared** (`--issuer`). fafnir's security master,
   as `duk th` serves it to `lookup_security`, carries no CIK to read. A
   cap-weighted index cannot hold a secondary class twice in any case:
   `duk mc` has no cap for one, and the build stops.
7. **A refresh compares as well as adds.** A close or a split is compared by
   its key; a dividend by its ex-date and amount. A dividend the source no
   longer lists, or now states differently, is a restatement like any other.
8. **Files are a first-class source.** `FileProvider` gained a
   `calendar_file` and a `cap_file`, so an index can be built where fafnir
   holds nothing.
9. **`pt` refuses a `.bmk`.** Read as a `.port` at schema version 1, it would
   have been offered `pt migrate`, which would apply the portfolio's
   migrations to an index. `bmk` refuses a `.port` likewise.
10. **`.bmk` migrations.** The runner is the `.port`'s, reading
    `schema/bmk/`. There is no `bmk migrate` yet: it arrives with the second
    `.bmk` schema. The version gate's minor-release rule covers `.port`
    migrations only until then.
11. **`examples/sample.bmk`** is generated by `scripts/build_sample_index.py`:
    five fictional stocks, cap-weighted and quarterly, with a split,
    quarterly dividends, a free float and a reconstitution. It is held to a
    fresh build, and its first day is checked by hand.

## Consequences

- **A fifth CLI**, built, and the `CLAUDE.md` table and README updated in
  the commit that adds it.
- **A second file format** with its own schema, migrations, sample and
  documentation (`docs/bmk-format.md`).
- **ADR 0006 §1 amended**, if question 3 is answered as recommended.
- **ADR 0030 completed**: `pt benchmark assign` exists, and `Cash (0%)`
  remains what "unassigned" means.
- **Issue 6 is narrowed.** Tracking error and capture move to issue 7.
- **`pert` gains a benchmark column and active return**, under new major
  output schemas (`twr-3.0`, `periods-2.0`). That makes the release a minor at
  least.
- **Factor work** gets synthetic indexes in a reproducible file, for `risky`
  and `po` to read through `portable_core`.

## Phasing

1. **The `.bmk` core and the CLI**: definition, inputs, `custom`, `equal`
   and `price`, every rule, rebuild, the sample.
2. **`cap`**, with caps from `duk mc`.
3. **`pt benchmark assign`**, and the benchmark and active return in
   `pert twr` and `pert periods`.

The owner is asked to check in at each boundary, the first before the
`.bmk` schema is fixed.

## Alternatives considered

- **Keep reading finished series from outside** (ADR 0030). That leaves the
  rebalancing rule and the constituents outside the file that reports them,
  so `G03` is disclosed on trust. It is kept as an input (`docs/benchmark-series.md`), not as the
  only route.
- **Weights only, no units.** It cannot represent drift (trap 2). Rejected.
- **Build indexes inside `pt`.** A `.port` is one portfolio's ledger. A
  benchmark has no ledger, and the owner wants indexes for factor work that
  belong to no portfolio. Rejected, though `pt` reads `.bmk` files through
  `portable_core`.
- **Adjusted prices (`duk ph --adj`) as the total return.** They are
  derived from the same splits and dividends, so they check nothing. They hide
  which payments were reinvested, and how depends on fafnir's adjustment
  convention. Rejected as the source; usable as a cross-check.
- **Proxy a published index with an ETF's total return.** It is a fund net
  of its fees and tracking error, not the index. It is admissible as a
  benchmark if labelled as the fund, which `custom` with one constituent
  already does.
