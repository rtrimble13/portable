# The `.bmk` index file, and `bmk`

A `.bmk` file is one index: a benchmark, a blend, or a synthetic index for
factor work. `bmk` defines it, reads its market data, and builds its levels.
The design and its reasons are [ADR 0050](adr/0050-benchmark-construction.md);
the tables are [bmk-schema.md](bmk-schema.md), generated from the DDL.

An index is **theoretical**. It is a benchmark or a factor, never performance
anyone earned, and it is never linked onto a portfolio's own returns
(`PORT-GIPS-J04`).

## What the file holds

| Group | Tables | Rule |
|---|---|---|
| **Your definition** | `methodology`, `reconstitution` and `reconstitution_member`, `security_event`, `distribution_schedule` | Dated and never edited. A row entered wrongly is replaced on its own date with `--corrects`, and kept (ADR 0025). |
| **Market data** | `security`, `security_symbol_history`, `trading_day`, `close`, `split`, `dividend`, `market_cap` | Each row records its source. A refresh adds rows. Changing one already stored is refused unless restated with a reason, which `input_restatement` logs. |
| **Built** | `build`, `level`, `holding`, `rebalance_weight`, `reinvestment` | Replaced by every `bmk build`, and reproduced exactly by a rebuild. `bmk validate` checks that. |

A file is self-contained: it rebuilds with no network, and two builds of the
same file write the same bytes.

## How an index is computed

**It holds units, not weights.** At a rebalance or reconstitution close, the
method's target weights become units: `units = weight × level ÷ close`.
Between them, the units hold and the weights drift with prices.

| Weighting | Target at each rebalance |
|---|---|
| `custom` | the weights the membership declares, summing to exactly 1 |
| `equal` | 1/N of the members |
| `cap` | each member's market cap × free float, over the total |
| `price` | one unit of each member, and a divisor |

**Rebalancing rules:** `daily`; `monthly`, `quarterly`, `semiannual` or
`annual`, at the close of the period's last session; `drift_band`, at a close
where a weight stands further from its target than the band (custom or equal
only); or `never`. A price-weighted index is `never`: only a reconstitution
changes it.

**Each day, in order:**
1. **At the open**, a split multiplies a holding's units by its ratio. Under
   `price` it changes the divisor instead. Neither level moves. A spinoff's
   shares are added.
2. **At the close**, `price level = Σ(units × close) ÷ divisor`.
3. **A dividend going ex** that day adds `Σ(units × dividend) ÷ divisor`
   index points, and the total-return level chains on the price level:
   `tr_t = tr_{t-1} × (price_t + points_t) ÷ price_{t-1}`. That is the dividend
   reinvested across the whole index at its ex-date close. Every one is a
   `reinvestment` row.
4. **After the close**, three things happen, in order:
   - a declared delisting or merger is settled at what it paid, and the
     proceeds are reinvested across the index;
   - a reconstitution or methodology change takes effect;
   - the units reset to the targets if either of those happened, or the
     rule falls due.

The **price-only level** is kept for factor work, and as the base of the
chain. It is never a benchmark (`PORT-GIPS-G01`).

## What stops a build

Nothing is carried forward, assumed or skipped (`CLAUDE.md` invariant 9). Each
of these names the security and the day:

| Code | Why |
|---|---|
| `PT-E-INDEX-CLOSE-MISSING` | A member has no close on a session it is held. |
| `PT-E-INDEX-EVENT-UNDECLARED` | A member has no close after a date, and nothing says what it became. Declare it with `bmk event add`. |
| `PT-E-INDEX-NO-SCHEDULE` | A security held has no declared distribution schedule. |
| `PT-E-INDEX-DIVIDEND-GAP` | A security has a longer gap between dividends than its schedule allows, or a declared non-payer paid. |
| `PT-E-INDEX-CAP-MISSING` | A cap-weighted member has no market cap on a rebalance date. |
| `PT-E-INDEX-ONE-ISSUER` | Two securities of one issuer are in one membership. |
| `PT-E-INDEX-DEFINITION` | The definition cannot be built, for example custom weights a departure leaves short of 1. |

**The schedule is the total return's safeguard.** A missing dividend
understates a total return silently, so each security declares how it pays:
`none`, `monthly`, `quarterly`, `semiannual`, `annual`, or `irregular` with
the longest gap allowed. The periodic schedules allow gaps of 45, 120, 210 and
400 days. `bmk schedule propose` reads each security's dividend history and
proposes one. `bmk schedule accept` declares the proposals, marked as
proposed and accepted.

## Where the market data comes from

**From fafnir**, through `duk` alone (ADR 0032), by fafnir's security id,
never by a ticker:
- unadjusted closes, `duk ph sid:N --close --exact`;
- splits and cash dividends, `duk ca sid:N --exact`, read from a year
  before the base date so the first schedule can be checked;
- the calendar, `duk tc`;
- market caps for a cap-weighted methodology, on the dates it rebalances,
  `duk mc sid:N... --exact`.

fafnir derives each cap on read: the raw close × the shares outstanding of
the latest balance sheet known that day × SF1's share factor. It is point in
time, so a historical build does not look ahead. Each cap is stored with the
filing behind it.

There is no cap for a fund, an ETF, a secondary share class, or a security
Sharadar does not feed. A file can supply one, marked as such. There is no
float in SF1: declare a free float per member with `SYMBOL/0.8` and
`--free-float-source`.

**From files**, through `--source file`:

| Setting | File |
|---|---|
| `price_file` | `symbol,date,price`: unadjusted closes |
| `action_file` | `symbol,action_type,ex_date,...`: `split` with `split_numerator` and `split_denominator`, `cash_dividend` with `cash_amount` |
| `calendar_file` | `exchange,date`: every session, and nothing else |
| `cap_file` | `symbol,date,market_cap`, optionally `close_raw`, `shares_outstanding`, `share_factor`, `fiscal_date`, `known_from`, `source` |

Each setting is a key in `~/.portablerc` or a `PORTABLE_*` variable.

A file may list a spinoff or a merger. A refresh does not read those, and
says so: declare them with `bmk event add`.

## A worked example

```bash
bmk init us4.bmk --name "Four US stocks, cap-weighted, quarterly" \
    --description "Large caps for the equity sleeve" --base-date 2024-01-02
export PORTABLE_BMK=us4.bmk PORTABLE_SOURCE=fafnir
bmk --as-of 2024-01-02 security add AAPL
bmk --as-of 2024-01-02 security add MSFT
bmk --as-of 2024-01-02 security add XOM
bmk --as-of 2024-01-02 security add JPM
bmk methodology set --weighting cap --rebalance quarterly --effective-from 2024-01-02
bmk reconstitute AAPL MSFT XOM JPM --effective-date 2024-01-02
bmk refresh --through 2025-06-30
bmk schedule propose
bmk schedule accept
bmk build
bmk levels --from 2025-06-02
bmk --as-of 2025-06-30 holdings
bmk validate
```

A custom blend states its weights: `bmk reconstitute VTI=0.6 BND=0.4 ...`.
A later membership is a full list, dated: `bmk reconstitute ... --effective-date 2025-04-01`.

`examples/sample.bmk` is a generated example: five fictional stocks,
cap-weighted and rebalanced quarterly, with a split, quarterly dividends, a
free float and a reconstitution. `make fixtures` rebuilds it, and
`tests/integration/test_sample_index.py` checks a level by hand.

## Output

`bmk levels` (`bmk-levels-1.0`) and `bmk holdings` (`bmk-holdings-1.0`) are
validated JSON. `levels` refuses unless the build is current: built from the
definition and inputs the file holds now, whose hash it carries.

## Not yet

These are ADR 0050's phase 3:
- `pt benchmark assign`, which copies a `.bmk` file's total-return levels into a
  `.port`, with the file's hash as their source;
- the benchmark and the active return beside `pert twr` and `pert periods`.

Until then a portfolio is benchmarked against `Cash (0%)` (ADR 0030).
Long-short factor portfolios are built as two long-only legs (answer 7).
