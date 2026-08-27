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

## v0.2 — `pert`, performance

**Three items block everything else in this milestone**, in this order:

1. **`PORT-GIPS-J05`** — the compliance-language lint. *Landed in v0.1.*
2. **`PORT-GIPS-D01`** — the `fee_class` schema change. *Landed in v0.1.*
3. **`PORT-GIPS-B02`** — cash-flow classification in one service function,
   audited against the matrix. *Landed in v0.1.*

All three were pulled forward into v0.1 precisely because retrofitting them
after returns exist would mean restating every published number.

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
| Broker import adapters — OFX/QFX and per-broker CSV, with duplicate detection | P1 |
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
