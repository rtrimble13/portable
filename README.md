# portable

A family of standalone command-line tools for investment portfolio analysis,
sharing one framework, one on-disk portfolio format, and one set of output
conventions.

`portable` is built around a claim: **the domain model is the product.** The CLIs
are thin. The value is a portfolio accounting core that is right about lots,
taxes, corporate actions, and cash flows — everything downstream is arithmetic on
top of a correct book of record.

Its governing rule is that **a silently wrong number is the worst possible
failure mode**, worse than a crash and worse than a missing feature. When
`portable` cannot tell whether an answer is right, it refuses and explains.

---

## The CLI family

| CLI | Purpose | Status |
|---|---|---|
| **`pt`** | Portfolio and account definition, transactions, history | **built** (v0.1) |
| `pert` | Performance, attribution, risk-adjusted returns | stub — milestone `v0.2` |
| `po` | Portfolio optimization (wraps [`rtrimble13/po`](https://github.com/rtrimble13/po)) | stub — milestone `v0.3` |
| `risky` | Risk and scenario analysis | stub — milestone `v0.4` |

Each is standalone. No CLI imports another; shared logic lives in
`portable_core`.

---

## Install

Requires Python 3.11+. A C++ compiler and CMake are optional (see below).

```bash
git clone https://github.com/rtrimble13/portable
cd portable
scripts/bootstrap.sh          # or scripts\bootstrap.ps1 on Windows
```

That creates a virtualenv, installs the pinned dependencies, and installs
`portable` editable. Then:

```bash
source .venv/bin/activate     # or .venv\Scripts\Activate.ps1
pt --version
```

**The compiled extension is optional.** `PORTABLE_BUILD_NATIVE=OFF pip install .`
produces a complete, correct, pure-Python install — not a degraded one. Every
C++ path in `portable` keeps a pure-Python reference implementation, and a
differential test compares them; that fallback is how we know the fast path is
right. See [ADR 0008](docs/adr/0008-cpp-integration-and-fallback.md).

---

## Sixty-second quickstart

```bash
# A portfolio is one file. Everything needed for analysis is inside it.
pt init demo.port --name "Demo" --inception 2024-01-02 --base-currency USD

# Accounts hold positions and cash. All transactions happen in an account.
pt --port demo.port account add --name "Brokerage" --type taxable \
     --custodian "Example Broker" --opened 2024-01-02
pt --port demo.port account tax-rates set --account Brokerage \
     --short 0.37 --long 0.20 --state 0.05 --niit 0.038 --effective-from 2024-01-01

# Fund it and buy something.
pt --port demo.port cash deposit --account Brokerage --amount 100000 --date 2024-01-02
pt --port demo.port buy AAPL --qty 100 --price 185.64 --date 2024-01-03 \
     --account Brokerage --fees 1.00 --fee-class transaction_cost

# Price it and value it. Prices carry their source and as-of; snapshots record
# exactly which prices they consumed.
pt --port demo.port price set AAPL --date 2024-03-28 --price 171.48 --source manual
pt --port demo.port value --date 2024-03-28

# Ask it things. Every command speaks JSON as well as it speaks terminal.
pt --port demo.port holdings --as-of 2024-03-28
pt --port demo.port pnl --unrealized --format json
pt --port demo.port tax --year 2024
```

A full worked example, with options, a bond, a split, and a spinoff, is in
[`examples/walkthrough.md`](examples/walkthrough.md) and runs against the
generated [`examples/sample.port`](examples/).

---

## What makes the numbers trustworthy

These are the properties `portable` is built to have. Each is tested, and each
is a thing you can check rather than a thing you have to believe.

- **The transaction ledger is append-only.** Database triggers reject `UPDATE`
  and `DELETE`. A mistake is corrected with a reversing entry plus a new entry,
  never by editing history.
- **Everything else is derived and rebuildable.** Positions, lots, balances, and
  valuations are materialized for speed but reproducible exactly by replaying
  the ledger. `pt rebuild` does it; a test asserts it for every fixture.
- **No binary floating point anywhere near money.** `Decimal` in Python,
  canonical decimal `TEXT` in SQLite. A lint rule enforces it and may not be
  silenced.
- **Determinism.** Same inputs, identical bytes out. Every command takes
  `--as-of` and defaults it explicitly rather than implicitly to "now".
- **Cash is conserved**, and `sum(lot.remaining_quantity) == leg.quantity`. Both
  are property-based tests over generated transaction sequences.
- **Trade-date accounting.** Settlement dates are recorded but do not drive
  recognition.
- **It fails loudly.** Unmatched lots, stale prices, a corporate action implying
  a fractional share the account cannot hold, a missing cash-flow policy — each
  stops, explains precisely, and exits non-zero. There is always an explicit
  flag where a human can legitimately decide.

---

## Machine-first output

Every command is usable by a human at a terminal *and* by an agent parsing
stdout. Structured results go to **stdout**; logs, warnings, and progress go to
**stderr**.

```bash
pt --port demo.port holdings --format json     # schema-stable, versioned
pt --port demo.port holdings --format csv      # RFC 4180, full precision
pt --port demo.port holdings --format markdown # paste into notes or an LLM
pt --port demo.port introspect --format json   # the whole command tree
```

`Decimal` serializes as a **string**, never a float. `--format json` output is
validated against [`schemas/`](schemas/) in CI. `pt introspect` emits the
complete command tree — arguments, types, defaults, help, output schema
references — which is what makes MCP integration a wrapper rather than a rewrite.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | generic error |
| `2` | usage error |
| `3` | portfolio/file error (missing, locked, wrong schema version) |
| `4` | validation failure (invariant broken, unmatched lots, missing return policy) |
| `5` | data unavailable (provider down, price stale beyond tolerance) |
| `6` | reconciliation break |

---

## Market data

`portable` reads prices through a provider interface with separately declarable
capabilities, so a partial provider is legal and its gaps are visible:

- **`FafnirProvider`** — the primary, reading the owner's
  [fafnir](https://github.com/rtrimble13/fafnir) warehouse. **Unadjusted prices
  only**, with explicit corporate-action transactions; adjusted prices are not
  fair values on the measurement date and would double-count splits.
- **`FileProvider`** — local CSV/JSON with a documented schema. A first-class
  citizen, not a test double: it is how benchmarks get in and how the repo is
  usable offline.
- **`NullProvider`** — refuses politely, so a command needing a price fails with
  a good message.

Every price is cached in the `.port` file with its source, as-of timestamp,
valuation level, and estimate flag, and every valuation snapshot records the
exact set of prices it consumed — so a return traces back to the ticks that
produced it. `--offline` forces cache-only operation.

See [`docs/market-data.md`](docs/market-data.md) and
[ADR 0006](docs/adr/0006-fafnir-access-path.md), which also records the two
things fafnir cannot supply and what `portable` does instead.

---

## Documentation

| Document | What it is |
|---|---|
| [`docs/domain-model.md`](docs/domain-model.md) | The concepts, written for a portfolio manager |
| [`docs/architecture.md`](docs/architecture.md) | How the pieces fit; how to add a module |
| [`docs/gips-standard.md`](docs/gips-standard.md) | The performance-measurement standard: 50 numbered requirements, provision-traceable to the 2020 GIPS standards |
| [`docs/tax-methodology.md`](docs/tax-methodology.md) | Relief methods, holding periods, basis adjustments; what is exact and what is estimated |
| [`docs/port-format.md`](docs/port-format.md) · [`docs/schema.md`](docs/schema.md) | The `.port` file |
| [`docs/market-data.md`](docs/market-data.md) | Provider interface, fafnir adapter, cache and staleness |
| [`docs/output-formats.md`](docs/output-formats.md) | JSON envelope, versioning, agent/MCP integration |
| [`docs/adr/`](docs/adr/) | Why things are the way they are |
| [`docs/roadmap.md`](docs/roadmap.md) | What is coming, in which milestone |
| [`CLAUDE.md`](CLAUDE.md) | The standing working agreement for agent sessions |

---

## Development

```bash
make lint        # ruff + the no-float and GIPS-language rules
make types       # mypy, strict on portable_core
make test        # unit + property + integration + golden
make test-fast   # unit only -- the pre-commit subset
make cpp         # configure, build, run Catch2
make schemas     # validate JSON Schemas against real command output
make docs        # regenerate docs/schema.md from the DDL comments
make check       # everything CI runs
```

CI runs Linux and Windows across Python 3.11 and 3.12. Contributions:
[`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## On performance methodology

`portable` implements valuation, cash-flow, and return methodology **modelled
on** the 2020 Global Investment Performance Standards published by CFA
Institute, specified requirement by requirement in
[`docs/gips-standard.md`](docs/gips-standard.md).

**No claim of GIPS compliance is made or implied, and none could be.** Compliance
is an entity-wide assertion that "cannot be met on a composite, pooled fund, or
portfolio basis" (Firms 1.A.1; Asset Owners 21.A.1), and the standards do not
apply to individuals. A lint rule enforces the language and may not be silenced.
GIPS® is a registered trademark of CFA Institute, which does not endorse this
tool.

**Nothing in `portable` is tax advice**, and it is not a substitute for a
broker's 1099-B. Wash-sale detection is deferred to v0.2, and until it lands the
tax report says so on its face.

---

## License

MIT. See [`LICENSE`](LICENSE).
