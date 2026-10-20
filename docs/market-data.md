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
pt --port p.port instrument sync -S fafnir     # compare the master; mirrors duk's -S
```

---

## The three providers

### `FafnirProvider` — the primary

Reads the owner's [fafnir](https://github.com/rtrimble13/fafnir) warehouse two
ways ([ADR 0029](adr/0029-prices-through-duk.md)): prices through `duk`, the
owner's tool for sourcing them, and everything else straight from fafnir's
`mart` views over `psycopg`. Both use the same DSN, so they read the same
warehouse. Every view name lives in one `SCHEMA` mapping at the top of
`providers/fafnir.py`, and every `duk` argument list is built there too, so a
fafnir change is a one-file change here.

| What | From |
|---|---|
| Security master | `mart.v_security_profile` |
| Symbol resolution **as of a date** | `mart.v_symbol_lookup` |
| End-of-day prices | `duk -S db ph <identifier> --close --json --precision 6` — **unadjusted** |
| Splits and cash dividends | `mart.v_corporate_action` — fafnir migration 0033 ([rtrimble13/fafnir#175](https://github.com/rtrimble13/fafnir/pull/175)) |
| Risk-free rate | `duk -S live yc`, for the date asked |

**`mart` and `ref` only, never `core`.** Over an SSH tunnel the owner connects
as a role in `fafnir_app`, which fafnir's ADR 0008 lets read `mart` and `ref`
and nothing else. A query that named `core` would fail there with
`permission denied`.

**Unadjusted prices only.** `duk` is never passed `--adj`, and the argument
list that would do it is not written. `PORT-GIPS-A01` requires fair value on
the measurement date, and adjusted prices are not fair values on the
measurement date — using them alongside explicit corporate-action transactions
would double-count every split.

**Exact, although `duk` prints floats.** `duk` holds prices in a floating-point
DataFrame and prints two decimal places unless told otherwise. `portable` asks
for six, the scale fafnir stores raw prices at (`NUMERIC(20, 6)`), parses the
text as `Decimal`, and rounds it back to six places. That recovers the stored
value exactly for any price below 10⁹. A value that is not within 10⁻⁷ of a
six-place number, a price of 10⁹ or more, and any record that is not
`{date, close}` are refused rather than rounded into shape.

**Symbols resolve as of a date**, through `mart.v_symbol_lookup`, because
fafnir's own data dictionary says the primary symbol is "**not** an
identifier". A ticker that has been reassigned resolves to whichever security
held it on the trade date, which is the only answer that does not silently
rewrite history.

**A range resolves day by day.** A ticker can name one security for part of a
range and another for the rest -- a rename, a delisting and a reuse -- so prices
and corporate actions are fetched segment by segment, each from the security the
ticker named on those days. A day on which it named nothing is refused by name,
not skipped, and there is no fallback to the security's primary symbol: fafnir's
own resolver uses that to say who holds a ticker *now*, which is never the
question here. fafnir's `asset_type` of `other` is refused too, rather than
assumed to be an equity.

**`duk` is told which security, by identifier.** Given a ticker, `duk` answers
for whoever holds it now. So each segment's security is named to `duk` by its
ISIN, else its CUSIP, else its CIK, which `duk` resolves to that one security
or refuses. A security with none of the three is refused for those dates, with
`pt price load --file` as the remedy. So is an identifier `duk` finds on
several securities.

**Lineage travels with every price.** `price.provider_ref` records
`security_id`, `trade_date`, and the identifier `duk` was given, so a valuation
traces back to the warehouse security that produced it (`PORT-GIPS-J03`). The
ingestion run no longer travels: fafnir keeps load lineage out of `mart`, so a
`fafnir_app` role cannot see it by any route.

#### Two things fafnir cannot supply

This is the most important part of this page, and it is why ADR 0006 exists.

1. **No total-return benchmark series.** The warehouse carries security prices,
   not index levels. `PORT-GIPS-G01` prohibits price-only benchmarks and
   requires `portable` to *refuse* rather than warn — so `FafnirProvider`
   declares **no benchmark capability at all**. Synthesising a total return by
   adding fafnir's dividends to a price series would be a
   defensible-looking wrong number of exactly the kind this repository exists to
   prevent. Benchmarks come from `FileProvider` with an explicit `return_type`.
2. **No treasury curve.** fafnir's own documentation says `duk yc` is live-only
   "until the economic-series fast-follow adds treasury data to the warehouse",
   so there is no table to query, and the rate comes from `duk -S live yc`.

#### Configuration

DSN resolution, highest first:

1. `PORTABLE_FAFNIR_DSN`
2. `FAFNIR_DSN` — fafnir's own convention
3. `[database].dsn` in `~/.dukrc`
4. `[database].dsn` in `~/.fafnirrc`

ADR 0006 also names a `--dsn` flag; it was never built.

Somebody who already has `duk` working needs no new configuration. `duk` is
run with `FAFNIR_DSN` set to the DSN resolved here, so a `PORTABLE_FAFNIR_DSN`
reaches it too. If `duk` is not on `PATH`, name it with `duk_path` in
`~/.portablerc`; without it fafnir supplies no prices and no risk-free rate,
and says so. The
environment is checked before any file because a DSN carries a password:
`portable` never reads a secret from its own config when an environment
variable is available, never logs one, redacts it in `portable config show`,
and **never includes it in an error message** — there is a test for that last
one.

Point it at a role in `fafnir_app`, fafnir's least-privilege reader.

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
| `source` | | Where the figure came from (a statement, a fund's site). Recorded as the price's `provider_ref` (`PORT-GIPS-J03`) |
| `valuation_level` | | 1–5, the GIPS hierarchy. Default 1. |
| `valuation_basis` | | `exchange_close` \| `model` \| `estimate` \| `manual` |
| `is_estimate` | | Preliminary values are flagged (`PORT-GIPS-A09`) |
| `currency` | | Default `USD` |

```csv
symbol,date,price,source,valuation_level
AAPL,2025-06-30,205.17,broker-statement,1
```

A missing column fails **on the file, naming the line** — not four commands
later on a valuation. In JSON, a price may be written as a number: it is read
from its text as a decimal, never through a float.

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

The full format, written for the tool that will build the owner's benchmark
series, is [docs/benchmark-series.md](benchmark-series.md).

### `NullProvider` — refuses politely

Declares nothing and supplies nothing. Its job is to make a command that needs
prices fail with a sentence telling the user what to do, rather than an
`ImportError` at CLI start or an `AttributeError` three frames deep. It is the
default when no source is configured.

---

## The price cache

Every price fetched is stored in the `.port` file with:

- **`source`** — `fafnir:duk ph`, `file:prices.csv`, `manual`
- **`as_of`** — when the source asserted it
- **`valuation_level`** — 1–5, the GIPS fair-value hierarchy (`PORT-GIPS-A02`)
- **`is_estimate`** — preliminary values are flagged (`PORT-GIPS-A09`)
- **`provider_ref`** — the source's own row key

And every valuation snapshot records the **exact set of prices it consumed**, in
`valuation_snapshot_price`. That is what `PORT-GIPS-J03` requires, and it is what
makes `PORT-GIPS-A09` workable: when a final price replaces an estimate,
`pt validate` names every snapshot that used the estimate, and `pt value` over
those dates values them again (ADR 0024).

### The valuation hierarchy

`pt price set` defaults to **level 5**, not level 1. A price typed at a terminal
with no documented basis is a subjective, unobservable input under the GIPS
hierarchy, and recording it as an observable exchange close would understate the
portfolio's level-5 percentage (`PORT-GIPS-H05`). Pass `--valuation-level 1` when
it genuinely is a close you are entering by hand.

### No market price at all

Some holdings will never be quoted: a contingent value right, say.
`pt instrument market-price SYMBOL --none` (or `pt instrument add
--no-market-price`) marks one. It is then valued at zero as a level-5
valuation, recorded in the snapshot with the source `none: no observable market
price`, and the snapshot is complete. A marked instrument takes no price, and
`pt price load` does not ask for one. The mark is refused for an instrument that
has ever had a price, because it applies to every date (ADR 0028).

### Staleness

A price older than the tolerance (default 5 days) is **refused**, exit code 5,
naming the symbol, the price date, and the gap.

Carrying a stale price forward silently produces a flat return series that looks
like a calm market. That is worse than an error, because nothing about the
output says anything is wrong. Raise `--staleness-tolerance` when the gap is
genuine — a holiday, a halted security — or price it by hand.

`--offline` forces cache-only operation. It is the right flag when the warehouse
is down and the prices you need are already in the file. The configured provider
is never built, so nothing can reach a network: `pt value`, `pt mark` and
`pt holdings` read the cache as they always do, and a command whose job is to
fetch -- `pt price load`, `pt ca sync`, `pt instrument sync` -- is refused at once
with `PT-E-OFFLINE-CACHE-MISS`, exit 5. That is its own error because its remedy
is not an outage's. `pt info` reports `offline`.

---

## Loading a history

[ADR 0020](adr/0020-historical-price-import.md). A return needs a price on every
instrument held, for every day it was held -- including every position since
sold. Both halves of what to ask for come from the portfolio:

```bash
pt --port p.port -S fafnir price load --from 2022-05-18 --to 2026-10-15
pt --port p.port -S fafnir ca sync --from 2022-05-18 --to 2026-10-15
pt --port p.port -S fafnir instrument sync
```

- **When.** With no symbols, `price load` and `ca sync` cover each instrument any
  account held in the range, over the days it was held, read from the ledger by
  the same replay every derived figure uses ([ADR 0021](adr/0021-derived-state-as-of-a-past-date.md)).
  A position sold in 2023 is priced for 2022–2023 and not after; an instrument
  never held is not asked for. Each held stretch is preceded by the staleness
  tolerance's worth of days, so a stretch that begins on a non-trading day --
  a cutover, an in-kind transfer -- can be valued from the previous close; a
  stretch with nothing before it (a purchase on a listing day) is noted, not
  refused.
- **Under which ticker.** Each day is asked for under the ticker the instrument
  carried that day, from portable's own symbol history (`pt ca symbol-change`),
  and the provider resolves that ticker as of that day.
- **`instrument sync` compares; it writes nothing.** Each instrument is looked up
  as of the last day it was held, and every field the provider states
  differently is listed for a person to resolve.

What fails, and how: a symbol the provider has no data for is a warning, and the
run goes on. A provider that cannot be reached -- or that cannot do what was
asked -- is a failure, exit 5, once, rather than a warning per symbol. A run that
loaded nothing at all is a failure too.

### Filling what fafnir does not hold

Not every price is in the warehouse: a mutual fund outside its universe, a
security with no identifier `duk` can be asked by (ADR 0029). Those come from
a price file in the schema above:

```bash
pt --port p.port price load --file fund-navs.csv
```

`--file` loads from that file instead of the configured provider, whatever
`-S` says. It loads **the instruments the file names, over the days they were
held**, and the range defaults to the file's first and last dates. An
instrument the file does not name is not reported missing from it. What the
file holds and was not loaded is reported: a ticker not held under that name in
the range (a typo, or the name it had before a rename), and rows dated outside
the days their instrument was held.

**Two sources disagreeing is refused.** The cache keeps one price per source
per day, and a valuation uses the one written last. So a file price and a
warehouse price that differ for the same day would be settled by the order the
commands ran in. Instead, `pt price load`, from a file or a provider, refuses
an instrument any of whose prices differs from one another source already gave
for that day. It stores nothing for that instrument, loads the rest, and exits
4 with `PT-E-PRICE-CONFLICT`, listing every disagreement. A person decides:

- `--on-conflict keep` leaves the stored prices and loads the rest;
- `--on-conflict replace` stores the new ones, which valuations then use.

The same number from two sources (`189.12` and `189.120000`) is not a
disagreement. Nor is a source correcting its own earlier price, which replaces
it as a final price replaces a preliminary one (`PORT-GIPS-A09`).

---

## Accepting price history

[ADR 0031](adr/0031-price-acceptance-against-statements.md). Loading a price
is one thing; knowing it is the right one is another. The custodian's statement
says what each position was worth on its date, an anchor independent of every
provider. A price from the wrong day, the wrong instrument (a reused ticker) or
the adjusted series disagrees with it.

```bash
pt --port p.port --as-of 2024-12-31 reconcile --against statement-2024.csv --record
pt --port p.port price check
pt --port p.port price accept AAPL -a Brokerage --date 2024-12-31 --reason "…"
```

**The statement's own date.** `pt reconcile --as-of D` compares holdings and
cash as they stood at the end of D (ADR 0021), so a year-end statement
reconciles against that year-end. A position line with a `market_value` also
checks its price.

**The price, to the cent a unit.** The statement's market value over its
quantity is the custodian's price. portable's is the one valuation would use on
that date: the newest on or before it, within the staleness tolerance. They tie
when

    |portable price × quantity − market value| ≤ $0.01 × quantity + $0.005

which is a cent a unit plus the half cent the statement's rounding of market
value to the cent accounts for. The statement's quantity is used, so a quantity
disagreement is not also reported as a price break. The tolerance is fixed:
`--tolerance` governs quantities and cash, not prices. A percentage of value is
not used, because on a large position it hides a price from the wrong day.

| Status | Means | Blocks returns |
|---|---|---|
| `tied` | agrees to the cent a unit | no |
| `accepted` | disagrees, and the owner accepted these exact figures with a reason | no |
| `break` | disagrees | **yes** |
| `unpriced` | no price on or before the date, or one stale beyond tolerance | **yes** |

**`--record` keeps the lines** (`statement_position`) under the statement's
date, which must be given explicitly. Whether each one ties is never stored:
`pt price check` re-decides every recorded line against the prices held now, so
reloading a price re-checks every anchor it touches. It exits 6 while any line
is a break or unpriced.

**An acceptance is for one disagreement.** `pt price accept` records the reason
together with the statement's quantity and market value and portable's price
and its date. If any of those changes, the line is a break again. An unpriced
line cannot be accepted: load or set a price first.

**A break blocks returns** for its account from its date until it is fixed or
accepted. `pert` will refuse returns for any period ending on or after that
date. Until it exists, `pt value` warns for every account it values from that
date on. Snapshots are still built and stored: they record what the prices
were, and the gate decides whether a return may be computed from them.

## What is not here

**Multi-currency.** The `FxCapability` protocol exists and nothing implements
it. Every table carries a currency column so that adding FX is an extension
rather than a migration, but v0.1 is USD-only and validates that. Backlog, P1.

**Intraday prices.** The protocol exists; no shipped provider declares it.

**Broker statement import.** Designed in `docs/broker-import.md` and pulled
forward to `v0.2` — note the constraint it inherits: an imported fee must carry a
`fee_class`, and an unclassifiable bundled fee is refused rather than guessed
(`PORT-GIPS-D01`, `D03`).
