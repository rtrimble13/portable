# ADR 0029 — Prices through `duk ph`; identity and corporate actions through `mart`

- **Status:** Accepted — implemented in `providers/fafnir.py`. The fafnir view it reads for corporate actions is drafted below and is not yet in fafnir
- **Date:** 2026-10-20
- **Milestone:** v0.2
- **Governs:** `CLAUDE.md` invariants 1, 6, 9; `PORT-GIPS-A01`, `PORT-GIPS-J03`
- **Supersedes:** [ADR 0006](0006-fafnir-access-path.md)'s price path and its table list. Its yield-curve decision and its two missing capabilities stand
- **Answers:** [ADR 0020](0020-historical-price-import.md) R1

## Context

ADR 0006 read fafnir directly over `psycopg`, from `core.security`,
`core.symbol_xref`, `core.daily_price` and `core.corporate_action`, and
rejected `duk` for prices. Three of its four reasons were about `duk`'s output:
it rounds `ph` to two places, its DataFrame is floating point, and a parsed
CLI table cannot carry warehouse lineage.

Two facts have changed since then.

- **The owner's access cannot read `core`.** fafnir's ADR 0008 has every
  per-person role reach the warehouse through an SSH tunnel as a member of
  `fafnir_app`, which has `SELECT` on `mart` and `ref` and no `USAGE` on
  `core`. Every query the adapter made named `core`, so over the owner's tunnel
  `pt price load`, `pt instrument sync` and `pt ca sync` would all fail with
  `permission denied for schema core`. fafnir measured exactly this failure for
  `duk` before its migration 0020 added `mart` views for it.
- **The owner decided (2026-10-20):** "duk is the primary tool for pt to source
  prices." `duk` runs on the owner's machine against the remote warehouse over
  the tunnel. Asked whether `portable` should run `duk ph --json` or read the
  same `mart` views `duk` reads, the owner chose to run `duk`. For corporate
  actions, which no `mart` view carries at event grain, the owner chose to add
  one to fafnir.

## Decision

### 1. Prices come from `duk -S db ph`

```
duk -S db ph <identifier> --start-date S --end-date E --close --json --precision 6
```

- **`-S db`**, always: the warehouse, never FMP live. Live is a moving vendor
  snapshot, and the gaps it would fill are filled from files instead (§5).
- **Never `--adj`.** `PORT-GIPS-A01` requires fair value on the measurement
  date, and adjusted prices double-count splits alongside explicit
  corporate-action transactions. The argument list is built in one place and
  has no adjusted branch.
- **`--precision 6`.** `duk` rounds JSON prices to two places by default.
  fafnir stores raw closes as `NUMERIC(20, 6)`, so six places carry every
  stored digit.

### 2. The float is `duk`'s, and it is undone exactly or refused

`duk` converts the warehouse's exact decimals to a float DataFrame, and pandas
prints ten decimal places of each float. So a stored `13247980.736555` arrives
as `13247980.7365550008`. This puts binary floating point on the price path,
outside `portable`'s process. CLAUDE.md invariant 1 forbids that, so this
section is the argument for allowing it, and the condition under which it
holds.

Inside `portable` there is still no float. The text is parsed with
`json.loads(..., parse_float=Decimal)` and rounded to six places. **For any
price below 10⁹ that recovers the stored value exactly.** Such a value has at
most fifteen significant digits, so the float nearest to it is within 6×10⁻⁸
of it. Both `duk`'s rounding to six places and pandas' ten-place printing
preserve that bound. 400,000 random six-place prices across every magnitude
below 10⁹ went through `duk`'s own `round_price_columns` and pandas `to_json`
when this was written: none differed after rounding, and the worst noise was
5.96×10⁻⁸.

The condition is enforced, not assumed. A price is **refused**, and the run
stops, when:

- it is 10⁹ or more;
- it is not within 10⁻⁷ of a six-place number;
- it is zero, negative, null or not finite;
- a record is anything but `{date, close}`;
- a date is outside the range asked for, or out of order.

Any of these means `duk` is not doing what this section depends on, and every
price after it would be suspect too. Prices are stored at six places, as
fafnir holds them.

### 3. The security is named to `duk` by identifier, never by ticker

`duk` resolves a ticker to whoever holds it *now*: the open
`symbol_xref` period first. For a reused ticker that is another issuer's
history under the right-looking name, the failure ADR 0020 R5 fixed.

So `portable` keeps resolving the ticker itself, day by day, through
`mart.v_symbol_lookup` (a passthrough of `core.symbol_xref`, so the same rows
and the same segments as before). It then names each segment's security to
`duk` by identifier: its **ISIN**, else its **CUSIP**, else its **CIK**, all
read from `mart.v_security_profile`. `duk` resolves an identifier to one
`security_id` and reads prices by that id. If the identifier matches several
securities (a CIK names an issuer), `duk` refuses rather than picks.

- A security with none of the three is refused for those dates
  (`PT-E-PRICE-MISSING`). The remedy is `pt price load --file`.
- An identifier `duk` cannot resolve to exactly one security is refused the
  same way, with `duk`'s own message.

Both are answers about one security, so the run carries on for the others.
`duk` failing outright stops the run instead (`PT-E-PROVIDER-UNAVAILABLE`).
That means a usage error (exit 2: this `duk` does not take these arguments) or
an `Error:` line (the warehouse or the role).

### 4. Everything else is read from `mart` and `ref`

| What | Relation |
|---|---|
| Symbol history | `mart.v_symbol_lookup` |
| Security master, identifiers | `mart.v_security_profile` (`primary_symbol` is exposed as `symbol`) |
| Splits and cash dividends | `mart.v_corporate_action` — **drafted below; not yet in fafnir** |
| Trading calendar | `ref.trading_calendar` |

No query names `core`. A test runs every query against a fake warehouse that
has no `core` schema. A query that meets `undefined_table` or
`insufficient_privilege` is refused by name, with a remedy. For the corporate
action view, the remedy points to this ADR.

### 5. One warehouse

`duk` reads `FAFNIR_DSN` before `~/.dukrc`. `portable` reads
`PORTABLE_FAFNIR_DSN` before either. So `portable` runs `duk` with
`FAFNIR_DSN` set to the DSN it resolved itself. Otherwise the identity read and
the price read could go to two different warehouses. The DSN is passed in the
child's environment and is never logged or put in an error.

`duk`'s output is decoded as UTF-8, not the locale's code page (invariant 6;
the owner works on Windows).

### 6. Capabilities follow the route

`EOD_PRICES` and `RISK_FREE_RATE` are declared only when `duk` is on `PATH` (or
at `duk_path`). Without it, asking for prices is refused with the actual
reason: `duk` is not on `PATH`. The general "fafnir cannot supply
eod_prices" would send the owner to the warehouse.

## Consequences

- **Lineage is shorter.** `provider_ref` records
  `security_id=…;trade_date=…;via=isin:…`. Before this, it also recorded the
  `ingestion_run_id` and vendor `source`. fafnir keeps load lineage out of
  `mart` on purpose, so no `fafnir_app` role can see it by any route. Direct
  SQL would not recover it either. `PORT-GIPS-J03` is still met to the
  security and date. Tracing a price to the warehouse load that produced it is
  now a question for fafnir's operator.
- **`as_of` is the price's date**, at midnight UTC, as for a file price. When
  the warehouse loaded a bar is not visible through `mart`, and the clock is
  not an input.
- **The source label changes** from `fafnir:core.daily_price` to
  `fafnir:duk ph`. Prices are keyed by instrument, date and source, so a date
  loaded under the old label and again under the new one holds two rows. The
  newer row wins, and they are equal when both came from the same bar. The
  old route never worked over the owner's tunnel, so no such rows are
  expected.
- **One `duk` process per segment.** That is about one per instrument: a few
  minutes for this portfolio's ~150 instruments, once.
- **`pt ca sync -S fafnir` refuses until fafnir has `mart.v_corporate_action`.**
  The refusal names the view and this ADR. Until then, actions come from
  `--source file`.
- **`psycopg` is still required** for the identity reads, and `duk` now is
  too, for prices.

## Alternatives considered

- **Read `mart.v_daily_price_raw` directly**, the view `duk` itself reads.
  It is exact `NUMERIC`, keeps no float anywhere, and needs no subprocess.
  This was recommended to the owner. The owner chose `duk`, to keep one tool
  as the way prices are sourced. §2 is what makes that choice safe, and its
  refusals are what keep it safe if `duk` changes.
- **Ask `duk` by ticker and trust its ladder.** Rejected: its first rung is
  "who holds this ticker now", which is ADR 0020 R5's failure.
- **Fall back to the ticker when a security has no identifier**, when the
  ticker's current holder is the same security. Rejected: it would restate
  `duk`'s resolution ladder in `portable`, a third copy of a rule fafnir
  already copies once and tests against the original. The refusal names the
  dates, and a file fills them.
- **`duk -S live` for what the warehouse lacks.** Rejected: a vendor snapshot
  that moves is not reproducible, and the owner named files as the gap-filler.

## Appendix — the fafnir migration this ADR reads

For the owner to apply in fafnir as its next migration. It follows migration
0020's pattern exactly: a definer-rights view in `mart`, `SELECT` granted by
0001's default privileges. fafnir's privilege test and data dictionary should
gain the view in the same change.

`sql/migrations/0029_corporate_action_mart.up.sql`:

```sql
-- 0029_corporate_action_mart.up.sql
-- mart.v_corporate_action -- splits and cash dividends, one row per event.
--
-- portable (rtrimble13/portable, its ADR 0029) syncs corporate actions as a
-- fafnir_app role, which has no USAGE on core (ADR 0008). It read
-- core.corporate_action and failed over the tunnel with `permission denied for
-- schema core`, as duk's resolution did before 0020. This is 0020's answer
-- again: a definer-rights view in mart.
--
-- At event grain, deliberately: mart.v_security_action_summary aggregates and
-- cannot say which dividends went ex between two dates. Lineage
-- (ingestion_run_id, loaded_at) is left out, as in mart.v_daily_price_raw.
-- `source` stays: that an operator wrote a row (0025) is a fact about the data,
-- not about a load.
--
-- Do NOT set security_invoker (ADR 0009). SELECT comes from the default
-- privileges 0001 set on mart.

BEGIN;

CREATE OR REPLACE VIEW mart.v_corporate_action AS
SELECT
    ca.corporate_action_id,
    ca.security_id,
    ca.action_type,
    ca.ex_date,
    ca.record_date,
    ca.payment_date,
    ca.declaration_date,
    ca.split_numerator,
    ca.split_denominator,
    ca.dividend_amount,
    ca.currency,
    ca.source
FROM core.corporate_action ca;

COMMENT ON VIEW mart.v_corporate_action IS
    'Split and cash-dividend events at (security_id, action_type, ex_date) grain. '
    'Passthrough of core.corporate_action without load lineage.';

COMMIT;
```

`sql/migrations/0029_corporate_action_mart.down.sql`:

```sql
-- 0029_corporate_action_mart.down.sql
-- A view only: no data is lost. portable's `pt ca sync -S fafnir` stops working.

BEGIN;

DROP VIEW IF EXISTS mart.v_corporate_action;

COMMIT;
```
