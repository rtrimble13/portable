# ADR 0049 — Every version on main is a release: one number in source, raised by the pull request, published by the merge

- **Status:** Accepted (owner, 2026-11-25: "implement versioning and release automation that is identical to the model used in `rtrimble13/dorq`, `rtrimble13/fafnir` and `rtrimble13/alpha-tango`"). **Implemented**: `src/portable_core/__init__.py`, `scripts/check_version.py`, `scripts/release.py`, `.github/workflows/release.yml`, the `version` job in `.github/workflows/ci.yml`, `make version` / `make bump`, `tests/unit/test_version.py`, `tests/unit/test_check_version.py`, `tests/unit/test_release_script.py`
- **Date:** 2026-11-25. Confirmed by the owner the same day: the first release
  is 0.2.0, the milestone names stay, and CI's `check` is to be a required
  status check on `main` (docs/releasing.md, "Recommended repository settings")
- **Governs:** `CLAUDE.md` invariant 6 (determinism) for the sample's stamp; "Schema changes require a migration … and a note in `CHANGELOG.md`"
- **Depends on:** [ADR 0035](0035-every-stored-stamp-is-read-from-an-injected-clock.md) (the sample's stamps are pinned), [ADR 0008](0008-cpp-integration-and-fallback.md) (the extension and its Python reference)
- **Related:** [docs/releasing.md](../releasing.md); rtrimble13/fafnir's ADR 0011 and ADR 0022, rtrimble13/dorq's ADR 0008, and rtrimble13/alpha-tango's `docs/versioning-and-releases.md`, the model this adopts

## Context

### Nothing was released

`portable` had no tags and no releases. `CHANGELOG.md` held a `[0.1.0]`
section headed "unreleased" and two thousand lines under `[Unreleased]`: the
whole of milestone v0.2 so far. The owner runs it from a `git clone`, upgraded
by `git pull` into an editable install. There was no answer to "which code am
I running, and what changed since I last pulled?" beyond `git log`. In
particular, nothing said when a pull needs `pt migrate` on every `.port` file,
or a reinstall.

### The number was written in five places

| Where | Value |
|---|---|
| `pyproject.toml` | `0.1.0` |
| `portable_core.__version__` | `0.1.0` |
| `portable_pt.__version__` | `0.1.0` |
| `portable_pert.__version__` | `0.1.0` |
| the C++ extension: `cpp/CMakeLists.txt`, `probe.cpp`, `bindings.cpp`, its Catch2 test, and the Python reference's `module_version` | `0.1.0` |

They agreed by coincidence. `po` and `risky` printed their help to
`--version` and exited 1.

### The model the owner's other repositories use

fafnir (Python), dorq (C++) and alpha-tango (skills) release the same way. The
rule is simple: **a version on main with no release is released, at the commit
just pushed.**
- A pull request that changes what ships must raise the version above its
  base branch's, and a gate on the pull request holds it to that. So the
  merge is the release.
- Nobody tags by hand, and no credentials outside CI are needed.
- Each repository adds a few mechanical rules for its own cases:
  - fafnir: a migration is a minor release at least;
  - dorq: a change to its output is a minor release at least;
  - alpha-tango: each version has a changelog section, which becomes the
    release's text.

## Decision

### 1. One number, in source

- **`__version__` in `src/portable_core/__init__.py` is the only literal**, a
  plain string a build can read without importing the package.
- **`portable_pt`, `portable_pert`, `portable_po` and `portable_risky`
  re-export it.**
- **Every CLI answers `--version` with it.** That includes the two stubs, for
  which it is the one question they can answer.
- **`pyproject.toml` declares `dynamic = ["version"]`** and reads the
  constant through scikit-build-core's regex provider.
- **`cpp/CMakeLists.txt` reads the same line**, sets `project(VERSION)` from
  it, and defines `PORTABLE_VERSION` for the C++. The Python reference reads
  the constant.

**Why a source constant, not a version derived from the tag** (fafnir's ADR
0011, measured there): a derived version is computed when pip runs, and this
installation is upgraded when git runs. After a `git pull`, a derived
`pt --version` would report the release pip last saw, which is a plausible,
wrong number. A source constant is right the moment the pull lands.

### 2. The pull request chooses the number, and a gate holds it there

`scripts/check_version.py`, the `version` job in CI on every pull request,
part of `check`:
1. **The version is `X.Y.Z` and never goes down.**
2. **A change to what ships raises it above the base's.** What ships is
   `src/`, `schemas/`, `cpp/`, `pyproject.toml` and `scripts/xlsx_to_csv.py`
   (a step of the owner's import). Docs, tests, CI, the sample and the
   repository's own scripts do not ship. A pull request touching only those
   keeps the version, and its merge releases nothing.
3. **An added schema migration is a minor release at least**, since each one
   means a `pt migrate` of every `.port` file after the pull. A migration that
   only fixes, and changes nothing a reader sees, can carry a line
   `-- release: patch` (fafnir's rule).
4. **A new major version of a published output schema** (`holdings-2.0`
   beside `holdings-1.0`) breaks a consumer pinned to the old one. So it is a
   minor release at least while the version is `0.y.z`, and a major one from
   `1.0.0` (dorq's rule for its output).
5. **A version that rises has its own `CHANGELOG.md` section**,
   `## [X.Y.Z] - YYYY-MM-DD`. It comes first after `## [Unreleased]`, is not
   empty, and is the text of the release (alpha-tango's rule).
   `CHANGELOG.md` is already where this repository records what a reader acts
   on, so it becomes the release notes rather than a second record.

### 3. `scripts/release.py` sets the version on a branch

`make bump BUMP=patch|minor|major|X.Y.Z` runs `scripts/release.py`.
- **It counts from the version on `origin/main`**, so running it again on a
  branch gives the same number.
- **It rewrites the constant and turns `[Unreleased]` into the release's
  dated section.** A section this branch set before is renamed instead.
- **It checks the result** against the gate and the version tests, and puts
  both files back if either fails.
- **It refuses to run on `main`, and it never commits, tags or pushes.**
- **`make version` reports** this branch's version, main's, the last
  release, and the migrations since it.

**It is Python, not fafnir's bash.** The owner works on Windows (ADR 0042),
and the bump is something the owner runs.

### 4. The merge publishes

`.github/workflows/release.yml`, on each push to `main`:
1. **It reads the constant** (`release.py plan`). A version that already has
   a GitHub release finishes in seconds. A tag already on another commit
   stops the run.
2. **It installs the commit as `make install` does**, with the native
   extension.
3. **It checks that the version matches everywhere**: the source, the
   distribution metadata, the extension, and all five CLIs. Then it runs the
   version tests.
4. **It creates the tag `vX.Y.Z` and the GitHub release together**:
   `gh release create --target <commit>`. This is a lightweight tag at the
   merge commit, as in all three repositories.
5. **The notes open with what the owner runs after `git pull`**, then give
   the version's `CHANGELOG.md` section, then GitHub's list of merged pull
   requests:
   - `make install` when `pyproject.toml`, the requirements or `cpp/`
     changed;
   - `pt --port FILE migrate`, naming the migrations;
   - any new major output schema.

   A section too long for a GitHub release (over 125,000 characters) is
   linked instead of carried.

**Never move or delete a pushed tag. Fix forward with the next version.**

### 5. The sample stamps a pinned version

`examples/sample.port` recorded the running version in
`meta.portable_version` and in each migration's `applied_by`. Releasing on
every merge would rewrite that committed binary on every release, and two
open pull requests would conflict on a file neither otherwise touches.
- **The generator now stamps `0.0.0+sample`**, as it pins its clock
  (ADR 0035). That is a version that is not a release, so nobody reads it as
  one.
- **`migrations.initialise` takes `applied_by`** for this, defaulting to the
  running release.
- **Nothing computes from either stamp.**

### 6. Choosing the number

`0.y.z` until the owner declares `1.0.0`. Judge the bump by what a reader of
`CHANGELOG.md` would call it:
- **patch**: a fix that changes no command, flag, output schema or file
  format.
- **minor**: a new command, flag or report. Also a migration, or a new major
  version of an output schema. While `0.y.z`, it is also anything that
  breaks.
- **major**, from `1.0.0`: an existing invocation, output field or stored
  column changes meaning.

**A fix that moves a reported number is at least a minor**, and says so
under "Fixed". The holdings fix in this release is an example: totals and
weights the owner may have recorded are different now.

**Why not derive the bump from the Conventional Commits this repository
already writes** (semantic-release, release-please):
- **A commit's type does not see what decides the number here**: a
  migration, a new major output schema, a fix that moves a reported figure.
  A `fix:` that restates every holdings total is not a patch to the person
  who reads it.
- **The owner's other repositories choose the number in review**, and this
  ADR adopts their model. The rules a machine can apply (§2) are applied
  mechanically, and the judgement is left where a reviewer sees it.

## Consequences

- **Releasing is merging.** A pull request that changes what ships carries
  its version and its `CHANGELOG.md` section, and merging it publishes the
  release.
- **`portable` releases often.** Each such pull request is a release.
  - **Milestones and versions no longer line up.** v0.2 names a stage of the
    roadmap; 0.2.0 is the first release. The roadmap keeps its milestone
    names.
- **The first release is 0.2.0**, made by the merge of the pull request that
  carries this ADR.
  - **`[0.1.0]` was never published.** Its section stays, marked so. 0.2.0
    contains it and everything under `[Unreleased]` until now, with this
    pull request's own changes: the holdings fix (`holdings-2.0`, rule 4's
    first case) and this machinery.
- **`make check` runs the gate against `origin/main`**, as CI does. A branch
  that changes what ships fails it until `make bump`.
- **The bump changes the constant that pip read**, so the editable install's
  metadata lags until `make install`.
  - `test_the_distribution_metadata_matches_the_source` is marked
    `needs_fresh_install` for this.
  - `release.py` deselects it.
  - The release workflow, which installs first, does not.
- **A version is always three numbers.** `--version`, the JSON envelope's
  `portable_version` and a migration's `applied_by` all carry it. A file
  stamped `0.0.0+sample` is the generator's.

## Alternatives considered

- **Tag by hand, as fafnir did under its ADR 0011.** That releases when
  somebody remembers, from a machine that can push tags, and between
  releases merged work waits. fafnir and dorq both moved off it. Rejected.
- **A version derived from the tag** (`setuptools-scm`, or scikit-build-core's
  equivalent). It is stale after a `git pull` (§1). Rejected.
- **Conventional-commit automation.** See §6. Rejected.
- **No changelog gate, with GitHub's generated notes alone** (fafnir, dorq).
  This repository already keeps the record a reader acts on in
  `CHANGELOG.md`, so a release without it would publish a list of pull
  request titles beside a better text left unpublished. Rejected, in favour
  of alpha-tango's rule.
- **`make fixtures` on every bump**, keeping the sample's stamp current. Every
  release would rewrite a 1.6 MB binary, and concurrent pull requests would
  conflict on it. Rejected (§5).
