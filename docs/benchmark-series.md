# Benchmark series: the format portable reads

The contract between the owner's benchmark tool and `portable`
([ADR 0030](adr/0030-default-benchmark-is-cash-at-zero.md)). Until a benchmark
is assigned, returns sit beside `Cash (0%)`. When the tool produces a series in
this format, assigning it is a dated, disclosed benchmark change
(`PORT-GIPS-G04`).

The fields marked **read today** are what `FileProvider.benchmark_series`
already reads. The others are what the assignment command will need for the
disclosures `docs/gips-standard.md` requires. Producing them now means nothing
has to change in the tool later.

## The file

One JSON object, keyed by benchmark name. The name is what reports show, so
make it specific (`"60/40 ACWI TR / US Agg TR, monthly rebalanced"`, not
`"Benchmark"`).

```json
{
  "60/40 ACWI-AGG TR": {
    "return_type": "total_return",
    "description": "60% MSCI ACWI Net TR, 40% Bloomberg US Aggregate TR, rebalanced monthly",
    "currency": "USD",
    "periodicity": "daily",
    "is_net_of_withholding": true,
    "levels": {
      "2022-05-18": "100.000000",
      "2022-05-19": "99.412300"
    },
    "components": [
      { "name": "MSCI ACWI Net TR", "weight": "0.60", "effective_from": "2022-05-18" },
      { "name": "Bloomberg US Aggregate TR", "weight": "0.40", "effective_from": "2022-05-18" }
    ],
    "rebalance_rule": "monthly"
  }
}
```

## Fields

| Field | Required | Read today | Meaning |
|---|---|---|---|
| `return_type` | ✓ | ✓ | `total_return` or `price_only`. **No default.** A price-only series is refused for performance (`PORT-GIPS-G01`): it understates the benchmark by roughly the dividend yield every year. |
| `levels` | ✓ | ✓ | Index **levels** by date, not returns. `YYYY-MM-DD` keys, values as **decimal strings** (`"99.412300"`). portable reads a JSON number as a decimal from its text, but a string cannot be rounded by any JSON tool in between. |
| `periodicity` | | ✓ | `daily` (default), `weekly`, `monthly`, `quarterly`, `annual`. Disclosed when less frequent than monthly (`PORT-GIPS-G05`). |
| `is_net_of_withholding` | | ✓ | `true`, `false`, or `null` for "not available", which is itself disclosed (`PORT-GIPS-G05`). |
| `currency` | | ✓ | Default `USD`. |
| `description` | ✓ | | The key features, or the name of a recognised index: the disclosure text (`PORT-GIPS-G05`). |
| `components` | for a blend | | One entry per component per weight change: `name`, `weight` (a decimal string), `effective_from`. On each `effective_from`, the weights in force sum to exactly 1. |
| `rebalance_rule` | for a blend | | `monthly`, `quarterly`, `annual`, `none` (drifting), or `drift_band:N` (rebalance when a weight drifts N percentage points). A blend without one is refused (`PORT-GIPS-G03`), as the schema already enforces. |

## What makes a series acceptable

- **Total return.** Dividends are reinvested in the level. Say whether net or
  gross of withholding taxes.
- **Levels, not returns.** portable computes period returns itself, over exactly
  the periods it reports, so they line up with the portfolio's.
- **Every date the portfolio's returns need.** At least every month-end from
  the history's start (2022-05-18) for the three-year standard deviation
  (`PORT-GIPS-F01`), and every day if daily comparisons are wanted. The first
  level must fall on or before the first period's start.
- **A blend states both its levels and its definition.** The tool computes the
  blended levels, because it owns the rebalancing. portable records the
  definition so it can disclose each component and weight as of each annual
  period end (`PORT-GIPS-G03`).
- **Deterministic.** The same inputs produce the same file, byte for byte, so a
  re-run can be compared with the last one (`CLAUDE.md` invariant 6).
- **Dates ascending, no duplicates, no blanks.** A missing level is absent, not
  `null` or `0`.

## What happens when it arrives

1. The file is loaded as a benchmark (its levels kept as `benchmark_level`
   rows, with the file as their source).
2. It is assigned from a date with a change type (`prospective` or
   `retroactive`) and a reason. `pt benchmark show` then lists the change from
   `Cash (0%)` with its date and reason, as `PORT-GIPS-G04` requires.
3. `pert` sets each return beside the benchmark's return over the same period.

Steps 1 and 2 are the commands still to build. They wait for the first real
file, so that its shape decides any detail this page leaves open.
