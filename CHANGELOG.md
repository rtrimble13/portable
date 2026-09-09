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

### Schema

- **`schema_version` 2 → 3**, migration `0003_in_kind_transfers_and_basis_source`.
  **The first migration that rebuilds a table** ([ADR 0019](docs/adr/0019-migrations-that-rebuild-a-table.md)).
  - **`transfer_in` / `transfer_out`** on `txn_type`, plus `original_basis`,
    `original_acquired_date`, `basis_source` and `basis_assumption` on
    `"transaction"` — bound by `CHECK` to those two types (ADR 0015).
  - **`lot.basis_source NOT NULL`, no default** (ADR 0017). No writer can add a
    lot without answering where the basis came from; a default would defeat the
    column. `lot` is dropped and recreated rather than altered, because it is
    derived state and SQLite cannot add a `NOT NULL` column with no default
    anyway. A migrated file has no lots until `pt rebuild` — reported by
    `pt validate`, not silent.
  - **Why a rebuild at all, measured rather than asserted.** SQLite cannot
    alter a `CHECK` constraint, and the obvious statement of the problem is
    wrong: the tables holding foreign keys into the ledger — `lot`, `position`,
    `realized_gain` — are all derived, so a migration can clear them first. The
    actual blocker is the ledger's **self-references**, `related_txn_id` and
    `reverses_txn_id`, which are ledger data and cannot be cleared. With the
    rebuild marker removed, 0003's own SQL succeeds on a file with no
    reversals and fails with `FOREIGN KEY constraint failed` on one with a
    single reversal. That asymmetry is the worst shape a bug can have — green
    on the fixture and on every new file, broken on the owner's real portfolio
    — and there is a test pinning it.
  - Migrations declare a rebuild with a `-- portable:rebuild` marker inside the
    checksummed text, so one cannot be quietly promoted after being applied.
    Rebuild mode sets `PRAGMA foreign_keys = OFF` **outside** the transaction
    (it is a documented no-op inside one, which is why the runner had to change
    first) and restores it in a `finally` — connection state left off would
    make every later write on that connection skip referential integrity.
  - The enforcement switched off is **reinstated, not skipped**:
    `PRAGMA foreign_key_check` runs inside the transaction before commit, and
    the trigger inventory is compared across the rebuild. A file that lost
    `trg_transaction_no_update` would look entirely fine and no longer be
    append-only, and no test of the migration's *data* would catch it.

### Added

- **In-kind transfers: `pt transfer in` / `pt transfer out`** (ADR 0015).
  Securities crossing the portfolio boundary without being bought or sold — an
  account funded in kind from a previous custodian, a gift of stock, a position
  that predates every available record.
  - **Two numbers travel on the row and must not be conflated.** `--value` is
    the market value on the transfer date: the flow amount (`PORT-GIPS-C02`).
    `--basis` is what the owner paid at the delivering custodian: the tax
    number. Swap them and neither error announces itself — value as basis makes
    every future sale report the gain since the transfer, and basis as value
    makes the period's return wrong by the entire unrealized gain.
  - **The holding period is preserved, not restarted.** `--acquired` becomes
    the lot's open date and holding-period start: a change of custodian is not
    a disposition, and restarting it would convert long-term gains into
    short-term ones on the next sale.
  - **No cash moves.** That is the whole argument for a transaction type rather
    than a back-dated `buy`, which would invent an outflow and then need an
    invented deposit to fund it — an external cash flow, which is how a track
    record gets silently rewritten (ADR 0007).
  - `classify` gains an arm: **`EXTERNAL` and in-kind at *both* levels**, unlike
    `transfer`, which is external at account level and no flow at all at
    portfolio level. ADR 0015's opening-day exception needs the account and the
    return engine, so it is stated in the code and implemented nowhere rather
    than half-implemented (invariant 10).
  - `transfer out` takes `--method` and `--lots`, because which lots leave is
    the same question a sale asks. Found by a test: the seeded account defaults
    to spec-ID and the command had no way to answer it.
  - `BasisSource` refusals with `PT-E-BASIS-SOURCE-INVALID`: a transferred
    lot's basis can never be `derived`; `unavailable` carries no figure and is
    refused one; the three approximate rungs must state their assumption; an
    acquisition date after the transfer is impossible.
  - 50 tests.

### Added

- **The cutover reconstruction, and `pt import reconstruct`** — the opening
  position set derived by rolling a custodian's history back from its dated
  snapshot (ADR 0017). `src/portable_core/services/reconstruction.py`.
  - **Quantities come back exactly** — arithmetic on numbers the custodian
    stated, nothing assumed. Cash rolls back the same way and is reported per
    account, because it is the reconciliation anchor's other half: quantities
    that reconcile and cash that does not is the signature of a sign error or a
    dropped row.
  - **The basis ladder.** One formula serves the top two rungs: the custodian's
    present basis is `surviving_block * unit_cost + cost of surviving
    additions`, so the unit cost is what is left when the additions come out,
    divided by what survives. Untouched since the cutover →
    `reconstructed`; partly consumed → `estimated` under an assumed FIFO
    relief, recorded as an assumption on every lot it touches.
  - **A block with no anchor gets no number.** Today's basis constrains the
    block only through what survives of it, so a block fully consumed — or a
    position liquidated entirely — has no equation to solve under FIFO or any
    other method. Those are `unavailable` with a **null** basis, not a zero and
    not a plausible figure: null says the evidence supports no number, zero
    would claim a basis of nothing. Seeding them at cutover market value is a
    later step and is explicitly not a basis claim (ADR 0017 §2b).
  - **The roll-back is also the completeness check.** A position that rolls back
    below zero proves the history is missing an event — usually a corporate
    action — which is what makes that gap *detectable* rather than something to
    take on trust. Reported by instrument, and distinguished from the sub-share
    residue that comes of a custodian stating transaction and holding
    quantities to different precisions.
  - **Dispositions inside the first year after the cutover are enumerated, not
    counted** (ADR 0017 §4). Beyond a year the character is certain whatever the
    seeded date says; inside it the seeded date is the block's *earliest*
    acquisition and biases toward long-term, which is the wrong direction to be
    relaxed about.
  - `BasisSource` in `domain/enums.py`; `schemas/import-reconstruct-1.0.json`
    published and validated in CI. Every position, its basis source and the
    assumption behind it are carried in `data`, not only in the rendered table:
    a consumer reading `--format json` must be able to see *which* position
    rests on which rung, and the ladder's caveats are an envelope field for the
    same reason the performance disclaimer is one.
  - 34 tests (546 in the suite). Nothing is written to a portfolio.

### Changed

- **`HoldingRecord` and `TransactionRecord` move to
  `portable_core.domain.import_records`** from `portable_core.importers`. ADR
  0018 §5 has the reconstruction, the batch builder and the reconciler
  operating on these with no knowledge of adapters — so a service importing its
  input type from `importers` had the dependency the wrong way round. They
  re-export from `portable_core.importers` unchanged.
- **`portable_core.importers` gains a layering rule**, which it had never had:
  `domain`, `errors`, `decimals` and itself. That gap is why nothing caught the
  reversed dependency until a service tripped over it. An adapter that could
  reach a repository would be able to write, and the point of the three-stage
  pipeline (ADR 0012) is that extraction cannot.

### Added

- **The generic tabular adapter, and `pt import inspect`** — a custodian whose
  exports are plain tabular files is now two TOML mapping files and a fixture,
  with no Python (ADR 0018). `src/portable_core/importers/`.
  - **Two documents are required and nothing else is** — a holdings snapshot
    carrying cash, and a transaction history. Everything beyond them is a
    declared capability. Cash on the snapshot is required rather than optional
    because cash reconciliation is the only check that catches a sign error, a
    dropped row or a double-counted transfer; an account whose custodian
    genuinely reports no cash line is listed in `allow_missing_cash`, so the
    exception is on the record rather than silent.
  - **A capability is declared on validated data, not on a present column.**
    Six named checks — `populated`, `unique`, `matches`,
    `not_before_trade_date`, `history_since`, `activity_covers` — run over the
    parsed rows, and a capability is declared only if its check passes. The
    reference custodian's settlement column, most of whose populated cells
    precede their own trade dates, is the case this exists for.
  - **A withheld capability's data is not read.** Not merely unreported: if
    `settlement_date` fails its check the records carry no settlement dates,
    rather than carrying the dates that failed. A capability that labels data
    which flows through anyway is a comment, not a safeguard.
  - **`activity_map.toml` has no default arm.** An activity string the map does
    not name stops the import and quotes the row. Everything wrong with a
    mapping is refused when the file *loads* — two rules for one string, a fee
    with no `fee_class` (`PORT-GIPS-D01`), a skip with no reason, an unknown
    type or check name — because a map is reviewed once and used for every row
    after.
  - **Sign conventions are declared per activity**, then normalised to one
    canonical convention: positive is cash in, negative is out. Custodians are
    not consistent even with themselves, and taking an amount column at face
    value is how a sign error gets in.
  - **A date pattern must carry a whole date.** `%Y-%m` parses without
    complaint and silently returns the first of the month; a round-trip check
    at load refuses it, along with bogus directives that would otherwise not
    surface until six thousand rows into an import.
  - Spreadsheets are refused by name with the remedy. The runtime dependencies
    stay Typer and Rich; a workbook parser for a file the custodian also emits
    as CSV is a large dependency for no capability (invariant 10).
  - `pt import inspect <directory>` reads both documents and reports the
    capability set with **what each absence costs**, before anything is
    written. `schemas/import-inspect-1.0.json` published and validated in CI.
  - `examples/importers/example-brokerage/` — a worked example, exercised by
    the suite so it cannot drift from the code.
  - 71 tests.

### Fixed

- **The error-code registry had never been tested, and had drifted.**
  `errors/kinds.py` said `tests/unit/test_errors.py` asserted its codes were
  unique; that file did not exist. `PT-E-WITHHOLDING-INVALID`,
  `PT-E-DUPLICATE-REF` and `PT-E-MIGRATION-BLOCKED` were raised in production
  and absent from `ERROR_CODES`, so `pt introspect` under-reported the failures
  a consumer has to handle. All three are published, `tests/unit/
  test_error_codes.py` now asserts every declared constant appears — reading
  the module source, so a constant added to the file and forgotten in the tuple
  fails — and the docstring names a file that exists.

### Schema

- **`schema_version` 1 → 2**, migration `0002_external_ref_unique`.
  `UNIQUE (account_id, external_ref)` on `transaction`, where a reference is
  present, replacing the non-unique lookup index it supersedes.
  - **Scoped per account, not globally.** `pt ca split --ref X` with no
    `--account` writes one ledger row per holding account, all naming one
    corporate action; global uniqueness would refuse a correct command and force
    invented suffixes. Imported rows are unaffected either way — ADR 0012's
    synthesized key already hashes the account in.
  - **Partial.** A row with no reference is the ordinary hand-entered case and
    stays unconstrained.
  - **`source` is deliberately not part of the key.** A hand-entered row and an
    imported row claiming one reference in one account *should* collide: that is
    exactly the case where somebody typed in a transaction the importer is about
    to add again.
  - `TransactionRepository.append` refuses a duplicate with
    `PT-E-DUPLICATE-REF`, naming the transaction that already holds it. Being in
    `append` rather than a service is what covers the corporate-action and
    options commands, which build their rows directly — before this they
    surfaced the raw `IntegrityError` as `PT-E-GENERIC: unexpected error … this
    is a bug`, at exit 1, for what is a user-fixable duplicate.
    `TradingService.check_external_ref` runs the same check earlier so
    `--dry-run` refuses rather than planning a trade that could never commit.
  - `pt import` scans an export's ledger before its first insert, so a payload
    carrying duplicates leaves no half-written file behind.

### Fixed

- **Migrations can state a data precondition**, checked before the transaction
  opens. A migration is pure SQL and cannot branch, so a constraint added over
  existing data either applies or fails with whatever SQLite says — and
  `_apply`'s remedy, *restore the backup*, would reproduce the same data and the
  same failure. For the one operation that can lose a ledger that is not good
  enough. 0002's precondition names every offending `(account, reference,
  transaction ids)` group and a remedy that works.
- **`meta.schema_version` is updated by a migration.** It is a required key that
  `pt validate` checks and `pt export` carries, and nothing had ever updated it —
  which never showed, because 0001 was the only migration there had ever been.
  Every upgraded file would otherwise have reported forever the version it was
  created at.

### Added

- **The import batch format, and `pt import batch`** — the reviewable artifact
  between extracting a custodian's export and committing it (ADR 0012).
  - `schemas/import-batch-1.0.json`, published and validated in CI. The first
    schema here that describes an **input**, so it does not extend the output
    envelope. A test asserts that anything the runtime loader accepts also
    validates against it; the loader checks by hand because `jsonschema` is a
    development dependency and a hand-written check can name the row index, the
    field and the remedy — which a batch under human review needs.
  - **A row states what happened, not what follows from it.** No
    `net_cash_effect` in a batch: the cash effect, the lot relief and the tax
    are derived through the same services a typed command uses, so an
    unclassified fee, a sale with no matching lot, a duplicate reference and an
    unknown instrument are refused for an import exactly as at the keyboard.
    Committed rows carry `source = 'import'`.
  - **Version 1 carries trades, cash and income** — the types with a service
    behind them. Corporate actions and the options lifecycle are refused by
    name rather than half-supported (`CLAUDE.md` invariant 10): they need
    position context a typed command gathers, and an importer deriving basis by
    a second, unreviewed route is the failure that avoids.
  - Rows are appended in **trade-date order** whatever order the file lists
    them in — a sale's relief has to see the purchase earlier in the same batch
    — then the ledger is replayed once (ADR 0016), because a historical batch is
    back-dated relative to whatever the file already holds.
  - **`--dry-run` is the real commit, rolled back.** Checking each row against
    the state before the batch would refuse a batch that commits perfectly well.
    A first attempt did exactly that and was caught in a smoke test; running it
    for real inside `scratch_transaction` is the only dry run that answers the
    question asked.
  - **Source documents are hash-checked** where they sit next to the batch: a
    review approves particular rows against a particular export. A file that
    cannot be found is reported rather than refused, so "verified" and "not
    checked" stay distinct.
  - Rows are reported grouped by `(action, rule)`, because a review is per rule
    and a thousand-row batch read one row at a time is not reviewed.
  - 38 tests.

### Changed

- **`pt import` is a noun with verbs** (ADR 0012). The export round trip moves
  from `pt import <file>` to `pt import portfolio <file>`, alongside the new
  `pt import batch <file>`. A breaking change to a v0.1 command, taken now
  because the surface ADR 0012 designed needs the noun.

### Added

- **`pt reconcile` compares per account, and compares cash** — the last of the
  `v0.2` import prerequisites, and the acceptance criterion every other one
  exists to serve (`docs/broker-import.md` §9).
  - **Per account.** It previously summed every account into one namespace when
    `--account` was omitted, so two accounts holding the same fund reconciled as
    a total: an overstatement in one cancelled an understatement in the other
    and the line balanced. There is a test for exactly that.
  - **Cash.** It previously compared quantities only, which passes on a sign
    error, a dropped row, and a double-counted transfer — every failure that
    leaves the share counts right and the money wrong. A cash line is now
    rendered for every account even when the statement is silent, because
    "they agree" and "nobody checked" must not look the same. A margin loan
    nets against cash, as a statement presents it.
  - **Cash equivalents are cash.** A custodian reports its sweep as a position
    and `portable` holds it as cash, so the statement's sweep lines fold into
    its cash figure before comparison (ADR 0013). This is the one place that
    decision has to be undone, and doing it here keeps it out of the ledger.
  - **Identifiers.** A line may be stated by `symbol`, `cusip` or `isin`. New
    `InstrumentRepository.find` is `resolve` for a caller whose job is to report
    what does not match, so one unknown line no longer abandons the comparison;
    ambiguity still raises, since two instruments answering to one identifier is
    a question only a person can settle.
  - **Refusals** rather than guesses: a line that cannot be attributed to an
    account when several are in scope, a statement naming an account not being
    reconciled, cash stated both by `--cash` and by a file row, and `--as-of`,
    which reconcile cannot honour because there is no as-of position query —
    accepting it would answer a question nobody asked.
  - The comparison moved to `services/reconciliation.py`; the command parses and
    renders. 21 tests.

- **Provenance and withholding on every ledger write** — the first three of the
  `v0.2` import prerequisites (`docs/broker-import.md` §10). No schema change:
  every column involved already existed and had no way to be set.
  - `TradeIntent.source`, `record_cash(source=)` and `record_income(source=)`
    carry where a row came from. The CLI still defaults to `manual`; the point is
    that an importer can now say `import`, so a figure is traceable to the
    document that produced it (`PORT-GIPS-J03`).
  - `--ref` on **all twenty** ledger-writing commands, up from three. Defined
    once as `RefOpt` in `commands/_shared.py`. Each of those commands writes at
    most one row per account per invocation, so a single `--ref` stays
    unambiguous under the `(account_id, external_ref)` uniqueness still to come.
  - `TradingService.record_income` — new, and the home for the withholding
    arithmetic that was previously absent and would otherwise have landed in a
    CLI module. `gross_amount` stays the income the instrument paid and
    `net_cash_effect` is what actually landed, because a report needs both: the
    return is earned on the gross and the cash balance moved by the net.
    Reclaimable and non-reclaimable withholding are stored separately, since
    reclaimable is accrued while non-reclaimable reduces return
    (`PORT-GIPS-A06`) and one combined figure cannot answer both.
  - `--withheld` and `--reclaimable` on `pt income dividend` and `pt income
    coupon`. Deliberately **not** on `pt income roc`: a return of capital is not
    income, so withholding against one is a different event needing its own
    reasoning rather than a shared flag.
  - `PT-E-WITHHOLDING-INVALID` refuses a split that cannot be true — negative
    withholding, withholding above the gross (the likeliest real mistake, passing
    the net as `--amount`), or a reclaimable portion above what was withheld,
    which would accrue a receivable that does not exist.
  - `pt trade show` reports `source`, `taxes_withheld` and
    `withholding_reclaimable`, so the new facts are readable rather than merely
    stored.
  - 25 tests: `tests/unit/test_income_and_provenance.py` and
    `tests/integration/test_provenance_cli.py`.

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

### Fixed

- **A back-dated ledger append left derived state disagreeing with the ledger**
  (ADR 0016, now implemented). `ReplayEngine.apply_transaction` derives against
  state as it currently stands, which equals a replay only when the appended row
  sorts last; a back-dated entry sorted ahead of rows already applied and so
  consumed the wrong lots. Reproduced with a three-transaction portfolio whose
  realized gain — and the tax estimated on it — changed on the next `pt rebuild`.

  Every live append now goes through `ReplayEngine.apply_or_rebuild`, which
  rebuilds in the same database transaction when the row does not sort last, and
  says so: `rebuilt` in the result payload and a warning, because a back-dated
  entry re-deriving the book can change a figure already reported.

- **`pt validate` could not detect that divergence** — the failure `CLAUDE.md`
  invariant 3 exists to make detectable. It called `rebuild()` twice and compared
  the two, but `rebuild()` drops derived state before re-deriving it, so the
  stored state was destroyed before anything could be compared against it. The
  command measured idempotence, and reported a clean file on a book that was
  demonstrably wrong.

  It now digests **stored** state first, replays inside a transaction that is
  always rolled back, and compares — so it detects the divergence, names the
  tables that differ, exits 4 with `PT-E-REPLAY-MISMATCH`, and leaves the file
  byte-identical. Idempotence is kept as a separate check with its own message.
  A command that repaired what it was asked to inspect would report a break and
  then, on a second run, a clean file, with nothing to show which was true.

- **`pt validate` discarded the ledger rows a replay could not apply.**
  `ReplayEngine.rebuild` collects one message per such row and keeps going, so
  that a single pass surfaces every problem; its docstring has always said
  `pt validate` is what turns those into a non-zero exit, and `validate` threw
  them away. A row that produces no derived state is a real invariant break —
  derived state is then not a function of the *whole* ledger — and cash
  conservation does not reliably catch it, because `apply_transaction` moves
  cash before it does the position work.

  `validate` now reports each as a problem under `unreplayable` and exits 4.
  `pt rebuild` still renders the same facts as warnings and still succeeds: it
  rebuilds and reports, `validate` judges. `ReplayResult.warnings` documents
  both audiences at the point it is defined.

- **The derived-state digest was blind to relationships.** It excluded every
  column whose name ended in `_id`, which dropped the surrogate keys a rebuild
  legitimately reassigns and also `account_id` and `instrument_id` — so a lot's
  quantity and basis were hashed and *which instrument it belonged to* was not.
  Foreign keys are now resolved to their natural keys (account name, instrument
  symbol) and hashed; surrogate keys stay excluded; `txn_id` is hashed as it
  stands, since the ledger is never rebuilt.

### Added

- `Repositories.transactions.count_after` — how many ledger rows sort after a
  given `(trade_date, seq)`. Zero is the only case in which incremental
  derivation equals a replay.
- `persistence.connection.scratch_transaction` — a transaction that always rolls
  back, for a command that must rewrite derived state to answer a question about
  it and leave the file untouched.
- `services.replay.derived_state_digests` — the digest per derived table, so a
  mismatch can name what differs rather than reporting "something".
- `services.trading.CommitResult` — `TradingService.commit` now returns the
  stored transaction *and* whether committing it rebuilt.
- `tests/unit/test_replay_ordering.py` and
  `tests/integration/test_validate_replay.py` — 18 tests, each of which fails
  without the corresponding part of the fix, including the ADR's reproduction
  asserted as the realized gain a person would read, and the split between
  `pt rebuild` reporting an unreplayable row and `pt validate` failing on it.

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
