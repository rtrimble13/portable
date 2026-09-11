# Roadmap

What exists, what is coming, and in which milestone. Every item below has, or
will have, a GitHub issue; the labels are `area:core`, `area:pt`, `area:pert`,
`area:po`, `area:risky`, `area:cpp`, `area:data`, `area:docs`.

---

## v0.1 — core + `pt` · **built**

`portable_core`, the `.port` format, and a production-usable `pt`.

- The domain model: portfolios, accounts, positions spanning instruments, lots,
  the append-only ledger.
- All six relief methods, the corporate-action engine, the tax engine, the
  valuation engine, and cash-flow classification.
- Four output formats, published JSON Schemas, layered configuration, the
  `PortableError` hierarchy.
- Three market data providers, with a price cache carrying full provenance.
- C++ scaffolding proven end to end, with the pure-Python fallback contract.
- Both project lint rules, and CI across Linux and Windows.

---

## v0.2 — onboarding, then `pert`

**Three items block everything else in this milestone**, in this order:

1. **`PORT-GIPS-J05`** — the compliance-language lint. *Landed in v0.1.*
2. **`PORT-GIPS-D01`** — the `fee_class` schema change. *Landed in v0.1.*
3. **`PORT-GIPS-B02`** — cash-flow classification in one service function,
   audited against the matrix. *Landed in v0.1.*

All three were pulled forward into v0.1 precisely because retrofitting them
after returns exist would mean restating every published number.

**Then broker import, ahead of the return engine.** Pulled forward from v1.0 for
the plainest of reasons: there is nothing to compute a return on until the real
portfolio is in the file, and a return engine validated only against generated
fixtures has been validated against the easy case. Designed in
`docs/broker-import.md`; decided in ADRs 0012–0015.

0. **The replay defect** ([ADR 0016](adr/0016-out-of-order-appends-and-validation.md))
   — a back-dated append leaves derived state disagreeing with the ledger, and
   `pt validate` cannot see it because it rebuilds before it compares. This blocks
   everything below it: a historical import is out-of-order by construction.
1. **Import prerequisites** — *done*. `source` on every write path; `--ref` on
   all twenty ledger-writing commands; a `taxes_withheld` path with the
   reclaimable split and its refusals; schema **0002**, `UNIQUE (account_id,
   external_ref)` with `PT-E-DUPLICATE-REF` on every writer and a migration
   precondition that names the offending rows; and `pt reconcile` comparing per
   account and including cash, resolving by symbol, CUSIP or ISIN.
2. **The batch format** — *done*. `schemas/import-batch-1.0.json` published and
   implemented, `pt import batch` with a dry run that is the real commit rolled
   back, source-document hash checking, and refusal by name for the transaction
   types a batch cannot carry in version 1.
3. **In-kind transfers** ([ADR 0015](adr/0015-in-kind-transfers-and-opening-positions.md))
   — *done*. `transfer_in` / `transfer_out` in schema **0003**, so a position
   that predates the ledger enters it without inventing the cash flow that
   would rewrite the track record. `pt transfer in` / `pt transfer out`. Two
   numbers travel on the row and are kept apart by construction: the market
   value on the transfer date, which is the flow, and the delivering
   custodian's basis and acquisition date, which are what the tax engine uses.
   Getting the migration there needed
   [ADR 0019](adr/0019-migrations-that-rebuild-a-table.md) first — SQLite
   cannot alter a `CHECK` constraint, so a new `txn_type` value means
   rebuilding the ledger table.
3a. **Cutover reconstruction** ([ADR 0017](adr/0017-cutover-reconstruction-and-basis-provenance.md))
   — *done*. `pt import reconstruct` derives the opening position set and each
   block's basis provenance; `lot.basis_source` is `NOT NULL` with no default;
   `pt import broker` turns the reconstruction into a batch of `transfer_in`
   rows, the cash held at the cutover, and the history after it; and `pt tax`
   and `pt pnl` mark every reported figure with the rung it rests on, state the
   share of reported basis that is not this portfolio's own arithmetic, and
   **exclude an `unavailable` disposition from every total**, listing it
   separately with proceeds only and marking the year incomplete. **The
   pipeline runs end to end and reconciles to the custodian's own snapshot** —
   `docs/broker-import.md` §9, which is the only thing standing behind the
   reconstruction. Remaining: the `report_issue` row of §2b, which waits on
   report issuance (`PORT-GIPS-J01`/`J02`) being built at all.
4. **The generic tabular adapter** ([ADR 0018](adr/0018-minimum-broker-dataset.md))
   — *done*. Two required documents (a holdings snapshot with cash, and a
   transaction history), everything beyond them a declared capability whose
   absence is a named refusal rather than a quiet degradation. A custodian with
   plain tabular exports is two TOML mapping files and a fixture, no Python;
   `pt import inspect` reads them and reports the capability set with what each
   absence costs. Six named checks earn a capability, and a capability that
   fails its check does not merely go unreported — the data behind it is not
   read.
4a. **The grammar the first custodian needed** — *done*. Measured against the
   reference custodian's traps, the mapping files could not express four of
   them without Python: transaction rows carrying names rather than symbols, one
   activity word naming several events, sweep bookkeeping that had to be dropped
   by a rule which could not also swallow a real movement, and both legs of an
   internal transfer reported once per account. Each is general, so each became
   grammar (`docs/broker-import.md` §6): a crosswalk, a note key, a
   cash-equivalent whitelist, and a pairing rule. Alongside them, the two
   things a periodic update needs: rows already in the ledger are skipped at
   extract and shown as such, and `pt import broker --incremental` extends an
   account rather than seeding it twice.
4b. **The maintenance loop, and the skill that runs it** — *done*. A sale
   the custodian's lot report covers relieves exactly the lots it names
   (`lots` on the batch row); `pt reconcile --realized` ties every
   disposition to that report per sale; `scripts/xlsx_to_csv.py` opens a
   spreadsheet export at the CSV boundary; and
   `.claude/skills/broker-import/SKILL.md` is the runbook an agent follows
   with the owner — authoring the mapping files by interview, importing in
   segments around corporate actions, updating `--incremental`, and
   explaining every break before accepting. No custodian-specific Python
   anywhere.
5. **The first custodian**, as an instance of that adapter, accepted on
   reconciliation rather than on parser tests — *done*: the William Blair
   adapter, three accounts, four years of history, positions and cash
   reconciled and realized gains tied per sale. **A second custodian is the
   only real test of item 4** — the first one always fits.

Then:

1. **Daily time-weighted return engine** — chain-linked sub-period returns from
   `valuation_snapshot`, Modified Dietz as the gap-day fallback, method recorded
   on every result. `PORT-GIPS-B01`–`B07`. Note the 2020 provisions specify the
   required *outcome* and never name a method: cite the archived 2011 Guidance
   Statement on Calculation Methodology for the formula, not a provision number.
2. **Money-weighted return (XIRR)** — annualized since-inception, daily external
   cash flows, Newton with a bisection fallback, and an explicit refusal on
   pathological flow patterns rather than a plausible root. Portfolio-level MWR
   is solved from **aggregated** flows, never asset-weighted from per-account
   solves. `PORT-GIPS-C01`–`C05`.
3. **Multi-period reporting** — MTD, QTD, YTD, 1/3/5/10-year, since-inception,
   calendar-year table. Sub-one-year returns are never annualized — a
   requirement, not a convention (`PORT-GIPS-B07`).
4. **Benchmarks** — blends with explicit rebalancing rules, active return,
   tracking error, up/down capture. **Total-return series only**
   (`PORT-GIPS-G01`).
5. **Risk-adjusted metrics** — Sharpe, Sortino, information ratio, Treynor,
   Jensen's alpha, beta, M², drawdown. The three-year ex-post standard deviation
   uses **36 monthly returns** annualized ×√12, computed identically for the
   benchmark from the same months (`PORT-GIPS-F01`–`F03`).
6. **Brinson-Fachler attribution** — outside GIPS entirely; cite the
   practitioner literature and an ADR, not GIPS.
7. **Position- and security-level analysis** — contribution, turnover, win rate.
8. **After-tax performance** — following the **USIPC After-Tax Performance
   Standards**, not GIPS, which removed after-tax at the 2010 edition. Always
   supplemental information.
9. **Tearsheet** — modelled on a GIPS Asset Owner Report, with a generated
   disclosure block.

**Also v0.2, and P0:** wash-sale detection. Until it lands, `pt tax` states on
its face that it does not account for wash sales.

---

## v0.3 — `po`, optimization

Wraps [`rtrimble13/po`](https://github.com/rtrimble13/po). **Integration, not
reimplementation.**

1. Vendor `portopt` and expose it through its existing pybind11 bindings;
   reconcile the CMake dependency sets.
2. `.port` → optimizer input: expected returns, covariance, current weights.
3. Optimizer output → proposed trades, as a `pt`-consumable file, closing the
   round trip.
4. **Tax-aware optimization** — penalise realizing short-term gains, respect
   account-level tax treatment in asset location, honour spec-ID lot selection
   when generating sells. This is the differentiator.
5. Constraint surface in the CLI.
6. Efficient frontier and reporting. **Backtested results must never be linked
   to the actual track record** (`PORT-GIPS-J04`) and are labelled theoretical
   supplemental information.

---

## v0.4 — `risky`, risk and scenarios

Exposure analytics · volatility and covariance · VaR and CVaR with exception
backtesting · stress testing and historical replays · option greeks and scenario
surfaces · fixed income duration and key-rate durations · drawdown and tail
analytics.

Option risk is the likeliest candidate for the first real C++ hot path.

---

## v1.0 and cross-cutting

| Item | Priority |
|---|---|
| Multi-currency — FX as first-class data, base vs. local decomposition | P1 |
| MCP server, generated from `pt introspect` and the published schemas | P1 |
| Further broker adapters — OFX/QFX and other custodians (the first lands in v0.2) | P1 |
| Corporate action auto-ingestion from fafnir | P1 |
| C++ hot paths — **profile first** | P2 |
| Retirement account rules — contribution limits, RMDs, penalties | P2 |
| Performance composites (`PORT-GIPS-E01`–`E10`) — optional under the Asset Owner regime | P2 |
| Transaction cost analysis using fafnir intraday data | P2 |
| Portfolio rebalancing — decide whether it is a `pt` subcommand or a fifth CLI | P2 |
| Close the eight open gaps in `docs/gips-standard.md` §11.2 | P2 |

---

## Candidate C++ hot paths

Listed so nobody starts here by instinct. **Profile first** — none of these is a
good idea until a profile says so, and every one of them keeps a pure-Python
reference implementation and a differential test
([ADR 0008](adr/0008-cpp-integration-and-fallback.md)).

- lot-relief matching over long histories
- daily valuation roll-forward
- covariance estimation
- Monte Carlo simulation

---

## Things deliberately not planned

- **A GUI.** `portable` is a CLI family with machine-readable output; a UI
  belongs on top of that, not inside it.
- **Real-time data.** The domain is portfolio accounting, and end-of-day is the
  right granularity for it.
- **A compliance claim.** Not a roadmap item at any version. Compliance is an
  entity-wide assertion that cannot be made for a single portfolio or by an
  individual, and a lint rule enforces the language.
