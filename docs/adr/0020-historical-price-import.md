# ADR 0020 — Historical price import: requirements and open questions

- **Status:** Proposed — this records requirements and open questions, not yet
  a committed design. Several sub-decisions are explicitly left to the owner
  (see "Open questions").
- **Date:** 2026-10-15
- **Milestone:** v0.2 — blocks the daily time-weighted return engine and every
  risk measure after it (`docs/roadmap.md`, the "Then:" list items 1 and 5)
- **Governs:** `PORT-GIPS-A01`, `A02`, `A09`, `F01`–`F03`, `G01`, `J03`;
  `CLAUDE.md` invariants 1, 6, 9, 10

## Context

`pert`'s return engine reads `valuation_snapshot`, which `pt value` builds from
a price on every held instrument, every day. `trimble-portfolio` now has real
history to value: three accounts, 2022-05-18 to the present (about 4.4 years),
roughly 150 distinct instruments ever held once closed and converted-away
positions are counted (current holdings are about 70). Nothing computes a
return until every one of those account-days is priced.

The provider interface, the three providers, the price cache with full
provenance, the GIPS valuation hierarchy, and staleness handling are already
built (v0.1, `docs/market-data.md`). `pt price set` / `load` / `show` work
today. What has not been exercised is running that machinery across a real,
multi-year, multi-instrument history with the gaps a real custodian relationship
actually has — closed positions, matured ETFs, mutual-fund share classes,
and a security with no market price at all. Broker import needed its own ADRs
(0012–0019) despite "the provider interface" existing in the abstract before
any of them; this ADR exists because the same is true here.

## What already works, so this does not re-propose it

- `MarketDataProvider` capability protocols (`providers/base.py`): a provider
  declares only what it can do, and `as_eod_provider` etc. refuse at the
  boundary rather than failing deep in a valuation.
- `FafnirProvider`: unadjusted `core.daily_price` only, symbol resolution
  **as of a date** via `core.symbol_xref`, declares no benchmark capability at
  all (ADR 0006).
- `FileProvider`: CSV/JSON prices, corporate actions, and benchmarks (the
  latter requiring `return_type`, with no default — `PT-E-GIPS-PRICE-ONLY-BENCHMARK`).
- The price cache (`price` table): full provenance, GIPS valuation-level
  hierarchy (default level 5 for a hand-set price, per `PORT-GIPS-A02`/`H05`),
  staleness tolerance, and an idempotent upsert on
  `(instrument_id, price_date, source)` — re-running a load is already safe.
- `ValuationEngine`: collects symbols it cannot price into `incomplete_symbols`
  rather than failing the whole snapshot, and marks the snapshot incomplete
  (`pricing.py`: "this snapshot must not be used for a return").
- `pt ca sync` / `pt instrument sync`: compare recorded data against the
  provider, defaulting to currently-held symbols.

## Requirements

### R1 — Settle source availability before anything else

No `FAFNIR_DSN`/`PORTABLE_FAFNIR_DSN` is configured in this working
environment, and `duk` is not on `PATH`. Whether a working fafnir instance
exists at all (on the owner's own machine, outside this session) is unknown
and determines how much of R3–R5 below is actually needed. This blocks
everything else and should be answered first.

### R2 — Price every instrument ever held, not just current holdings

`pt ca sync` and `pt instrument sync` both default to "all held," which means
*currently* held. The symbols that matter most for this exercise — `FSMVX`,
`TRSGX`, `WSMDX`, `GSIMX`, `IBDN`, `IBDO`, and others fully disposed of between
2022 and 2025 — are exactly the ones that default excludes. Every command run
against "the full symbol set" in this document means the roughly 150 symbols
the ledger has ever touched, not the ~70 in `pt holdings` today. There is no
existing command that produces that list mechanically; one is needed (e.g.
`pt instrument list --ever-held`, derived from the ledger rather than typed by
hand) so the list cannot silently go stale as new history is added.

### R3 — A bulk file-import pipeline, to whatever extent fafnir falls short

`FileProvider` expects one pre-built CSV with every symbol and date already
in it. There is no equivalent of `scripts/xlsx_to_csv.py` for price history —
nothing converts a vendor export (Stooq, Yahoo, a paid data vendor, however the
owner sources it) into the documented `symbol,date,price,source,valuation_level`
shape. Two things are missing specifically:

- **A conversion step** honoring the same no-float-literal, fail-on-the-file
  discipline `xlsx_to_csv.py` already applies to broker exports — refuse a
  value that looks like binary noise rather than rounding it, name the exact
  row on a bad date or missing column.
- **A coverage report**: "for symbol X between Y and Z, which trading days
  have no price." Nothing like this exists today. Currently a gap surfaces
  only when `pt value` marks one account-day incomplete, discovered one day at
  a time rather than as a single pre-flight report across ~150 symbols × ~4.5
  years.

Whether this needs the full segmented-review discipline broker-import has
(`inspect` → batch → reconcile) or a lighter one-shot load depends on R1: if
fafnir supplies most of it cleanly, the file path only needs to cover the
exceptions R4/R5/R7 name below, which is a much smaller problem.

### R4 — Mutual-fund NAV coverage is unverified

Roughly 20 of the ~150 symbols are mutual fund share classes (William Blair,
Fidelity, Goldman Sachs GQG, T. Rowe Price, Dodge & Cox, PIMCO, Hood River,
AMG, Hamlin, BlackRock). ADR 0006 documents `core.daily_price` as the EOD
source but says nothing about whether fafnir's security master carries mutual
funds at all, as opposed to only exchange-traded securities. This needs
checking against the real warehouse, not assumed either way. If fafnir does
not carry them, these ~20 symbols need the file path regardless of how R1
resolves.

### R5 — Coverage must extend past "currently active"

The daily valuation the return engine needs covers every account-day since
inception, which means pricing `FSMVX`, `TRSGX`, `WSMDX`, `GSIMX`, `IBDN`, and
`IBDO` for the period they **were** held (2022–2025), even though none of them
are current holdings. `core.symbol_xref`'s point-in-time design (ADR 0006)
suggests fafnir is built to answer "what did this ticker mean on date X," which
is promising, but whether `core.daily_price` keeps serving a `security_id`
after the symbol stops trading (a fund closes, an ETF matures, a merger
retires a CUSIP) is unverified and should be checked directly rather than
inferred from the schema's intent.

### R6 — Validate corporate-action consistency before trusting a series across it

All 22 corporate actions in this portfolio were recorded by hand from the
custodian's own data, independent of any market-data provider. Before trusting
a provider's unadjusted price series across a split boundary, `pt ca sync`
should be run against the **full** historical symbol list (R2), not its
held-only default, to confirm the provider's own ex-dates and ratios agree
with the ledger. A silent disagreement here is exactly the kind of
defensible-looking wrong number this repository is organized against — one
side of a split priced on the wrong ratio looks like a plausible price, not an
error.

Note the check is necessarily partial: fafnir's `core.corporate_action` covers
splits and cash dividends only (ADR 0006), so none of this portfolio's seven
share-class conversions or the Abiomed cash-and-CVR merger would appear there
to check against in the first place.

### R7 — A permanently unpriceable instrument needs a real answer, not a workaround

`003CVR016` (the Abiomed contingent value right) will never have a market
price from any provider. `ValuationEngine` has exactly one bucket today for
"no price" — `incomplete_symbols` — which marks the whole day's snapshot
incomplete, and an incomplete snapshot "must not be used for a return" by the
tool's own design. If that stays true for as long as the CVR is held, no daily
snapshot for the account holding it is ever complete, which blocks performance
reporting on everything else in that account too, not just the CVR.

Two ways to resolve it, not decided here:

- **(a) A schema change** — a flag on the instrument or the leg meaning "known
  to have no observable price; treat as zero and complete, not missing."
  Cleaner, and consistent with the project's bias toward explicit states over
  inferred ones, but a real migration: `schema_version` bump, both directions
  tested, `CHANGELOG.md` entry, per `CLAUDE.md`.
- **(b) A standing manual-price convention** — `pt price set 003CVR016 --price
  0 --valuation-level 5 --estimate`, reaffirmed periodically. Ships today with
  no code change, but is a standing manual chore, and needs a note somewhere
  visible that the zero is a placeholder pending the CVR's resolution, not a
  stated valuation of it.

### R8 — The benchmark's total-return series must be sourced, not synthesized

`PORT-GIPS-G01` and ADR 0006 already make `portable` refuse a benchmark file
that does not declare `return_type` — fafnir carries no total-return index at
all, by design, and `FileProvider.benchmark_series` already enforces this.
What remains is pure data sourcing, external to `portable`: an actual
total-return series (not a price index) for whatever benchmark(s) `pert` will
compare against, covering at least 2022-05-18 to the present, for the
`PORT-GIPS-F01` three-year monthly standard deviation once that reporting
exists. This has its own lead time independent of the fafnir question above,
and needs an answer to which benchmark(s) are actually relevant — that is the
owner's investment-policy call, not inferrable from the ledger.

### R9 — Staleness tolerance and calendar awareness answer different questions

The existing staleness tolerance (default 5 days, `docs/market-data.md`)
guards *ongoing* use — a price that has gone stale since it was last fetched.
A bulk *historical* load needs a different check: a legitimately missing
trading day (an exchange holiday) must not be reported as a gap, while a
genuinely missing trading day must be. `ref.trading_calendar` exists in fafnir
(ADR 0006's table) but nothing today — not `FafnirProvider.eod_prices`, not any
coverage tool — is confirmed to consult it, and `FileProvider` has no calendar
awareness at all. Whatever answers R3's coverage-report need should be
calendar-aware, not a bare "this date has no row."

### R10 — Define the acceptance test before calling the import done

Broker import has an explicit acceptance test (`docs/broker-import.md` §9):
reconcile to the custodian's stated positions, cash, and lot-level realized
gains. Price history has no analog yet. Two independent anchors are already
in hand from the completed broker import and should be used rather than
inventing new ones:

- The William Blair holdings snapshot (2026-08-27): loaded close price ×
  held quantity on that date should match the custodian's stated market value
  for every still-held position, within the tolerance `pt reconcile` already
  uses.
- Every recorded sale's trade price is itself a real traded price on that date
  for that symbol — a sanity cross-check against the loaded EOD close (not an
  exact match; intraday execution differs from the close), not a hard
  reconciliation, but a cheap one worth automating.

## Decision

Not made here — R1, R4, R5, and R8 are external facts (what fafnir actually
has, what the owner wants as a benchmark) that determine how much of R3's
tooling is actually needed, and that work should not start before they're
answered. Proposed sequencing once they are:

1. **Verify before building.** Run `pt instrument sync` and `pt ca sync`
   against the full historical symbol list (R2) through a real fafnir
   instance if one exists, and directly probe fafnir for mutual-fund (R4) and
   delisted-security (R5) coverage. This determines the actual shape of R3 —
   possibly nothing beyond a handful of exceptions, possibly the whole file
   path.
2. **Fill exactly the gaps step 1 finds**: a `FileProvider` CSV (or CSVs) for
   whatever fafnir can't supply, a conversion script for whatever format that
   data arrives in, the coverage-gap report (R3/R9), the R7 decision for the
   CVR, and R8's benchmark acquisition. Then `pt price load` across the full
   history and run R10's reconciliation.

## Open questions for the owner

1. Is there a working fafnir instance to point `portable` at? Nothing is
   configured in this session.
2. If yes: what does it actually return for a mutual fund symbol, and for a
   symbol that stopped trading in 2023–2025 (R4, R5)? Worth checking directly
   rather than assuming either way.
3. Which benchmark(s) should `pert` compare against, and where will their
   total-return series come from (R8)?
4. For the CVR: the schema flag now, or the standing manual zero-price
   convention for now with the schema change deferred (R7)?
5. How far back should price history go? At minimum the account-opening date,
   2022-05-18 — ADR 0015 already treats that date as establishing beginning
   value rather than a return period before it, so there should be no need to
   price anything earlier. Confirm that's the right floor.

## Consequences if adopted as scoped

- No schema change is required **unless** R7 is decided as (a).
- `pt instrument list --ever-held` (or equivalent) is a small, low-risk
  addition regardless of how R1/R3 resolve — it's needed either way to keep
  R2/R6's "full list" from going stale.
- The coverage-gap report (R3/R9) is new functionality with its own tests;
  scope depends on step 1's findings.
- This explicitly blocks `docs/roadmap.md`'s "Daily time-weighted return
  engine" and everything under it until the full history is priced and R10's
  reconciliation passes.

## Progress (2026-10-15)

Done without a warehouse, because each was needed however R1 resolves:

- **R2.** `pt price load` and `pt ca sync`, given no symbols, cover every
  instrument held in the range over the days it was held, from the ledger;
  `pt instrument sync` compares every instrument ever held, as of the last day
  it was held (`services/price_history.py`).
- **R5, the adapter half.** `FafnirProvider` resolves a range segment by
  segment through `core.symbol_xref` and refuses the days a ticker named
  nothing, instead of resolving once as of the range's end -- which priced the
  earlier days of a reused ticker from the wrong issuer. Each day is asked for
  under the ticker portable recorded for it.
- **R4, partly answered by the data dictionary.** fafnir v1.5.0 documents
  NAV-priced funds (`asset_type = 'fund'`) in `core.daily_price`. Whether the
  funds this portfolio held are in the tracked universe is still a question
  for the real warehouse -- `pt instrument sync -S fafnir` now answers it.
- Every adapter query now runs under test against fafnir's documented tables,
  and an unreachable warehouse fails `pt price load` instead of warning.

- **R7, decided by the owner (2026-10-16): the flag.** An instrument marked
  as having no market price is valued at zero, at level 5, and the snapshot
  holding it is complete ([ADR 0028](0028-instrument-with-no-market-price.md)).

## Progress (2026-10-20)

- **R1, decided by the owner: fafnir through `duk`, files for the rest.**
  "duk is the primary tool for pt to source prices", run on the owner's
  machine against the warehouse over an SSH tunnel; prices fafnir does not
  hold come from CSV or JSON files. The tunnel's `fafnir_app` role cannot read
  `core`, which every adapter query named, so the route changed with it:
  [ADR 0029](0029-prices-through-duk.md). Corporate actions need a fafnir
  `mart` view, drafted there for the owner to apply.

- **R8, decided by the owner: the default benchmark is cash.** At 0%,
  labelled as exactly that, until a benchmark is assigned
  ([ADR 0030](0030-default-benchmark-is-cash-at-zero.md)). The owner is
  building a separate tool to construct benchmark series; bringing its output
  in -- the assignment command and the series import -- waits for it.

- **R10, decided by the owner on the recommendations put to them:** anchors
  are the holdings export and each year-end statement since 2022-05-18; the
  price is compared to the cent a unit, not by a percentage of value; a break
  blocks the account's returns from its date until fixed or accepted with a
  reason; the check lives in `pt reconcile`. Built as
  [ADR 0031](0031-price-acceptance-against-statements.md). The trade-price
  check (fills within the day's range; funds at NAV) flags for review and is
  still to build.

- **R3's coverage report and R9's calendar:** `pt price gaps` lists every held
  session with no price from any source, judged by the venue's trading
  calendar from fafnir's `ref.trading_calendar`, NASDAQ's where the venue has
  none, as fafnir falls back. A day outside the calendar's span is reported
  as unchecked, not as a gap or as closed. `docs/market-data.md` "Finding the
  gaps".

Still open: R3's conversion step (a vendor export into the price file
schema), R10's trade-price check, and R8's real benchmark series.

## Alternatives considered

- **Skip verification, build the full file-import pipeline now.** Rejected:
  if fafnir already covers most of this portfolio cleanly, most of R3 is
  wasted effort, and the broker-import experience (ADR 0017) shows that
  building against assumed data gaps before confirming they're real produces
  design decisions (averaging, `unavailable` baskets) that a fuller data
  source would have made unnecessary.
- **Treat the CVR as priced at the Abiomed acquisition's cash price forever.**
  Rejected: that is a basis claim with no support, exactly the "plausible
  wrong number" this repository exists to avoid — the CVR's value, if any, is
  genuinely unknown until it resolves.
- **Synthesize a total-return benchmark from a price index plus fafnir's
  dividend data.** Already rejected in ADR 0006 for the same reason it would
  be rejected here: a defensible-looking wrong number, prohibited by
  `PORT-GIPS-G01`.
