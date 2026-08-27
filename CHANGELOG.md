# Changelog

All notable changes to `portable` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Two rules specific to this repository:

- **A schema change gets an entry here and a `schema_version` bump**, in the
  same commit as its migration (`CLAUDE.md`).
- **A change to a `PORT-GIPS-xxx` requirement gets an entry here either way**,
  and an ADR if it changes an implementation obligation
  (`docs/gips-standard.md` §13).

## [Unreleased]

### Added

- Repository scaffolding: `pyproject.toml` (scikit-build-core), pinned
  `requirements*.txt` plus `constraints.txt`, `Makefile`, pre-commit hooks, and
  `scripts/bootstrap.{sh,ps1}` for Linux and Windows.
- **Both project lint rules**, before there was any prose to police:
  - `no-float-in-money-paths` — flags float literals, `float` annotations and
    casts in `src/`, and `REAL`/`FLOAT`/`DOUBLE`/`NUMERIC` SQL column types.
    `NUMERIC` is included deliberately: SQLite's affinity silently stores it as
    `REAL`. A `# no-float: allow` marker requires a stated reason and has **no
    effect at all** inside a money-critical package.
  - `no-GIPS-compliance-language` (`PORT-GIPS-J05`) — allow-lists exactly three
    things: `docs/gips-standard.md`, the approved disclaimer wherever it
    appears, and a line carrying `gips-lint: allow`.
- `portable_core.disclaimer` — the single approved form of words, with the
  wrapping constraint that keeps it recognisable to the lint rule that protects
  it.
- C++ scaffolding: CMake + pybind11 + Catch2 matching `rtrimble13/po`'s
  conventions, a `portable_native` proof-of-concept module, and the
  native/Python fallback dispatch with differential tests.
- Stub CLIs for `pert`, `po`, and `risky` that print what they will do and exit
  non-zero. No function returns a plausible default.
- CI: lint/types/project rules, the suite across Linux and Windows × Python
  3.11/3.12, a pure-Python job proving `PORTABLE_BUILD_NATIVE=OFF` still works,
  and the C++ build with Catch2 on both platforms.
- Documentation: `docs/architecture.md`, `docs/domain-model.md`, and eleven
  ADRs covering every decision the bootstrap prompt left open.

### Changed

- `CLAUDE.md` invariant 11 carries `gips-lint: allow` markers on the three lines
  that name the prohibited phrases in order to forbid them. This is the case the
  marker exists for, and `CLAUDE.md` says so itself.

## [0.1.0] — unreleased

Initial release: `portable_core`, the `.port` format, and `pt`.

### Core

- **Domain model** — portfolios, accounts, positions that span instruments,
  lots, and an append-only transaction ledger. Frozen dataclasses with a
  runtime guard that rejects a `float` in any money field.
- **Schema**, 30 tables, including all ten `PORT-GIPS` §5.1 objects in the first
  version rather than retrofitted. `UPDATE` and `DELETE` on `transaction` abort
  by trigger; a fee with a `NULL` `fee_class` is rejected;
  `benchmark.return_type` is `NOT NULL` with no default.
- **Decimal boundary** — canonical `TEXT` storage, one arithmetic context, and
  largest-remainder allocation so a split total never loses a cent.
- **Services** — `LotEngine` (six relief methods), `PositionEngine`,
  `TaxEngine`, `CorporateActionEngine`, `ValuationEngine`, `ReplayEngine`, and
  cash-flow classification as one level-aware function.
- **Formatters** — `table`, `json`, `markdown`, `csv`, with the two return rules
  (`PORT-GIPS-B07`, `H04`) enforced where no call site can bypass them.
- **Providers** — `FafnirProvider` (unadjusted prices only, no benchmark
  capability), `FileProvider`, `NullProvider`, with capability protocols so a
  partial provider's gaps are visible.
- **Config** — five layers with provenance and secret redaction.
- **Errors** — 38 stable codes and seven exit codes.

### `pt`

Portfolio, account, instrument, trading, cash, income, corporate action,
options-lifecycle, pricing, valuation, position, lot, policy, reporting, query
and introspect commands. Global flags work before *or* after the subcommand.

### Documentation

`architecture.md`, `domain-model.md`, `tax-methodology.md`, `port-format.md`,
`schema.md` (generated), `market-data.md`, `output-formats.md`, `roadmap.md`,
eleven ADRs, and a worked `examples/walkthrough.md` whose every command was run
and whose output was checked.

### Known gaps

Tracked as issues, not hidden: wash-sale detection (v0.2, P0), several `pt`
commands from the bootstrap surface, and `--offline` not yet enforced in the
provider path.
