# ADR 0008 — C++ integration: optional extension, mandatory Python reference

- **Status:** Accepted
- **Date:** 2026-08-27
- **Milestone:** v0.1

## Context

Bootstrap §8.4 asks for the C++ toolchain wired end to end and proven with one
trivial `portable_native` module, importable after `pip install -e .`, tested
from both pytest and Catch2, building in CI on Linux and Windows — and **no
production C++ in this engagement**. §8.1 fixes the toolchain as CMake +
pybind11 + Catch2 via `scikit-build-core`, matching
[`po`](https://github.com/rtrimble13/po) so that integrating `portopt` later is a
merge rather than a rewrite.

`po`'s conventions, read at commit-time of this ADR: CMake ≥ 3.20, C++17,
`CMAKE_POSITION_INDEPENDENT_CODE ON`, all dependencies via `FetchContent` with
`GIT_SHALLOW TRUE` (Eigen 3.4.0, nlohmann/json 3.11.3, toml++ 3.4.0, spdlog
1.14.1, CLI11 2.4.2, pybind11 2.12.0, Catch2), and `PORTOPT_BUILD_*` option
flags gating each component.

`CLAUDE.md` adds the hard rule: **every C++ path keeps a pure-Python reference
implementation and a differential test comparing them. No exceptions.**

## Decision

### Build

`scikit-build-core` is the build backend, as fixed. The extension build is gated
on **`PORTABLE_BUILD_NATIVE`** (default `ON`), and `cpp/CMakeLists.txt` mirrors
`po`'s option-flag style with a `PORTABLE_` prefix and the same pybind11 and
Catch2 tags, so the two dependency sets reconcile rather than collide.

**`cpp/CMakeLists.txt` reads `PORTABLE_BUILD_NATIVE` from the environment
itself, and that is load-bearing.** scikit-build-core reads its *own*
configuration — `[tool.scikit-build.cmake.define]` in `pyproject.toml`, or
`SKBUILD_CMAKE_DEFINE` — and does **not** pass an arbitrary environment variable
through to CMake. Without the explicit `if(DEFINED ENV{...})` block,
`PORTABLE_BUILD_NATIVE=OFF pip install .` silently builds the extension anyway,
which makes every document describing the switch wrong. `-D` and
`SKBUILD_CMAKE_DEFINE` continue to work.

Setting `PORTABLE_BUILD_NATIVE=OFF` produces a **complete, correct, pure-Python
install**. This is not a degraded mode; it is the reference implementation, and
it is what the differential tests compare against. It exists so that a user
without a compiler — or an air-gapped CI runner that cannot `FetchContent`
pybind11 — still gets every number `portable` computes, at the cost of speed
only.

### The fallback contract

Every native function has a Python twin, and the dispatch is one module:

```python
# src/portable_core/native/__init__.py
HAVE_NATIVE: bool          # did the extension import?
def implementation() -> Literal["native", "python"]: ...
```

Rules, enforced by tests:

1. **The Python reference is written first and is normative.** If the two
   disagree, the native one is wrong until proven otherwise.
2. **A differential test compares them** over generated inputs (hypothesis) for
   every exported function, and is **skipped, loudly, not silently** when the
   extension is absent — a skipped differential test prints which functions went
   unverified.
3. **`pt info --format json` reports which implementation is live**, so a number
   can always be traced to the code that produced it (`PORT-GIPS-J03`).
4. **No native-only functionality, ever.** The extension may only make existing
   Python faster. A capability that exists only in C++ would make
   `PORTABLE_BUILD_NATIVE=OFF` a silently different product.
5. **Money stays in Python.** `Decimal` does not cross the pybind11 boundary in
   v0.1. When a hot path in a money computation is eventually moved (backlog:
   lot-relief matching, valuation roll-forward), it moves as integer or string
   arithmetic with an explicit, tested conversion — never as `double`.
   `CLAUDE.md` invariant 1 does not stop at the language boundary.

### What ships in v0.1

`portable_native.probe()` returning a struct with the build's C++ standard,
compiler id, and a checksum of a known input — enough to prove the toolchain end
to end and to be a template. Its Python twin lives in
`portable_core/native/_reference.py`. Catch2 tests the C++ side; pytest tests the
binding, the twin, and their agreement.

## Consequences

- CI matrices Linux and Windows × Python 3.11/3.12 with the extension **on**, and
  runs one additional job with it **off** to prove the pure-Python path is not
  quietly rotting.
- `FetchContent` needs network at configure time. CI caches the build directory;
  `make cpp` documents the requirement. The `OFF` path needs neither.
- **MSVC needs `/Zc:__cplusplus`.** Without it MSVC reports `__cplusplus` as
  `199711L` regardless of `/std:c++17`, for backward compatibility.
  `build_info()` reads that macro, so a Catch2 assertion about the standard
  fails on Windows and passes everywhere else. Both the extension **and** the
  `portable_probe` static library the Catch2 suite links carry the flag —
  flagging only one leaves the test binary and the extension disagreeing about
  what standard they were built to.
- **The no-extension CI job asserts the extension is genuinely absent**, rather
  than trusting the flag it just passed. That assertion is what caught
  `PORTABLE_BUILD_NATIVE=OFF` doing nothing, and it exists for the same reason
  the differential test skips loudly: a check that silently passes when its
  subject is missing has stopped being a check.
- Integrating `portopt` in v0.3 is `add_subdirectory` plus dependency-tag
  reconciliation, which is the point.

## Alternatives considered

- **Mandatory extension** — rejected: it makes a compiler a precondition for
  reading your own tax report, and it removes the differential test's control
  arm.
- **`setuptools` + a separate `make cpp`** — simpler, but the bootstrap fixes
  `scikit-build-core` precisely so `pip install .` is the whole story.
- **Cython / mypyc** — faster to adopt, but does not converge with `po`, which is
  the entire strategic reason C++ is here at all.
