# Contributing to `portable`

`portable` computes numbers its owner files real taxes from and defends real
investment decisions with. That governs everything below.

**A silently wrong number is the worst possible failure mode in this repo** —
worse than a crash, worse than a missing feature. When in doubt, refuse and
explain.

Read [`CLAUDE.md`](CLAUDE.md) first. It is the standing working agreement: the
invariants, the placement rules, and the domain traps. This document is the
mechanics.

---

## Before you start

- **Check [`docs/adr/`](docs/adr/).** Decisions have reasons and the reasons are
  written down. If you disagree, write the next ADR — do not silently revert.
- **Check [`docs/gips-standard.md`](docs/gips-standard.md)** before touching
  valuation, cash flows, returns, risk measures, or benchmarks. It is
  provision-traceable and each requirement carries its own acceptance test. If
  you believe a requirement there is wrong, the argument has to be made against
  the cited provision, not around it.
- **Check in at phase boundaries** on multi-step work, especially before a
  schema change.

---

## Setup

```bash
scripts/bootstrap.sh          # or scripts\bootstrap.ps1 on Windows
source .venv/bin/activate
pre-commit install
```

---

## The invariants are not style preferences

Breaking one is a bug even if every test passes. The full list is in
`CLAUDE.md`; these are the ones contributors trip over.

1. **No binary floating point in any money, quantity, price, or rate path.**
   `Decimal` in Python, canonical decimal `TEXT` in SQLite. There is a lint rule.
   **Do not silence it** — inside a money-critical package the suppression marker
   is itself an error.
2. **The transaction ledger is append-only.** Triggers reject `UPDATE` and
   `DELETE`. Corrections are a reversing entry plus a new entry.
3. **All non-ledger state is derived and rebuildable.** If you add derived state,
   you add it to the replay path **and** to
   `tests/property/test_replay_reproduces_state.py` **in the same commit**.
4. **Cash is conserved**, and `sum(lot.remaining_quantity) == leg.quantity`. Both
   are property-tested. If your change makes one fail, your change is wrong.
5. **Determinism.** Same inputs, identical bytes out. No wall-clock, no locale,
   no unordered iteration in output.
6. **Never claim GIPS compliance, in any form.** Not in code, not in docs, not in
   a commit message, not in a report footer. There is a lint rule; it may not be
   silenced. The one approved disclaimer is `docs/gips-standard.md` §9.3.

---

## Where code goes

| Kind | Location |
|---|---|
| Business logic | `services/` — never in a CLI module, never in a repository |
| SQL | `persistence/` and `schema/` — nowhere else |
| Typed domain objects | `domain/` — no I/O, no SQL, no business rules |
| Anything fafnir-shaped | `providers/fafnir.py` — nowhere else |
| Output formatting rules | `formatters/`, once, not inline at call sites |

**If two CLIs would need the same logic, it belongs in `portable_core`.** No CLI
imports another CLI, ever. `tests/unit/test_layering.py` enforces all of this.

---

## Tests

**Tests land with the code they test, not after.**

```bash
make test-fast    # unit only -- what pre-commit runs
make test         # unit + property + integration + golden
make check        # everything CI runs
```

- **Unit** — every service, every relief method, every corporate action, every
  day-count convention. Boundary cases on holding period (exactly one year, one
  year plus a day, leap years) are load-bearing. Do not "simplify" them.
- **Property (hypothesis)** — the invariants above, over generated transaction
  sequences.
- **GIPS acceptance** — the tests named in `docs/gips-standard.md` are part of
  the suite. When you implement a requirement, **link the test name into that
  requirement's `Test` line**, so the document doubles as a coverage map.
- **Integration** — end-to-end through the real CLI against a real `.port` file,
  asserting on parsed JSON.
- **Golden files** — so formatting regressions show up in a diff.

**Before declaring any task done:** `make check` is green, and any new invariant
has a test that fails without your change.

---

## Schema changes

1. A **new numbered file** in `src/portable_core/schema/` — never an edit to an
   applied one.
2. Idempotent, and tested in both directions where reversible.
3. Bump `schema_version`; add a `CHANGELOG.md` entry.
4. `make docs` to regenerate `docs/schema.md` from the DDL comments.
5. Money, quantity, price, and rate columns are `TEXT` with a `-- decimal`
   comment.

---

## C++

**Every C++ path keeps a pure-Python reference implementation and a differential
test comparing them. No exceptions** — that fallback is how we know the fast path
is right.

The Python reference is written **first** and is **normative**. No native-only
functionality, ever. `Decimal` does not cross the pybind11 boundary. ADR 0008 and
`docs/architecture.md` §10 have the full contract.

Profile before you optimise. `docs/roadmap.md` lists the candidate hot paths;
none of them is a good idea until a profile says so.

---

## Commits and pull requests

- **[Conventional Commits](https://www.conventionalcommits.org/)**, small and
  reviewable, and **the message explains *why*** — the diff already shows what.
- **When a document becomes wrong, fix it in the same commit** that made it
  wrong. A stale `CLAUDE.md` is worse than none, and the same rule binds
  `docs/gips-standard.md`.
- **`make check` green** before you open the PR.

---

## Push back

If the design is wrong, say so and make the case. In a domain this fiddly,
agreeable compliance is not the helpful behaviour.
