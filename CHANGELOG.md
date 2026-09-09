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
- `docs/broker-import.md` — the design for turning custodian exports into ledger
  rows: the three-stage pipeline, the canonical batch format, the activity and
  instrument maps as reviewed data files, the refusal list, and reconciliation as
  the acceptance criterion. **Design only; nothing is implemented.**
- Five ADRs for the decisions that design rests on:
  - **0012** — import is a staged pipeline with a reviewable batch file between
    extraction and commit, because the ledger is append-only and the cheap place to
    catch a bad row is before it is written.
  - **0013** — a cash sweep vehicle is cash. Its transfer rows are discarded at
    import (a third of the sample export), its income is income on the cash
    balance, and `account.sweep_instrument_id` is dropped rather than left as a
    column no code reads.
  - **0014** — an adviser fee billed to one account and settled from another is a
    `transfer` plus a `fee`, never two fees; an unpaired settlement to an account
    outside the portfolio is a `withdrawal`, never a fee.
  - **0015** — `transfer_in` / `transfer_out` for securities crossing the portfolio
    boundary, carrying the lot's original basis and acquisition date separately
    from the market value that forms the flow. This is how a position acquired
    before the ledger begins enters it without inventing a cash flow.
  - **0016** — a back-dated append forces a full rebuild in the same transaction,
    and `pt validate` digests stored derived state *before* rebuilding so that it
    compares stored against replayed rather than one rebuild against another.
  - **0017** — the opening position set is reconstructed by rolling the
    transaction export backwards from the dated holdings snapshot, and every lot
    carries a `basis_source` (`derived` · `reconstructed` · `estimated` ·
    `custodian_asserted`), `NOT NULL` with no default, which `pt tax` and
    `pt pnl` disclose. Written after it was established that no further broker
    report is obtainable: it **amends ADR 0015**, whose flat refusal on averaged
    basis would, on that evidence, have declined to build the portfolio at all
    rather than declining to guess. The rule is now that approximation is
    permitted and concealment is not. The relief-method assumption is FIFO, and
    the ADR records how little any such assumption reaches: of 70 positions held
    at the sample cutover, 38 are exactly reconstructed, FIFO anchors 6 more, and
    26 have no surviving remainder to anchor against and so get
    `basis_source = 'unavailable'`. Those are excluded from every `pt tax` total
    and reported separately with the year marked incomplete, rather than shown as
    a gain measured from an arbitrary date.
  - **0018** — the generalisation. Any import requires exactly two documents: a
    holdings snapshot carrying cash, and a transaction history. Everything else —
    cost basis, acquisition dates, lot detail, transaction identifiers, symbols,
    settlement dates, a flows report, history reaching inception — is an
    `ImportCapability` the adapter declares, mirroring `providers.Capability`, and
    its absence is a refusal that names the missing input rather than a quiet
    degradation. A capability is declared only on **validated** data: a column is
    not a capability, and a custodian emitting a settlement-date column full of
    impossible dates has not supplied settlement dates. Adapters are two TOML
    mapping files plus a fixture, with Python reserved for data that needs logic a
    mapping cannot express; ADRs 0013 and 0014 are re-scoped accordingly, and
    `account.sweep_instrument_id` becomes a declared *set* of cash-equivalent
    identifiers, since one nullable column does not survive an account that sweeps
    to two vehicles.

### Changed

- `CLAUDE.md` invariant 11 carries `gips-lint: allow` markers on the three lines
  that name the prohibited phrases in order to forbid them. This is the case the
  marker exists for, and `CLAUDE.md` says so itself.
- `docs/roadmap.md` — broker import pulled forward from v1.0 to the head of v0.2,
  ahead of the return engine, because there is nothing to compute a return on
  until the real portfolio is loaded.
- `docs/broker-import.md` — reworked once it was established that no further
  broker report is available. Reporting inception becomes the transaction
  export's first date rather than the date the accounts were funded; the
  capital-flows export contributes no ledger rows and becomes `portfolio_event`
  documentation plus a cross-check; §4 records what the reconstruction recovers
  exactly (position quantities, and about 83% of pre-cutover cost basis) and what
  it can only estimate. Then restructured to lead with the custodian-neutral
  contract — required documents, capabilities, canonical records, the adapter
  contract, the reconstruction, refusals, acceptance — with the reference
  custodian moved into a worked example that declares four of the nine
  capabilities and notes, per trap, which ones generalise.

### Found, not yet fixed

- **A back-dated ledger append leaves derived state disagreeing with the ledger**,
  and `pt validate` reports no problem because it rebuilds before it compares —
  measuring idempotence rather than the fidelity `CLAUDE.md` invariant 3 requires.
  Reproduced with a three-transaction portfolio in which the live realized gain and
  the gain after `pt rebuild` differ. ADR 0016 records the fix; it lands in v0.2
  ahead of any import work.

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
