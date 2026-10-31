# ADR 0032 — fafnir through `duk` alone: identity, actions and calendar join prices

- **Status:** Accepted — implemented in `providers/fafnir.py`. Needs a `duk` with `th`, `ca` and `tc`, whose `ph` and `ca` take `--exact` and which names a security by `sid:` (fafnir, the release after 3.0.5; `duk --help` lists the commands)
- **Date:** 2026-10-31
- **Milestone:** v0.2
- **Governs:** `CLAUDE.md` invariants 1, 6, 9; `PORT-GIPS-A01`, `PORT-GIPS-J03`
- **Supersedes:** [ADR 0029](0029-prices-through-duk.md) §3 (naming a security by ISIN, CUSIP or CIK), §4 (identity, actions and calendar read from `mart` and `ref`) and §5 (DSN resolution); [ADR 0006](0006-fafnir-access-path.md)'s DSN order and its `psycopg` dependency. ADR 0029 §1, §2 and §6, and ADR 0006's two missing capabilities, stand

## Context

ADR 0029 split the adapter in two. Prices came through `duk ph --exact`, the
owner's tool for sourcing them; everything else was read straight from
fafnir's `mart` and `ref` over `psycopg`, because `duk` could not answer it:

| Read | Why `duk` could not answer it then |
|---|---|
| Which security a ticker named, day by day | `duk` resolves a ticker to whoever holds it *now* (ADR 0020 R5) |
| The security master and identifiers | only inside `duk ls`'s company report, with no `security_id` |
| Splits and cash dividends at event grain | no command; `ls` summarises them |
| The trading calendar | no command |

That left two routes to one warehouse, a database driver as a dependency, a
DSN `portable` had to find for itself and hand on to `duk` so the two routes
could not read different warehouses, and fafnir's relation names in
`portable`'s source.

**The owner decided (2026-10-31):** "Portable.pt should rely on duk rather
than direct calls to fafnir db." fafnir's `duk` gained what was missing, in the
same change:

- **`duk th TICKER`** — every period of a ticker's history, `valid_from` to
  `valid_to` inclusive, each with the security it named and that security's
  master record (`security_id`, primary ticker, name, asset type, venue,
  currency, country, CIK, ISIN, CUSIP, listing status). No resolution and no
  pick: overlapping periods are all printed.
- **`duk ca SYMBOL --exact`** — splits and cash dividends from
  `mart.v_corporate_action`, one row per event, every number as the stored
  decimal text.
- **`duk tc`** — every venue with a calendar and its span; **`duk tc VENUE`** —
  its rows over a range, open or (an ad-hoc closure) not. `duk` never answers
  for one venue with another's.
- **`sid:<security_id>`** — names one warehouse security to any `duk` command,
  exactly.

fafnir pins the JSON these print in
`test/duk/test_db_portable_reads_integration.py`, against a real warehouse.

## Decision

### 1. Every read of fafnir is a `duk` command

| What | `duk` command |
|---|---|
| Ticker history and the security master | `duk -S db th ticker:<T> [ticker:<U> ...] --json` |
| End-of-day closes | `duk -S db ph sid:<id> --close --json --exact` |
| A day's range, for `pt trade check` | `duk -S db ph sid:<id> --hlc --json --exact` |
| Splits and cash dividends | `duk -S db ca sid:<id> --start-date S --end-date E --json --exact` |
| Calendar spans | `duk -S db tc --json` |
| A venue's sessions | `duk -S db tc <venue> --start-date S --end-date E --json` |
| Risk-free rate (ADR 0006) | `duk -S live yc --start-date S --end-date E --json` |

The adapter imports no database driver and holds no SQL;
`test_layering.py`'s SQL rule no longer exempts it, and `test_providers.py`
asserts no driver is imported. The ticker is passed as `ticker:<T>`, `duk`'s
explicit spelling, so it is never read as anything else.

### 2. The ticker is still resolved here, by the same rule

`duk th` returns the ticker's raw periods; `portable` decides which security
each day belongs to, exactly as before: of the periods covering a day, the
security whose primary ticker it is wins, then the later period; a day no
period covers is refused by name; there is no fallback to a primary symbol.
The history is read once per ticker per run and filtered locally, so prices,
actions and the master record for one ticker cost one `th` call between them.
A run that knows which tickers it will ask about -- `price load`, `ca sync`,
`instrument sync` -- says so first (`MarketDataProvider.prefetch`, a hint every
other provider ignores), and the adapter reads all of their histories in one
`duk th` (200 tickers a call, far inside Windows' command-line limit). The
answers are the ones a read per ticker gives; only the number of processes
changes.

fafnir's own `TICKER@DATE` rule is not used. It orders the latest start first,
and discounts a `1900-01-01` "first seen" period before the security's first
bar, so for overlapping periods it can answer differently from the rule above.
Reading raw periods keeps the policy here, where ADR 0020 R5 put it and where
its tests are; reconciling the two rules is a question for both repositories,
raised rather than settled by this ADR.

### 3. Each stretch is named to `duk` by its security id

Each segment's security is named `sid:<security_id>`: the id `duk th` printed
for it. ADR 0029 §3 named it by ISIN, else CUSIP, else CIK, because `duk` took
nothing more exact. That had two refusals of its own, both now gone:

- **A security with no ISIN, CUSIP or CIK** — dead issuers minted from
  Sharadar among them — was refused for its dates (`PT-E-PRICE-MISSING`), with
  `pt price load --file` as the only remedy. It is now priced.
- **An identifier on several securities** — a CIK names an issuer, and GOOG
  and GOOGL share one — was refused by `duk` as ambiguous. A security id
  cannot be.

`provider_ref` records `security_id=…;trade_date=…;via=sid:…`.

### 4. Exact, end to end, for actions too

ADR 0029 §2 is unchanged for prices. Corporate actions are read the same way:
`ca --exact` prints split ratios, dividend amounts and ids as decimal text,
read straight into `Decimal`. A JSON number, an exponent, a date that is not
`YYYY-MM-DD`, or an ex-date outside the dates asked for is refused, and the
run stops: every action after it would be suspect too. A `th` record missing a
field, with a `security_id` that is not a positive integer, or an `is_primary`
that is not a boolean is refused the same way — which security a day belongs to
is the whole question.

### 5. The calendar fallback stays in `portable`

`duk tc` never falls back. `portable` reads the venue list once, and judges a
venue with no calendar by NASDAQ's — fafnir's `CALENDAR_FALLBACK_EXCHANGE`, as
its ingestion does — so `pt price gaps` finds the gaps fafnir would, and its
report says which instruments were judged that way, as before.

### 6. One route, one configuration: `duk`'s

With one route there is no second warehouse to keep in step, so `portable`
stops finding a DSN for itself. `duk` reads its own configuration
(`FAFNIR_DSN`, then `[database].dsn` in `~/.dukrc`). `portable` keeps one
override, for a portfolio that should read a different warehouse from the one
`duk` is configured for: `fafnir_dsn` in `~/.portablerc`, or
`PORTABLE_FAFNIR_DSN`, handed to `duk` as `FAFNIR_DSN` in the child's
environment and never logged. `portable` reads no fafnir file; `~/.fafnirrc`,
which ADR 0006 also read and `duk` never did, is no longer consulted.

### 7. Capabilities follow the route, all of them

Every capability fafnir supplies now comes through `duk`, so all of them are
declared only when `duk` is on `PATH` (or at `duk_path`), and each refusal
without it names `duk` as the reason (ADR 0029 §6, extended).

### 8. What stops a run, and what is one security's gap

As ADR 0029 §3 had it for prices, now for every command: exit 2 (a usage
error: this `duk` lacks the command or flag) or an `Error:` line (the
warehouse, the role, the DSN) stops the run with `PT-E-PROVIDER-UNAVAILABLE`.
Anything else is about the security asked for, `PT-E-PRICE-MISSING`, and the
run goes on for the others. An empty answer is `[]`, with `duk`'s sentence on
stderr; an earlier `duk` printed the sentence (`No data found…`, `No corporate
actions…`, `No ticker history…`) instead, which is read as empty too. Neither is
a malformed answer.

## Consequences

- **`psycopg` is no longer a dependency.** The `portable[fafnir]` extra is
  empty and kept only so that an existing install command still works. What
  the owner needs for fafnir is a `duk` new enough to have `th`, `ca` and `tc`.
- **A warehouse without `mart.v_corporate_action`** (fafnir migration 0033) is
  refused by `duk`, naming the migration; `portable` passes that on and stops.
- **More `duk` processes, fewer than it looks, and cheaper.** A price load is
  one `th` for the run plus one `ph` per segment; `ca sync` one `th` plus one
  `ca` per segment; `price gaps` one `tc` list per run and one `tc` per venue
  asked; `trade check` one `th` per ticker, read once. Each costs about 0.23s
  (fafnir defers pandas and the vendor clients, which `th`, `ca`, `tc` and
  `ph --exact` do not use), against about 0.7s before.
- **Source labels change.** Instruments looked up from fafnir record
  `fafnir:duk th` (was `fafnir:mart.v_security_profile`), and calendars
  `fafnir:duk tc` (was `fafnir:ref.trading_calendar`). Price rows keep
  `fafnir:duk ph`, so no price is duplicated under a new label.
- **The contract is two-sided.** fafnir's integration test pins the JSON;
  `portable`'s `tests/unit/test_fafnir_through_duk.py` answers with a fake
  `duk` printing the same. A change to either is a change to both.
- **Configuration changes for one case only.** Someone whose DSN lived only in
  `~/.fafnirrc` could not run `duk` by hand either; they move it to
  `~/.dukrc` or `FAFNIR_DSN`, which `duk` reads.

## Alternatives considered

- **Keep reading `mart` and `ref` directly** (ADR 0029 §4). One fewer process
  per read and no `duk` release needed. Rejected by the owner's decision: one
  tool is the way fafnir is reached, and a driver, a DSN search and relation
  names in `portable` are what that tool exists to keep out.
- **Have `duk` resolve the ticker by date, a `ph --as-traded` that segments
  itself.** One call per ticker instead of two. Rejected for now: it would
  move ADR 0020 R5's rule into fafnir, where a different rule (`TICKER@DATE`,
  §2 above) already lives, and make a fafnir change able to reassign a
  portfolio's history silently. Raw periods keep the decision where its tests
  are.
- **Keep naming a security by ISIN, CUSIP or CIK.** Rejected: `sid:` is exact,
  and retires two refusals that were artefacts of the naming rather than gaps
  in the warehouse.
