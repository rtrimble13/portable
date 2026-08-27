# ADR 0006 — fafnir access path: direct warehouse SQL, `duk` for the yield curve

- **Status:** Accepted
- **Date:** 2026-08-27
- **Milestone:** v0.1

## Context

Bootstrap §2 names [`fafnir`](https://github.com/rtrimble13/fafnir) as
`portable`'s primary market data source and says: "Prefer querying the warehouse
directly over shelling out to `duk` where a stable schema exists; use `duk` where
it is the documented interface. Whichever you choose, document the decision in an
ADR and isolate it behind the provider interface."

`fafnir`'s `doc/data_dictionary.md` was read at commit-time of this ADR. The
relevant contract:

| Object | Grain | Adjustment status |
|---|---|---|
| `core.security` | `security_id` | — (master; carries `cusip`, `isin`, `cik`, `exchange_code`, `currency`) |
| `core.symbol_xref` | `(symbol, valid_from)` | — (point-in-time ticker → `security_id`) |
| `core.daily_price` | `(security_id, trade_date)` | **RAW (unadjusted)** |
| `core.corporate_action` | `(security_id, action_type, ex_date)` | — (`split` / `dividend`) |
| `mart.v_daily_price_adjusted` | `(security_id, trade_date)` | **ADJUSTED**, derived on read |
| `ref.trading_calendar` | `(exchange_code, trade_date)` | — (`is_open`) |

## Decision

**Read the warehouse directly over `psycopg`** for security master, prices,
corporate actions, and the trading calendar. **Shell out to `duk`** for the
treasury yield curve, and for nothing else.

### Which tables, and why

- **`core.daily_price` — always. Never `mart.v_daily_price_adjusted`.**
  `PORT-GIPS-A01` requires fair value on the measurement date, and `CLAUDE.md`
  requires unadjusted prices with explicit corporate-action transactions.
  fafnir's own data dictionary states `core.daily_price` is the unadjusted
  endpoint and that the series "contains split-sized jumps. That is correct."
  The adapter does not expose the adjusted view to the valuation path at all —
  `FafnirProvider.get_eod_prices()` cannot be made to return adjusted prices,
  because the SQL that would do so is not written. A future return-series
  consumer that legitimately wants adjusted prices gets a separately named
  method, so the choice is always visible at the call site.
- **`core.symbol_xref` for resolution**, so a ticker resolves to the security it
  named *on the trade date*, not the one it names today. Renames and relists are
  a real source of wrong history.
- **`core.corporate_action` for `pt ca sync`** — splits and dividends only, which
  is what fafnir carries. Spinoffs, mergers, and symbol changes are **not** in
  the warehouse; `pt ca sync` reports that it cannot see them rather than
  implying the list is complete.

### Why direct SQL rather than `duk`

1. The schema is documented, versioned (`meta.schema_migration`), and stable —
   which is the condition the bootstrap sets for preferring it.
2. `PORT-GIPS-J03` requires that supporting data for every reported figure be
   captured, including third-party records. Direct SQL lets the adapter record
   *which warehouse rows produced a price* — `security_id`, `trade_date`,
   `ingestion_run_id`, `source` — into `price.provider_ref`. A parsed CLI table
   cannot carry that lineage.
3. Determinism: a subprocess boundary adds locale, formatting (`duk` rounds `ph`
   output to 2 dp by default), and version drift to a money path.
4. `duk`'s `ph` output is a formatted DataFrame; round-tripping `Decimal` through
   it is exactly the float exposure ADR 0005 forbids.

### Why `duk` for the yield curve

fafnir's `duk.md` states plainly that `yc` is **live-only** — "until the
economic-series fast-follow adds treasury data to the warehouse; in db mode it
logs a warning and reads live (requires an FMP key)". There is no `core` or `ref`
treasury table to query. `duk -S live yc` **is** the documented interface, so the
adapter invokes it with `--json`, and treats its absence as a capability the
provider does not have rather than an error.

### Configuration

DSN resolution order, highest first: `--dsn` flag → `PORTABLE_FAFNIR_DSN` →
`FAFNIR_DSN` (fafnir's own convention) → `[database].dsn` in `~/.dukrc` →
`~/.fafnirrc`. Secrets are never read from a `portable` config file when an
environment variable is available, and are never logged (bootstrap §6.3).

## Two capabilities fafnir does not have — and what `portable` does about it

This is the most important consequence of reading the data dictionary, and it is
recorded here so nobody later assumes otherwise.

1. **No total-return benchmark series.** fafnir carries security prices, not
   index levels, and nothing in `core` or `mart` is a total-return index.
   `PORT-GIPS-G01` prohibits price-only benchmarks and requires `portable` to
   *refuse* rather than warn. Therefore `FafnirProvider` **declares no benchmark
   capability at all** in v0.1. A benchmark reaches `portable` through
   `FileProvider` with an explicit `return_type`, and a series loaded without one
   is rejected — `PT-E-GIPS-PRICE-ONLY-BENCHMARK`. Synthesising a total return by
   adding `core.corporate_action` dividends to a price series would be a
   defensible-looking wrong number of exactly the kind this repo exists to avoid.
2. **No treasury curve in the warehouse.** Hence the `duk` shell-out above, and
   hence the risk-free rate is a declared, named series on the result
   (`PORT-GIPS-F05`), not an ambient constant.

## Consequences

- `psycopg[binary]` is an **optional** dependency (`portable[fafnir]`). Its
  absence makes `FafnirProvider` unavailable with a clear message, not an
  `ImportError` at CLI start.
- Everything fafnir-shaped lives in `src/portable_core/providers/fafnir.py`.
  Table and column names appear in one `_SCHEMA` mapping at the top of that
  module, so a fafnir migration is a one-file change. `CLAUDE.md` already forbids
  coupling to fafnir internals anywhere else; there is a test that enforces it.
- If the warehouse is unreachable, the provider reports it and `--offline`
  operation against the `.port` price cache continues to work (exit 5 only when
  a required price is genuinely missing or stale beyond tolerance).

## Alternatives considered

- **`duk` for everything** — one interface, no DSN handling, no Postgres
  dependency. Rejected on lineage (2), determinism (3), and `Decimal` (4).
- **Import `duk` as a library** (`duk.datasource.db`) — its documented
  DataFrame contract is stable and this would avoid the subprocess. Rejected
  because it makes `fafnir` a hard Python dependency of `portable` and couples
  the two release cycles, which the isolation requirement exists to prevent. It
  remains the obvious upgrade path if the subprocess proves painful.
