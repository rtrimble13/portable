# Releasing and upgrading

`portable` is versioned `vX.Y.Z` and installed from a git checkout. Nothing is
published to PyPI. A release is three things:
- a number that says what is running;
- a tag that names the commit it came from;
- a GitHub release whose notes say what to run after the pull, and what
  changed.

**Every version that reaches main is a release**
([ADR 0049](adr/0049-every-version-on-main-is-a-release.md), as in
rtrimble13/fafnir, dorq and alpha-tango). A pull request that changes what
ships raises the version, and merging it publishes the release. Nobody tags by
hand.

## How a release happens

```bash
make version                       # this branch, main, and the last release
make bump BUMP=minor DRY_RUN=1     # what would happen
make bump BUMP=minor               # set this branch's version; then commit it
```

1. **In the pull request**, `scripts/release.py` (`make bump`) sets the version
   the merge will release. It takes `patch`, `minor`, `major` or an explicit
   `X.Y.Z`, counted from the version on `main`. Running it again on the same
   branch gives the same number:

   | Command | 0.2.0 on main becomes |
   |---|---|
   | `make bump BUMP=patch` | 0.2.1 |
   | `make bump BUMP=minor` | 0.3.0 |
   | `make bump BUMP=major` | 1.0.0 |
   | `make bump BUMP=0.4.0` | 0.4.0 |

   - **It changes two files.** It rewrites the one line that carries the
     number, `__version__` in `src/portable_core/__init__.py`. It also turns
     `CHANGELOG.md`'s `## [Unreleased]` section into `## [X.Y.Z] - YYYY-MM-DD`,
     with a new, empty `## [Unreleased]` above it.
   - **It checks both** against the gate below and the version tests. If
     either fails, it puts both files back.
   - **It refuses to run on `main`, and it does not commit.** Commit both files
     with the change that needs them, then run `make install`, so pip's record
     of the version catches up.
2. **CI's `version` job** checks every pull request (`scripts/check_version.py`,
   part of `check`; `make version-check` runs it locally, and `make check`
   includes it):
   - **The version is `X.Y.Z` and never goes down.**
   - **A change to what ships raises it above the base branch's.** What ships
     is `src/`, `schemas/`, `cpp/`, `pyproject.toml`, and `scripts/xlsx_to_csv.py`,
     a step of a broker import. A pull request that changes only docs, tests,
     CI, the sample or the repository's own scripts keeps the version, and its
     merge releases nothing.
   - **An added schema migration** raises the minor version at least (below).
   - **A new major version of an output schema** raises the minor version at
     least while the version is `0.y.z`, and the major from `1.0.0` (below).
   - **A raised version has its own section in `CHANGELOG.md`**, dated, first
     after `## [Unreleased]`, and not empty: it is the text of the release.
3. **The merge publishes it.** On each push to `main`,
   `.github/workflows/release.yml` reads `__version__`. If GitHub has no release
   of it, the workflow:
   - **installs the commit** as `make install` does, with the native extension;
   - **checks that the number matches everywhere**: the source, the
     distribution metadata, the extension, and all five CLIs (`pt`, `pert`,
     `po`, `risky`, `portable`);
   - **creates the tag `vX.Y.Z`** at that commit, with the GitHub release.

   The notes open with what to run after the pull ([Upgrading](#upgrading)),
   then give the version's `CHANGELOG.md` section, then GitHub's list of the
   merged pull requests.
   - **Already released:** a push that leaves the version released finds
     nothing to do, in seconds.
   - **After a failure:** run it again from the Actions tab (`release`, "Run
     workflow", on `main`).

**Never move or delete a pushed tag.** A checkout that already has it will not
pick up a moved one. Fix forward with the next version.

## Upgrading

```bash
git pull
pt --version
```

That is the whole upgrade for a release that changes only Python. A release's
notes say when it needs more:

- **Dependencies or the native extension changed** (`pyproject.toml`, the
  requirements, or `cpp/`): run `make install` as well (or
  `scripts/bootstrap.ps1` on Windows). An editable install picks up Python
  from the checkout. A changed requirement, or C++ to compile, needs pip to
  run.
- **A schema migration was added**: run `pt --port FILE migrate` for every
  `.port` file you keep. It takes a backup first. Until a file is migrated,
  `pt` refuses it (`PT-E-SCHEMA-TOO-OLD`, `docs/port-format.md`).
- **An output schema has a new major version** (`holdings-2.0`): anything
  that reads `--format json` of that command, and pins the old schema, has to
  move.

## Which number to trust

`pt --version` reads the source constant, so it is right the moment the pull
lands. Two other numbers can disagree with it, and each answers a different
question:

| What you ran | What it tells you |
|---|---|
| `pt --version` | what the **code on disk** says. Correct after a bare `git pull`. |
| `git describe --tags` | which **commit** is checked out: `v0.2.0`, or `v0.2.0-3-gabc1234` if it is three commits past the tag, which is not a release |
| `pip show portable` | what pip recorded at the **last install**. Lags a pull until `make install`. |

The `v` lives on the tag only. `portable_core.__version__` is `0.2.0`, and the
tag naming that commit is `v0.2.0`.

A `.port` file records the version that created it (`meta.portable_version`)
and that applied each migration (`schema_migration.applied_by`). The committed
`examples/sample.port` says `0.0.0+sample`, a version that is not a release:
the generator pins it, as it pins its clock, so a release does not rewrite the
file (ADR 0049 §5).

## Where the number lives

There is one place: `__version__` in `src/portable_core/__init__.py`, a plain
string literal.
- **The other packages re-export it**: `portable_pt`, `portable_pert`,
  `portable_po` and `portable_risky`.
- **`pyproject.toml` reads it** through `[tool.scikit-build.metadata.version]`.
- **`cpp/CMakeLists.txt` reads the same line** and defines `PORTABLE_VERSION`
  for the extension.

`tests/unit/test_version.py` asserts each link, including that no second
`__version__` literal appears in `src/` and no version literal in `cpp/`.

## Choosing the number

`0.y.z` until the owner declares `1.0.0`. Judge the bump by what a reader of
`CHANGELOG.md` would call it:

- **patch**: a fix that changes no command, flag, output schema or file
  format, and no number a report gives.
- **minor**: a new command, flag or report; a schema migration; a new major
  version of an output schema; a fix that moves a reported number; and, while
  `0.y.z`, anything that breaks.
- **major**, from `1.0.0`: an existing invocation, output field or stored
  column means something different.

Two cases are mechanical, and the gate holds them:
- **A migration is not a patch.** Each is a `pt migrate` of every file after
  the pull, so the number has to say one is there. A migration that only fixes
  (an index, a constraint) and changes nothing a reader sees can say so with a
  line `-- release: patch` in its `.sql`.
- **A new major output schema is not a patch.** A consumer pinned to the old
  one breaks.

The rest is judgement, made in review. A fix that changes a figure the owner
may have recorded is at least a minor, and its `CHANGELOG.md` entry says which
figures moved.

## Recommended repository settings

These are set in GitHub, not in files:

- **Branch protection on `main`:** require a pull request, and the status
  check `check` (CI's one green light, which includes the `version` gate), so
  a change to what ships cannot merge without its version. Disallow force
  pushes, so a published tag's commit stays on `main`.
- **Actions, workflow permissions:** "Read repository contents" is enough. The
  release job asks for `contents: write` itself, as fafnir's, dorq's and
  alpha-tango's do.

## Fixing a bad release

Releases are not edited.
1. **Fix forward.** Open a pull request with the fix and a new version, and
   merge it.
2. **Mark the bad release on GitHub.** Edit its notes to point to the fixed
   version. If it is harmful, tick "Set as a pre-release", so it no longer
   shows as the latest.
3. **Say what was wrong** in the new version's `CHANGELOG.md` section, under
   **Fixed**.
