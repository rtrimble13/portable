# Market data

The provider interface, the fafnir adapter, the file schemas, and how staleness
is handled. The design decision behind the fafnir adapter is
[ADR 0006](adr/0006-fafnir-access-path.md).

---

## Capabilities, not a monolith

A provider declares what it can do. Capabilities are **separately declarable
protocols**, so a partial provider is legal and its gaps are *visible*:

`security_master` · `eod_prices` · `intraday_prices` · `corporate_actions` ·
`dividends` · `benchmarks` · `risk_free_rate` · `fx` (interface only in v0.1)

That is not architectural neatness. A monolithic interface would have forced
`FafnirProvider` to implement `benchmark_series` and return *something* — and
"something" there is a price-only series that understates the benchmark by
roughly the dividend yield every year. Declaring nothing is the honest answer,
and the type system enforces it: `provider.eod_prices(...)` does not type-check
on a bare `MarketDataProvider`. Access goes through
`as_eod_provider(provider)`, which checks the declaration and then confirms the
shape, so a provider that *claims* a capability it does not implement fails at
the boundary rather than three frames into a valuation.

```bash
pt --port p.port info --format json | jq .data.native_implementation
pt --port p.port instrument sync -S fafnir     # mirrors duk's -S ergonomics
```

---

## The three providers

### `FafnirProvider` — the primary

Reads the owner's [fafnir](https://github.com/rtrimble13/fafnir) warehouse
directly over `psycopg`. Every table and column name lives in one `SCHEMA`
mapping at the top of `providers/fafnir.py`, so a fafnir migration is a one-file
change here.

| What | From |
|---|---|
| Security master | `core.security` |
| Symbol resolution **as of a date** | `core.symbol_xref` |
| End-of-day prices | `core.daily_price` — **unadjusted** |
| Splits and cash dividends | `core.corporate_action` |
| Risk-free rate | `duk -S live yc` |

**Unadjusted prices only.** `mart.v_daily_price_adjusted` is not read by the
valuation path, and the SQL that would read it is not written. `PORT-GIPS-A01`
requires fair value on the measurement date, and adjusted prices are not fair
values on the measurement date — using them alongside explicit corporate-action
transactions would double-count every split.

**Symbols resolve as of a date**, through `core.symbol_xref`, because fafnir's
own data dictionary says the primary symbol is "**not** an identifier". A ticker
that has been reassigned resolves to whichever security held it on the trade
date, which is the only answer that does not silently rewrite history.

**Lineage travels with every price.** `price.provider_ref` records
`security_id`, `trade_date`, `ingestion_run_id`, and `source`, so a valuation
traces back to the warehouse load that produced it. `PORT-GIPS-J03` requires
supporting data for every reported figure "including records obtained from third
parties" — which is why the reference names the row, not just the vendor.

#### Two things fafnir cannot supply

This is the most important part of this page, and it is why ADR 0006 exists.

1. **No total-return benchmark series.** The warehouse carries security prices,
   not index levels. `PORT-GIPS-G01` prohibits price-only benchmarks and
   requires `portable` to *refuse* rather than warn — so `FafnirProvider`
   declares **no benchmark capability at all**. Synthesising a total return by
   adding `core.corporate_action` dividends to a price series would be a
   defensible-looking wrong number of exactly the kind this repository exists to
   prevent. Benchmarks come from `FileProvider` with an explicit `return_type`.
2. **No treasury curve.** fafnir's own documentation says `duk yc` is live-only
   "until the economic-series fast-follow adds treasury data to the warehouse",
   so there is no table to query and `duk` **is** the documented interface. It
   is the one place `portable` shells out, and it does so for nothing else.

#### Configuration

DSN resolution, highest first:

1. `--dsn`
2. `PORTABLE_FAFNIR_DSN`
3. `FAFNIR_DSN` — fafnir's own convention
4. `[database].dsn` in `~/.dukrc`
5. `[database].dsn` in `~/.fafnirrc`

Somebody who already has `duk` working needs no new configuration. The
environment is checked before any file because a DSN carries a password:
`portable` never reads a secret from its own config when an environment
variable is available, never logs one, redacts it in `portable config show`,
and **never includes it in an error message** — there is a test for that last
one.

Point it at the least-privilege `fafnir_app` role.

`psycopg` is an optional dependency (`pip install 'portable[fafnir]'`). Its
absence is felt only by somebody who asked for fafnir, never at CLI start.

### `FileProvider` — a first-class citizen

Not a test double. It is how a benchmark enters `portable` at all, how the
examples run with no warehouse, and how anybody without a Postgres instance uses
the tool fully.

**Prices** — CSV or JSON.

| Column | Required | Notes |
|---|---|---|
| `symbol` | ✓ | |
| `date` | ✓ | `YYYY-MM-DD` |
| `price` | ✓ | A plain decimal. Never a float literal. |
| `source` | | Recorded on the price, and ends up in the audit trail |
| `valuation_level` | | 1–5, the GIPS hierarchy. Default 1. |
| `valuation_basis` | | `exchange_close` \| `model` \| `estimate` \| `manual` |
| `is_estimate` | | Preliminary values are flagged (`PORT-GIPS-A09`) |
| `currency` | | Default `USD` |

```csv
symbol,date,price,source,valuation_level
AAPL,2025-06-30,205.17,broker-statement,1
```

A missing column fails **on the file, naming the line** — not four commands
later on a valuation.

**Corporate actions** — CSV or JSON, requiring `symbol`, `action_type`,
`ex_date`; optionally `pay_date`, `record_date`, `split_numerator`,
`split_denominator`, `cash_amount`.

**Benchmarks** — JSON, and this is where `PORT-GIPS-G01` is enforced:

```json
{
  "SPXTR": {
    "return_type": "total_return",
    "periodicity": "daily",
    "is_net_of_withholding": false,
    "levels": { "2025-06-30": "13724.11" }
  }
}
```

**`return_type` is required and has no default.** A series that does not declare
it is rejected. There is no default *deliberately*: a price index understates
its benchmark by roughly the dividend yield every year, flattering the portfolio
by 1.5–2% annually, and a defaulted `total_return` is precisely how that happens
by accident.

### `NullProvider` — refuses politely

Declares nothing and supplies nothing. Its job is to make a command that needs
prices fail with a sentence telling the user what to do, rather than an
`ImportError` at CLI start or an `AttributeError` three frames deep. It is the
default when no source is configured.

---

## The price cache

Every price fetched is stored in the `.port` file with:

- **`source`** — `fafnir:core.daily_price`, `file:prices.csv`, `manual`
- **`as_of`** — when the source asserted it
- **`valuation_level`** — 1–5, the GIPS fair-value hierarchy (`PORT-GIPS-A02`)
- **`is_estimate`** — preliminary values are flagged (`PORT-GIPS-A09`)
- **`provider_ref`** — the source's own row key

And every valuation snapshot records the **exact set of prices it consumed**, in
`valuation_snapshot_price`. That is what `PORT-GIPS-J03` requires, and it is what
makes `PORT-GIPS-A09` workable: when a final price replaces an estimate, the
snapshots that used the estimate are identifiable and can be rebuilt.

### The valuation hierarchy

`pt price set` defaults to **level 5**, not level 1. A price typed at a terminal
with no documented basis is a subjective, unobservable input under the GIPS
hierarchy, and recording it as an observable exchange close would understate the
portfolio's level-5 percentage (`PORT-GIPS-H05`). Pass `--valuation-level 1` when
it genuinely is a close you are entering by hand.

### Staleness

A price older than the tolerance (default 5 days) is **refused**, exit code 5,
naming the symbol, the price date, and the gap.

Carrying a stale price forward silently produces a flat return series that looks
like a calm market. That is worse than an error, because nothing about the
output says anything is wrong. Raise `--staleness-tolerance` when the gap is
genuine — a holiday, a halted security — or price it by hand.

`--offline` forces cache-only operation. It is the right flag when the warehouse
is down and the prices you need are already in the file.

---

## What is not here

**Multi-currency.** The `FxCapability` protocol exists and nothing implements
it. Every table carries a currency column so that adding FX is an extension
rather than a migration, but v0.1 is USD-only and validates that. Backlog, P1.

**Intraday prices.** The protocol exists; no shipped provider declares it.

**Broker statement import.** Backlog, P1 — and note the constraint it inherits:
an imported fee must carry a `fee_class`, and an unclassifiable bundled fee is
refused rather than guessed (`PORT-GIPS-D01`, `D03`).
