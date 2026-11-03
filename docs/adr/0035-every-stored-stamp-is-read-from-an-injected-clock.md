# ADR 0035 — Every stored stamp is read from an injected clock

- **Status:** Accepted — implemented in `portable_core/clock.py`, `Repositories(clock=)`, `migrations.initialise(clock=)` and `migrate(clock=)`, and `scripts/build_sample_portfolio.py`. Decision 6 was corrected after the first CI run (see there)
- **Date:** 2026-11-02
- **Milestone:** v0.2
- **Governs:** `CLAUDE.md` invariant 6; `PORT-GIPS-J06`
- **Relates to:** [ADR 0010](0010-derived-state-and-replay.md) (replay order is not `created_at`), [ADR 0026](0026-dry-run-rehearses-the-write.md) (a dry run is the real write)

## Context

`CLAUDE.md` and the fixture generator both said that two runs of `make fixtures`
produce identical bytes. They did not. The generator pinned `created_at` on
ledger rows only. Every other write stamped `datetime.now()`: the repositories'
private `_now()` for the policy, account, instrument, tax-rate, corporate-action
and price rows, and the migration runner's own `_now()` for each
`schema_migration.applied_at`. The committed file carried
`2026-10-20T22:07:53Z` in nine columns, and every `make fixtures` produced a
diff.

The wall clock was also read in eight other modules. Three services
(`trading`, `regrouping`, `labels`) and three `pt` command modules (`trade`,
`options`, `corporate_actions`) read it to stamp the ledger rows they build,
although `cli/context.py` says the clock is not to be read deep in a service.
`pt init` read it for the portfolio's `meta` stamps, and `pt price set` for a
hand-set price's `as_of`.

`pert` will test against generated fixtures, and `PORT-GIPS-J06` records
determinism as the evidence `PORT-GIPS-J01`'s content hash depends on.

## Decision

1. **One clock type.** `portable_core.clock.Clock` has `now()` and `stamp()`.
   A stamp is UTC, to the second, with a literal `Z`. `SystemClock` reads the
   wall clock. `FixedClock(moment)` always reads one moment and refuses a naive
   datetime, because converting a naive one would assume the machine's time
   zone. The module is a stdlib-only leaf, like `decimals`.
2. **Injected, never global.** `Repositories(con, clock=SYSTEM_CLOCK)` hands
   its clock to every repository. An individual repository has no default, so
   one added to the bundle without the clock fails to construct instead of
   stamping from the wall clock. `migrations.initialise` and `migrate` take
   `clock=` for `applied_at`. There is no module-level setter, no contextvar and
   no environment variable. The clock a write uses is the one at the call that
   built the bundle.
3. **Every write-time stamp reads it.** That covers every `created_at` and
   `updated_at` the repositories write, every `applied_at`, the ledger rows that
   services and `pt` commands build (through `repos.clock`), `pt init`'s
   `meta.created_at` and `updated_at`, and `pt price set`'s `as_of`, which
   `ValuationRepository.superseded_prices` already treats as a write-time stamp.
4. **The generator passes `FixedClock(2026-08-27T00:00:00Z)`** to both the
   runner and the bundle, and its ledger rows no longer stamp themselves.
5. **What stays on the wall clock, and why.** The `--as-of` default is a
   business date, defaulted once and explicitly (invariant 6). The envelope's
   `generated_at` is output, and is already injectable. A migration backup's
   file name must differ from the previous backup's, and a fixed clock would
   overwrite it.
6. **The generator zeroes freed space, and only the library's version field
   may differ.** With the stamps pinned, a build through Ubuntu's SQLite 3.45.1
   and one through a SQLite 3.53.1 held the same schema and the same rows, but
   24 of 106 pages differed. This decision first put that down to the library
   version, and skipped the byte comparison on any other version. The first CI
   run showed otherwise. On Windows the library reported 3.45.1, so the
   comparison ran, and the same 24 pages differed. The cause is
   `SQLITE_SECURE_DELETE`. Ubuntu compiles SQLite to zero what a delete frees,
   other builds leave the old bytes in place, and the migrations that rebuild
   a table free pages. With `PRAGMA secure_delete = ON` set by the generator,
   the 3.45.1 and 3.53.1 builds differ only in header bytes 96–99. That field
   holds the version of the library that last wrote the file. The committed
   file was already written with freed space zeroed, so its bytes do not
   change. `tests/integration/test_sample_fixture.py` checks four things:
   - every stamp in a fresh build is the generator's. This check fails on the
     wall clock however fast the machine is. Two builds take about 0.4 s and
     can land in the same second;
   - two builds, each in its own process, are byte-identical;
   - the committed file holds the same schema and rows as a fresh build. A
     failure here names the rows that drifted;
   - the committed file equals a fresh build byte for byte, with bytes 96–99
     set aside, on every library.

## Consequences

- `make fixtures` twice gives the same bytes. A diff in `examples/sample.port`
  now means the code changed. The one exception is a regeneration on another
  SQLite library, which rewrites the four bytes that name it and nothing else.
  The tests pass either way.
- `PORT-GIPS-J01` hashes the report envelope, not the `.port` file, so the
  file's bytes never reach it. Stamps could: `pt trade show` prints
  `created_at`. A fixture can now pin them.
- Only the generator zeroes freed space. A real portfolio keeps whatever its
  SQLite was compiled with: nothing compares its bytes, and secure delete
  adds I/O to every delete.
- A test, or a `pert` fixture, pins every stamp by building the bundle with a
  `FixedClock`. Nothing has to be monkeypatched.
- Nothing orders, replays or values by a stamp (ADR 0010), the derived and
  valued tables carry none, and a dry run reads the clock exactly as the real
  write does (ADR 0026). No figure changes.
- The CLI cannot pin its clock, and gains no flag or variable to do so: a real
  command stamps real time. Golden-file CLI tests that need it are a separate
  decision.

## Alternatives considered

- **Monkeypatch `_now` in the script.** This hides the dependence, it would
  have missed the migration runner's separate `_now`, and every pinning caller
  would need to know each module's private helper. Rejected.
- **A settable module-level clock, or a contextvar.** This is hidden global
  state: a write's stamp would depend on what an earlier caller set, and it
  would leak between tests. Rejected.
- **Stop storing stamps outside the ledger.** This needs a schema change, and
  it drops the record of when a row was entered. Rejected.
- **Skip the byte comparison on another library version.** This decision's
  first text did that, on the belief that page layout belongs to the library.
  CI then ran Ubuntu with SQLite 3.46.1 and Windows with bundled libraries,
  and only the Windows 3.45.1 job compared bytes at all. That is how the real
  cause was found. Rejected: on most platforms nothing would have held the
  bytes.
- **Zero the version field in the committed file.** Rewriting the header by
  hand edits a database behind SQLite's back, and SQLite restores the field on
  the next write anyway. The test sets those four bytes aside instead.
- **Commit a text dump instead of the binary.** That would change what every
  test and `examples/walkthrough.md` open, which is a larger change than this
  fix. Rejected.
