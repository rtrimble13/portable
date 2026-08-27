# ADR 0005 — Decimal representation, storage, and rounding policy

- **Status:** Accepted
- **Date:** 2026-08-27
- **Milestone:** v0.1

## Context

`CLAUDE.md` invariant 1 and bootstrap §3.2: no binary floating point in any
money, quantity, price, or rate path. `Decimal` in Python, canonical decimal
`TEXT` in SQLite, adapters registered in exactly one place, one rounding policy
applied only at documented boundaries.

SQLite has no decimal type. Its `NUMERIC` affinity silently converts to `REAL`
for values it cannot hold as an integer — which is exactly the bug we are
avoiding, wearing a disguise.

## Decision

### Storage

Money, quantity, price, and rate columns are declared **`TEXT`**, and every such
column carries a `-- decimal` DDL comment that the schema-doc generator and the
no-float lint rule both key off.

The canonical string form, produced by `portable_core.decimals.to_text()`:

- Produced by `format(value, 'f')` — never `str()`, which yields `1E+2`.
- Never `NaN` or `Infinity`; both raise.
- Negative zero normalises to `0`.
- Trailing zeros are **preserved**, because they carry the significance the
  source asserted: a price quoted `10.500` is stored `10.500`. Ordering
  comparisons are therefore done in Python or via `CAST(x AS REAL)` **only in
  non-money contexts**; money comparisons happen in the service layer.

### Adapters

`sqlite3.register_adapter(Decimal, ...)` and
`sqlite3.register_converter("DECIMAL", ...)` are registered exactly once, in
`portable_core/persistence/connection.py`, which is the only module allowed to
call them. `detect_types=sqlite3.PARSE_DECLTYPES` is **not** used, because column
declared types are `TEXT`; conversion happens in the repository mapping layer,
explicitly, so a forgotten conversion is a `str` where a `Decimal` is expected
and fails loudly rather than a silent float.

### Arithmetic context

One `decimal.Context` in `portable_core/decimals.py`:

- `prec = 34` — comfortably above IEEE 754-2008 decimal128, and far above any
  realistic share count × price.
- `rounding = ROUND_HALF_EVEN` — the valuation default the bootstrap fixes.
- `traps` include `InvalidOperation`, `DivisionByZero`, and **`Inexact` is not
  trapped** (division of money by a share count is legitimately inexact);
  `Overflow` and `Underflow` are trapped.

All engine arithmetic runs inside `with decimal.localcontext(PORTABLE_CONTEXT)`.

### Rounding boundaries — the only places rounding happens

| Boundary | Rule |
|---|---|
| Currency amount persisted to the ledger | 2 dp, `ROUND_HALF_EVEN` |
| Cost basis and proceeds | 2 dp, `ROUND_HALF_EVEN` |
| Per-unit price persisted | **not rounded** — stored as supplied by the source |
| Share quantity | account-configurable: whole shares or 6 dp fractional; a corporate action producing a fractional share an account cannot hold is an error, not a rounding (`CLAUDE.md` invariant 9) |
| Option contracts | whole contracts, `ROUND_HALF_EVEN` never applies — a fractional contract raises |
| Rates and factors | not rounded in intermediate arithmetic; rendered at the formatter |
| Human output (`table`, `markdown`) | formatter-only, per `docs/output-formats.md` |
| `json` / `csv` output | **never rounded** — full stored precision, `Decimal` as a JSON string |

Intermediate results are **not** rounded. Allocation of a total across lots uses
largest-remainder so the parts sum exactly to the whole; the residue is never
dropped.

## Consequences

- A `float` cannot enter a money path without either the lint rule or the
  dataclass `__post_init__` guard rejecting it.
- Sorting money in SQL is not available. Repositories that need ordered money
  (e.g. HIFO lot selection) sort in Python inside the engine, which is where the
  relief-method logic belongs anyway.
- Storage is larger than `REAL`. Irrelevant at this scale.

## Alternatives considered

- **Integer minor units (cents)** — exact and fast, but wrong for prices with
  more than 2 dp (fafnir stores `NUMERIC(20,6)`), for share quantities, and for
  rates. It also pushes a scale factor into every call site.
- **SQLite `NUMERIC` affinity** — silently becomes `REAL`. This is the bug.
- **A SQLite decimal extension** — a loadable extension the owner would have to
  build on Windows; rejected on portability.
