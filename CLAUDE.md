# CLAUDE.md — Working Agreement for `portable`

Read this before touching anything in this repository. It is the standing context for
every agent session here. The one-time build instructions live in
`portable_bootstrap_prompt.md`; this file is what stays true afterward.

---

## What this is

`portable` is a family of standalone Python CLIs for investment portfolio analysis,
sharing one framework (`portable_core`), one on-disk portfolio format (`.port`, SQLite),
and one set of output conventions. Some tools are pure Python; some are Python front-ends
over C++ back-ends.

The owner is a CFA charterholder who uses these tools on a real, tax-aware, multi-account
portfolio and files real taxes from their output. **A silently wrong number is the worst
possible failure mode in this repo** — worse than a crash, worse than a missing feature.
When in doubt, refuse and explain.

| CLI | Purpose | Status |
|---|---|---|
| `pt` | Portfolio and account definition, transactions, history | **built** |
| `pert` | Performance, attribution, risk-adjusted returns | stub — see milestone `v0.2` |
| `po` | Portfolio optimization (wraps `rtrimble13/po`) | stub — see milestone `v0.3` |
| `risky` | Risk and scenario analysis | stub — see milestone `v0.4` |

**Keep this table current.** If you promote a CLI from stub to built, update it here in
the same commit.

---

## The invariants

These are not style preferences. Breaking one is a bug even if every test passes. If you
believe one is wrong, say so and propose an ADR — do not quietly work around it.

1. **No binary floating point in any money, quantity, price, or rate path.**
   `decimal.Decimal` in Python; canonical decimal `TEXT` in SQLite; never `REAL`.
   Adapters and converters are registered in exactly one place in `portable_core`.
   There is a lint rule enforcing this. Do not silence it.

2. **The transaction ledger is append-only.** Database triggers reject `UPDATE` and
   `DELETE` on `transaction`. A mistake is corrected with a reversing entry plus a new
   entry — never by editing history. This is what makes the tax trail defensible.

3. **All non-ledger state is derived and rebuildable.** Positions, lots, cash balances,
   realized gains, and valuation snapshots are materialized for speed but must be exactly
   reproducible by replaying the ledger. `pt rebuild` does this, and a test asserts that
   replay reproduces materialized state for every fixture. If you add derived state, you
   add it to the replay path and to that test in the same commit.

4. **Cash is conserved.** For every transaction, cash effects across accounts plus the
   change in cost basis plus fees must balance. This is a property-based test. If your
   change makes it fail, your change is wrong.

5. **`sum(lot.remaining_quantity) == position.quantity`.** Always, for every position, at
   every point in history. Also property-tested.

6. **Determinism.** Same inputs → identical bytes out. No wall-clock dependence, no
   locale dependence, no unordered iteration in output. Every command takes `--as-of` and
   defaults it explicitly rather than implicitly to "now". This is also what makes
   report-level error detection work — see `docs/gips-standard.md` `PORT-GIPS-J01`.

7. **Trade-date accounting**, not settlement-date. Settlement dates are recorded but do
   not drive position or P&L recognition. This is stricter than the archived T+3
   accommodation and exactly matches the current standard (`PORT-GIPS-A05`).

8. **Structured output to stdout, everything else to stderr.** `--format json` output is
   schema-stable, versioned, and validated against `schemas/` in CI.

9. **Fail loudly on ambiguity.** Unmatched lots, stale prices beyond tolerance, fractional
   shares an account cannot hold, reconciliation breaks — stop, explain precisely, exit
   non-zero. Provide an explicit flag where a human can legitimately decide. Never guess.

10. **No stubs that pretend to work.** `NotImplementedError` with a link to its issue is
    honest. A function that returns zero, an empty list, or a plausible-looking default is
    a landmine that will surface as a wrong number months later.

11. **Never claim GIPS compliance, in any form.** `portable` implements performance
    methodology *modelled on* the 2020 GIPS standards. It is not, and cannot be,
    GIPS-compliant: compliance is an entity-wide assertion and "cannot be met on a  <!-- gips-lint: allow -->
    composite, pooled fund, or portfolio basis" (GIPS for Firms 1.A.1; for Asset Owners
    21.A.1), and the standards do not apply to individuals. The phrases
    **"GIPS-compliant"**, **"GIPS-consistent"**, **"in compliance with"**, **"in accordance  <!-- gips-lint: allow -->
    with"**, and **"consistent with the GIPS standards"** are prohibited in source, docs,  <!-- gips-lint: allow -->
    templates, fixtures, and output — GIPS 1.A.9 names that last one verbatim. There is a
    lint rule. Do not silence it. The one approved disclaimer is in
    `docs/gips-standard.md` §9.3. The rule allow-lists three things and nothing else:
    `docs/gips-standard.md`, the approved disclaimer wherever it appears, and any line
    carrying an explicit `gips-lint: allow` marker — which exists so that this invariant,
    and the lint rule's own test fixture, can name the prohibited phrases in order to
    forbid them. Adding a marker anywhere else is silencing the rule.

---

## Where things go

```
src/portable_core/
  domain/        # typed domain objects — no I/O, no SQL, no business rules
  services/      # business logic: LotEngine, PositionEngine, TaxEngine,
                 # ValuationEngine, CorporateActionEngine, DisclosureEngine
  persistence/   # repositories — the only place SQL lives
  schema/        # versioned DDL files + migrations
  providers/     # MarketDataProvider interface + Fafnir/File/Null implementations
  formatters/    # table / json / markdown / csv — number presentation rules live here
  config/        # layered configuration resolution
  errors/        # PortableError hierarchy, stable error codes
  cli/           # shared CLI plumbing every tool builds on
src/portable_pt/         # the pt CLI — thin
src/portable_pert/ po/ risky/   # stubs
cpp/                     # CMake + pybind11 modules, Catch2 tests
tests/unit/ integration/ property/ fixtures/
schemas/                 # JSON Schema per command output
docs/  docs/adr/         # architecture, domain model, tax methodology, GIPS, decisions
```

**Placement rules:**

- Business logic goes in `services/`, never in a CLI module and never in a repository.
- If two CLIs would need the same logic, it belongs in `portable_core`. **No CLI imports
  another CLI**, ever.
- SQL appears only in `persistence/` and `schema/`. If you are writing a query elsewhere,
  you are in the wrong file.
- Domain objects have no I/O. If a domain object needs a price, it is passed one.
- Anything touching fafnir's schema or `duk` lives in `providers/fafnir.py` and nowhere
  else. `portable` must never couple to fafnir internals outside that module.
- New output formatting rules go in `formatters/`, once, not inline at call sites.
- **Valuation, cash-flow, and return logic must satisfy `docs/gips-standard.md`.** Cite the
  requirement ID (`PORT-GIPS-B02`, `PORT-GIPS-F01`, …) in the code comment and in the test
  name, so the standard doubles as a coverage map. Cash-flow classification lives in
  exactly one service function and is never re-derived at a call site.

---

## Domain glossary

Use these words precisely; the code does.

- **Portfolio** — one `.port` file. Contains everything needed for analysis. Maps to what
  GIPS calls a **total fund**.
- **Account** — holds positions and cash. All transactions happen here. Tax-aware:
  `taxable` / `tax_deferred` / `tax_exempt`, with effective-dated rate schedules.
  Tracks P&L **net of tax**.
- **Position** — a holding, possibly spanning **multiple instruments** (a spread, a
  covered call, a collar). Tracks P&L **independent of tax**. Legs bind instruments to
  a position with a role.
- **Lot** — created by an opening transaction, consumed by closing ones. The tax engine's
  atom. Carries basis, holding-period start, and a log of every basis adjustment.
- **Transaction** — an immutable ledger event. Position-aware: opening if it creates or
  adds to a position, closing if it reduces or liquidates.
- **Relief method** — how closing trades consume lots. Default **spec-ID**; also FIFO,
  LIFO, HIFO, LOFO, average cost. Per-account default, per-trade override.
- **Valuation snapshot** — per account per date: beginning MV, ending MV, accrued income,
  external cash flows and their timing. The substrate `pert` needs. Every price
  records its source and as-of timestamp.
- **External cash flow** — capital entering or leaving. **Income is not an external cash
  flow**, and an internal transfer nets to zero at portfolio level while counting at
  account level. See the trap below and `PORT-GIPS-B02`.
- **Return basis** — gross-of-fees, net-of-external-costs-only, or net-of-fees. Which fees
  are deducted differs by basis and is not a matter of taste (`PORT-GIPS-D01`).

---

## Domain traps

These are the mistakes that produce plausible, wrong numbers. Most have already bitten
somebody. Check yourself against this list when touching the tax or performance paths.

**Holding period.** Long-term requires *more than one year* measured from the **day after**
acquisition to the disposition date. The one-year-exactly boundary is short-term. Leap
years matter. There are tests on this boundary — do not "simplify" them.

**Short sales are always short-term** regardless of how long the position is held.

**Corporate actions and holding period.** A split does *not* reset the holding period. A
spinoff allocates basis by relative fair market value and the new shares inherit the
original holding period. Getting this wrong changes tax rates, not just cosmetics.

**Return of capital reduces basis**; it is not income. Once basis reaches zero it becomes
capital gain. Note it is still *not* an external cash flow for performance purposes — the
tax treatment and the flow treatment are different questions.

**Option premium.** A written call that is assigned adds premium to *proceeds* on the
stock sale. A long call that is exercised adds premium to the acquired stock's *basis*. A
written option that expires worthless is short-term gain regardless of how long it was
open. Do not treat premium as independent P&L when the option resolves into stock.

**Ex-date vs. pay-date.** Entitlement is determined on the ex-date; cash arrives on the
pay-date. Both are recorded. Accruing on the wrong one shifts returns across period
boundaries. GIPS recommends accruing dividends as of the ex-date (`PORT-GIPS-A06`).

**Adjusted vs. unadjusted prices.** fafnir keeps unadjusted OHLCV in `core.*` and adjusted
prices in `mart.*`. Portfolio accounting uses **unadjusted** prices with explicit
corporate-action transactions — never adjusted prices, or you will double-count splits.
Performance and return series may use adjusted. Know which you are asking for.

**Accrued interest is part of market value** for bonds. A bond bought between coupons pays
accrued interest to the seller; that is not basis. Accrual for interest-bearing
instruments is *required*; accrual for dividends is *recommended* (`PORT-GIPS-A06`).

**External cash flow classification is level-dependent, and it is the easiest way to
produce a plausible wrong return.** Income — dividends, coupons, reinvestments — is
**never** an external cash flow. A transfer between two of the owner's own accounts **is**
an external flow at account level and **is not** at portfolio level, because it nets to
zero. Treat it as external at portfolio level and a $100k shuffle between your own
accounts silently rewrites your track record. The full matrix is `PORT-GIPS-B02`; the
classification lives in one service function and is tested against that matrix verbatim.

**Large cash flow and significant cash flow are different thresholds.** A *large* flow
triggers revaluation and a sub-period return. A *significant* flow triggers temporary
removal of a portfolio from a composite. GIPS defines both terms but supplies **no
number** for either — the threshold is a stored, effective-dated policy value, and a
missing policy is an error, not a zero (`PORT-GIPS-B03`, `PORT-GIPS-E09`).

**TWR vs. MWR.** Time-weighted removes the effect of client cash flows and measures the
manager. Money-weighted includes them and measures the investor's actual experience. They
are different numbers answering different questions and neither is "more correct". Label
every return with its **method, basis, and period**. **TWR leads**: GIPS requires
time-weighted returns and permits money-weighted only in addition, and only when a
two-limb gate is met that an open-ended personal portfolio does not satisfy
(`PORT-GIPS-B01`, `PORT-GIPS-C01`). Never present MWR alone.

**Do not annualize sub-one-year returns.** This is not convention — it is an unconditional
requirement, GIPS for Firms 2.A.12 and for Asset Owners 22.A.9, and it binds the
since-inception money-weighted return too. Enforce it in the formatter so no call site can
bypass it (`PORT-GIPS-B07`, `PORT-GIPS-C03`).

**Three-year standard deviation uses 36 *monthly* returns.** Not daily scaled by √252, not
weekly. Monthly, annualized by ×√12, computed the same way for the benchmark, from the
same 36 months. When 36 months are unavailable, the absence is *disclosed*, never rendered
as a blank or a zero (`PORT-GIPS-F01`, `PORT-GIPS-F02`).

**Price-only benchmarks are prohibited.** The S&P 500 price index is far easier to obtain
than the total-return index and understates the benchmark by roughly the dividend yield
every year — flattering the portfolio by 1.5–2% annually. Benchmarks must be total-return
series, and `portable` refuses a price-only series rather than warning (`PORT-GIPS-G01`).

**Custody fees are not transaction costs.** They are administrative under the Firms
ladder and fall inside *investment management costs* under the Asset Owner ladder, which
`portable` follows — so they reduce net-of-fees returns and nothing else. Fee
classification is a stored enum, not an inference at call time (`PORT-GIPS-D01`).

**Do not double-deduct fund expenses.** ETF and mutual fund NAVs are already net of the
fund's expenses. GIPS requires those expenses to be reflected; they already are. "Correcting"
for an expense ratio on top of NAV-based returns is a silently wrong number
(`PORT-GIPS-D05`).

**Wash sales cross accounts.** The 30-day window spans *all* of the taxpayer's accounts,
including IRAs, and covers substantially identical securities and options. Wash-sale
detection is deferred to `v0.2` — until then, the tax report must state plainly that it
does not account for wash sales.

**Nothing here is tax advice**, and this tool is not a substitute for a broker's 1099-B.
Reports say so. Separately, **after-tax performance is outside GIPS entirely** — removed at
the 2010 edition and handed to the US country sponsor. After-tax returns are supplemental
information and follow the USIPC After-Tax Performance Standards, not GIPS
(`docs/gips-standard.md` §7.1).

---

## Conventions

**Python.** 3.11+, stdlib `venv` and `pip`. `src/` layout, editable install. Typer for
CLIs, Rich for human output. ruff for lint and format. mypy strict on `portable_core`.
Type-annotate everything public.

**CLI shape.** `pt <noun> <verb>`, consistent flags. Every mutating command supports
`--dry-run` and `--yes`. Every command supports `--format`, `--as-of`, `-v/-vv`,
`--quiet`, `--no-color`. Anything that could prompt must have a flag that supplies the
answer — non-interactive operation is a requirement, not a nicety.

**Exit codes.** `0` ok · `1` generic · `2` usage · `3` portfolio/file · `4` validation
· `5` data unavailable · `6` reconciliation break. Keep `--help` and the README in sync.

**Errors.** Raise from the `PortableError` hierarchy with a stable code
(`PT-E-LOT-UNMATCHED`), a human message, and structured context. Errors render as JSON
when `--format json` is active. Never let a bare exception reach the user.

**Output.** Full precision in `json` and `csv`; formatted in `table` and `markdown`.
`Decimal` serializes as a string in JSON, never as a float. Explicit `null` — never let
blank and zero mean the same thing. Every performance report carries the disclaimer from
`docs/gips-standard.md` §9.3; in `--format json` it is an envelope field, not a rendered
string, so a consumer cannot drop it without noticing.

**Commits.** Conventional Commits, small and reviewable, message explains *why*. Tests
land with the code they test, not after.

**C++.** CMake + pybind11 + Catch2, matching `rtrimble13/po`'s conventions so that
integrating it later is a merge. **Every C++ path keeps a pure-Python reference
implementation** and a differential test comparing them. No exceptions — that fallback is
how we know the fast path is right.

---

## Running things

```bash
scripts/bootstrap.sh          # or scripts\bootstrap.ps1 on Windows — creates the venv
make lint                     # ruff check + format --check + the no-float and
                              # GIPS-language lint rules
make types                    # mypy
make test                     # full pytest suite incl. property + integration
make test-fast                # unit only — the pre-commit subset
make cpp                      # configure, build, and run Catch2
make schemas                  # regenerate + validate JSON Schemas
make docs                     # regenerate docs/schema.md from DDL comments
make fixtures                 # rebuild examples/sample.port from its generator
make check                    # everything CI runs
```

Also available: `make install`, `make format`, `make coverage`, `make clean`.

`examples/sample.port` is **generated, not hand-crafted** — a hand-made fixture
drifts from what the code produces and quietly stops testing anything. Rebuild it
with `make fixtures`; it is deterministic, so two runs produce identical bytes.

The owner works on **Windows**. CI runs Linux and Windows. Do not introduce
POSIX-only assumptions — paths, shell invocations, or line endings.

**Before declaring any task done:** `make check` is green, and any new invariant has a
test that fails without your change.

---

## How to work here

- **Check `docs/adr/` before redesigning anything.** Decisions have reasons, and the
  reasons are written down. If you disagree, write the next ADR — do not silently revert.
- **Check `docs/gips-standard.md` before touching valuation, cash flows, returns, risk
  measures, or benchmarks.** It is provision-traceable to the 2020 GIPS standards and each
  requirement carries its own acceptance test. If you believe a requirement there is wrong,
  the argument has to be made against the cited provision, not around it.
- **Ask before assuming** on anything material and hard to reverse. Decide, write a
  one-page ADR, surface it in your summary.
- **Check in at phase boundaries** on multi-step work, especially before a schema change.
- **Schema changes require a migration**, tested in both directions where reversible, plus
  a bump to `schema_version` and a note in `CHANGELOG.md`.
- **When this document is wrong, fix it** in the same commit that makes it wrong. A stale
  CLAUDE.md is worse than none. The same rule binds `docs/gips-standard.md`.
- **Push back.** If the design is wrong, say so and make the case. In a domain this
  fiddly, agreeable compliance is not the helpful behavior.

---

## Reference

- `docs/adr/` — **read this before redesigning anything.** The decisions the
  bootstrap left open, each with its reasoning:

  | ADR | Decision |
  |---|---|
  | 0002 | Raw SQL behind repositories, not an ORM |
  | 0003 | Frozen dataclasses for domain objects, not Pydantic |
  | 0004 | Instrument subtype detail tables, not a JSON column |
  | 0005 | Decimal representation, storage, and the seven rounding boundaries |
  | 0006 | fafnir access path — and the two capabilities it does not have |
  | 0007 | Cash-flow classification as one level-aware function |
  | 0008 | C++ optional extension, with a mandatory Python reference |
  | 0009 | A position spans instruments; lots hang off legs |
  | 0010 | Derived state, the replay contract, and what `pt rebuild` guarantees |
  | 0011 | What the tax engine computes exactly, estimates, and refuses |

- `docs/architecture.md` — how the pieces fit, how to add a module
- `docs/domain-model.md` — the concepts, written for a portfolio manager
- `docs/gips-standard.md` — **the performance-measurement standard.** Provision-traceable
  to the 2020 GIPS standards (Firms and Asset Owners), 50 numbered requirements with
  acceptance tests, the compliance boundary, and a verification register recording what
  was checked and what is still open. Binds `pert` entirely and parts of `pt`.
- `docs/tax-methodology.md` — relief methods, holding periods, basis adjustments,
  what is estimated vs. exact
- `docs/port-format.md`, `docs/schema.md` — the `.port` file
- `docs/market-data.md` — provider interface, fafnir adapter, cache and staleness
- `docs/output-formats.md` — JSON envelope, versioning, agent/MCP integration
- `docs/roadmap.md` — what is coming and in which milestone
- Prior art: [fafnir](https://github.com/rtrimble13/fafnir) (market data warehouse,
  Postgres + `duk`) · [po/portopt](https://github.com/rtrimble13/po) (C++17 optimizer
  with pybind11 bindings)
