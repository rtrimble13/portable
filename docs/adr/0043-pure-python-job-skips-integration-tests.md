# ADR 0043 — The pure-Python CI job skips the integration tests while nothing computes natively

- **Status:** Accepted (owner, 2026-11-16)
- **Date:** 2026-11-16
- **Milestone:** v0.2
- **Amends:** [ADR 0008](0008-cpp-integration-and-fallback.md), Consequences, first bullet: the
  job with the extension **off** stays, and still asserts the extension is absent, but runs the
  unit and property tests rather than the whole suite. The rest of ADR 0008 stands
- **Governs:** the `no-native` job in `.github/workflows/ci.yml`;
  `tests/unit/test_layering.py`

## Context

ADR 0008 runs one CI job with `PORTABLE_BUILD_NATIVE=OFF`, so the pure-Python
reference cannot rot unnoticed. That job ran the whole suite: 11m49s in the
last run on `main`, of which nearly all was the integration tests starting `pt`
once per CLI call.

The extension computes nothing that production code uses. Outside
`portable_core/native/`, the only import from it is `pt info` asking which
implementation is live, to report it (`PORT-GIPS-J03`). So with the extension
built or not, every line an integration test runs is the same Python, and the
test jobs already run every integration test with it built. Outside
`test_native_fallback.py`, the pure-Python job's integration run checked
nothing those jobs did not.

## Decision

The `no-native` job keeps its install with the extension off and its check
that the extension is absent, then runs `pytest -n auto -m "not integration"`:
every unit and property test, including the fallback, console-script and
schema tests.

The premise is tested, not assumed. `tests/unit/test_layering.py` fails when
code under `src/` outside `portable_core/native/` imports anything from it
other than `HAVE_NATIVE`, `implementation()` and `build_info()`, or imports
the module itself or `portable_native`. A companion test holds the rule to
each of those shapes, so it is known to fire.

## Consequences

- The job's tests take seconds instead of minutes. It runs beside the test
  matrix, so this saves runner time, not wall-clock time.
- The integration tests run only with the extension built. While the guard
  holds, that is the same code.
- **The first production path that dispatches through the extension fails
  the guard.** That is the point at which the two builds become different
  products. Its author restores `pytest -q -n auto` in the job, supersedes this
  ADR, and removes the guard. `docs/architecture.md` §10, which anyone writing
  C++ reads first, says so.
- Reverting is that same one-line change.

## Alternatives considered

- **Keep the full suite.** With the suite parallel, it costs a few runner
  minutes per push and no wall-clock time. Declined because, while the guard
  holds, it repeats work and checks nothing new.
- **Run no tests, only the install and the absence check.** This would lose
  the fallback tests, which are the job's reason to exist.
- **Make one leg of the test matrix the pure-Python one.** That leg would then
  run neither the differential tests nor, on its Python, the extension, and
  each job would be doing two things.
