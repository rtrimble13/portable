# ADR 0042 — CI runs Linux only, for now

- **Status:** Accepted (owner, 2026-11-16)
- **Date:** 2026-11-16
- **Milestone:** v0.2
- **Amends:** [ADR 0008](0008-cpp-integration-and-fallback.md), Consequences, first bullet: CI no longer
  matrices Windows. The rest of that ADR stands, the pure-Python job included
- **Governs:** `.github/workflows/ci.yml`; `CLAUDE.md` "Running things"

## Context

One run of the suite takes more than ten minutes. CI ran it six times per push,
on Linux and Windows × Python 3.11, 3.12 and 3.13, and built and ran the Catch2
suite on both platforms as well. Windows runners are the slowest to provision
and to run this suite, which starts a fresh interpreter for every CLI call the
integration tests make. The owner asked for CI on Linux only, with the other
platforms not needed at this time.

## Decision

Every CI job runs on `ubuntu-latest`. The Python matrix (3.11, 3.12, 3.13)
stays, as do the pure-Python job, the Catch2 job and the single `check` gate.

`tests/unit/test_ci_workflow.py` keeps its rule that a Windows-capable step runs
one command, and the test that proves the rule bites. It stops requiring a
Windows job to exist, since there is none for it to check.

## Consequences

CI no longer catches the Windows failures it was there to catch. Each of these
has bitten, or is guarded by code that exists because it would:

- **A step whose status is only its last command's.** PowerShell does this; the
  rule above is dormant and applies again as soon as a Windows runner returns.
- **stdout encoded as cp1252 when piped.** `configure_streams()` fixes it, and
  `test_console_encoding_cli.py` still runs, but Linux cannot reproduce the
  Windows default the fix exists for.
- **`Path.home()` reading `USERPROFILE`, not `HOME`.**
- **MSVC reporting `__cplusplus` as `199711L`** without `/Zc:__cplusplus`
  (ADR 0008).
- **A different SQLite library**, which is how the header version bytes were
  found (ADR 0035).
- **POSIX-only paths, shell invocations and line endings** in general.

The owner works on Windows, so the owner's machine is now the only Windows test
bed: `make check` there is the substitute before relying on a change.
`CLAUDE.md`'s rule against POSIX-only assumptions stands; nothing in CI enforces
it any longer.

Reverting is one line per job: give `test` and `cpp` an `os` matrix of
`[ubuntu-latest, windows-latest]` and `runs-on: ${{ matrix.os }}` again. Their
steps are still one command each, so nothing else changes.

## Alternatives considered

- **Windows on a schedule, or on `workflow_dispatch` only.** Keeps a Windows
  signal for a fraction of the cost: weekly, or before a release. Not taken
  because the owner asked for Linux only; it is the cheapest way back.
- **Windows on one Python only.** One Windows job instead of three. Still adds
  the slowest job to every push.
- **Keep both platforms.** The status quo, declined on runtime.
