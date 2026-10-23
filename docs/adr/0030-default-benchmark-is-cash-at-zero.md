# ADR 0030 — The default benchmark is cash at 0%, labelled as such

- **Status:** Accepted — implemented in `services/benchmarks.py` and `pt benchmark show`
- **Date:** 2026-10-20
- **Milestone:** v0.2
- **Governs:** `PORT-GIPS-G01`, `PORT-GIPS-G02`, `PORT-GIPS-G04`; `CLAUDE.md` invariant 10
- **Answers:** [ADR 0020](0020-historical-price-import.md) R8, for now

## Context

`PORT-GIPS-G02` allows no silent omission: every return is presented beside
a benchmark, or with the reason it has none. Choosing a benchmark is the
owner's investment-policy decision (ADR 0020 R8). fafnir carries no
total-return index, so none can come from there (ADR 0006).

The owner decided (2026-10-20):

- "The default benchmark should be cash."
- The owner will build a separate tool that constructs benchmark series.
  Integrating its output comes later.
- Asked whether "cash" means 0% or a Treasury-bill return: 0%, labelled as such.

`portable` already has the tables for a benchmark and its effective-dated
assignment (`benchmark`, `portfolio_benchmark`). Nothing wrote or read them.

## Decision

1. **A date with no benchmark assigned is benchmarked against `Cash (0%)`:**
   a return of exactly zero in every period. It is a constant in
   `services/benchmarks.py`, not a stored row. That makes it what "unassigned"
   means, so every portfolio has it, including one created before this
   decision, and no migration is needed. A return of zero is a total return,
   trivially, so `PORT-GIPS-G01` has nothing to refuse.
2. **It is labelled as exactly that, everywhere it appears.** Its description
   says "a return of exactly zero in every period. Not a Treasury-bill or
   money-market series". Its disclosure says no benchmark has been assigned and
   that a return above it is the portfolio's own return, not a return over a
   market. A column of zeros must not be read as a market that went nowhere,
   or as the risk-free rate (`PORT-GIPS-F05` names that separately).
3. **Leaving the default is a benchmark change.** When a benchmark is
   assigned, the move from `Cash (0%)` to it is disclosed with its date,
   type (prospective or retroactive) and reason, as every later change is
   (`PORT-GIPS-G04`).
4. **One resolver.** `benchmarks.history()` and `benchmarks.in_effect()` are
   the only way a benchmark is looked up:
   - a price-only benchmark is refused there (`PORT-GIPS-G01`);
   - two assignments effective the same day are refused as ambiguous.
5. **`pt benchmark show`** prints the benchmark in effect, its history, and
   each disclosure. In `--format json` the disclosures are envelope data, not
   rendered text.

## Consequences

- When `pert` computes a return, the benchmark beside it already exists and
  is already disclosed. The benchmark return over any period of a `Cash (0%)`
  stretch is zero.
- **Assigning a benchmark has no command yet.** The repository can store an
  assignment (`BenchmarkRepository.assign`) and the resolver reads it. The
  command that writes one, and the import of a series's levels, wait for the
  owner's benchmark tool. The format that tool should write is
  [docs/benchmark-series.md](../benchmark-series.md).
- **Still open from `PORT-GIPS-G04`:** how long each disclosure stays in a
  report: a prospective change for as long as the prior benchmark's returns
  are shown, a retroactive one for at least a year. That depends on the report
  period, which only `pert` knows.

## Alternatives considered

- **A Treasury-bill return.** A real cash benchmark, but the owner chose 0%.
  It would also need a T-bill series, and fafnir has none (`duk yc` is
  live-only).
- **No benchmark, with a disclosed reason**, which G02 also permits. This
  matches the owner's intent, but it means every report says "none" until the
  benchmark tool exists, and every consumer has to handle a missing column.
  The owner chose a stated default instead.
- **Store `Cash (0%)` as a row, assigned at `pt init`.** Rejected: an
  existing portfolio would need a migration to get a row whose content never
  varies, and the row's effective date would have to be invented.
