# Output formats

Every command speaks four formats and is usable by a human at a terminal *and*
by an agent parsing stdout.

**Structured results go to stdout; logs, warnings, and progress go to stderr.**
That is what makes `pt holdings --format json | jq` work while `-v` is on.

---

## The JSON envelope

```json
{
  "schema_version": "1.0",
  "portable_version": "0.1.0",
  "command": "holdings",
  "generated_at": "2026-08-27T12:00:00Z",
  "as_of": "2025-06-30",
  "portfolio": "Sample Portfolio",
  "data": { "columns": [...], "rows": [...], "total_market_value": "103372.30" },
  "warnings": [],
  "disclaimer": null
}
```

Published as JSON Schema under [`schemas/`](../schemas/) and validated in CI
against **real command output**, not hand-written examples.

### Pin against `schema_version`

Not against `portable_version`. A patch release that changes nothing in the
output must not look like a breaking change to a consumer.

### `Decimal` is always a string

`"19050.00"`, never `19050.00`.

A JSON number cannot round-trip a decimal, and a consumer that parses one as a
float has silently lost the guarantee the entire codebase is built on. The
schema says `"type": "string"` for exactly this reason, so a generated client
gets it right without being told.

**Full stored precision, never rounded.** `"100.000000"` stays six places
because the trailing zeros carry the significance the source asserted.

### Null is explicit, and is never zero

| Format | A missing value |
|---|---|
| `json` | `null` |
| `csv` | empty cell |
| `table` / `markdown` | `—` |

A blank and a zero must never look the same. An unpriced holding reports
`"market_value": null` — not `"0.00"`, which would say the position is worthless.

### `disclaimer` is a field, not a footer

Always **present**, and `null` when the command emits no return. Absent and null
must not look the same to a consumer: one means "this command has no
disclaimer", the other means it went missing.

Carrying it as a named key rather than a rendered string means a consumer who
drops it has to delete `payload["disclaimer"]`, which shows up in their code. A
footnote at the end of a formatted block can be lost by accident.

`pt tax` requires a non-empty disclaimer **in the schema**, and pins
`excludes_wash_sales` as a `const`. Removing the wash-sale statement therefore
breaks CI rather than a filing.

### Errors are JSON too

Under `--format json` a failure writes a well-formed envelope to **stdout** and
exits with its code:

```json
{ "error": { "code": "PT-E-GIPS-NO-FLOW-POLICY", "message": "...",
             "exit_code": 4, "remedy": "...", "context": {...} },
  "data": null, "warnings": [] }
```

A consumer parsing stdout gets structured JSON whether the command succeeded or
failed, rather than needing a special case for "the output is not JSON today".

**Branch on `code`, never on `message`.** Codes are stable; messages get
improved. `pt introspect` publishes the full list.

### Determinism

Same inputs, identical bytes. Keys are sorted; nothing depends on dict insertion
order, the locale, or unordered iteration.

`generated_at` is the one wall-clock value, and it is **excluded from the content
hash** along with `portable_version` — neither is part of what the command
*computed*. That is what makes `PORT-GIPS-J01`'s error detection work: rebuild a
report, hash it, compare. Including the clock would mean the hash only ever told
you that time had passed.

---

## The other three

**`csv`** — RFC 4180, one logical table per invocation, header row, no
formatting, full precision. A spreadsheet is where somebody re-does the
arithmetic, and handing it a rounded number guarantees their total disagrees
with yours.

**`markdown`** — GitHub-flavored tables with numeric columns right-aligned, for
pasting into notes or an LLM context window.

**`table`** — the human default. Rich-rendered and colour-aware; degrades to
plain text automatically on a non-TTY or under `NO_COLOR`, so piping into a file
yields clean text without anybody remembering a flag.

---

## Number presentation

All of it lives in `formatters/numbers.py`, once, and not inline at call sites.

| | `table` / `markdown` | `json` / `csv` |
|---|---|---|
| Currency | `1,234,567.89` | `"1234567.89"` |
| Negative | `-1,234.50` — leading minus | `"-1234.50"` |
| Quantity | `100`, trimmed | `"100.000000"` |
| Sub-penny price | `0.0003123` | `"0.0003123"` |
| Small return | `3.0 bps` | `"0.0003"` |
| Null | `—` | `null` / empty |

Two choices worth explaining. Negatives take a **leading minus** rather than
parentheses: both are defensible and consistency is what matters, and the minus
survives being copied into a spreadsheet or a narrow terminal, where a lone `)`
at a line break becomes ambiguous. And a **sub-penny price does not floor to
`0.00`** — a back-adjusted series or a deep out-of-the-money option genuinely
trades there, and rendering it as zero says something false. This mirrors
`duk`'s behaviour, so the two tools show the same number.

---

## The two return rules

These live in the formatter **so that no call site can bypass them**.

**A return for a period shorter than one year is never annualized.**
`PORT-GIPS-B07` — unconditional, from Firms 2.A.12 and Asset Owners 22.A.9.
Attempting it raises `PT-E-GIPS-ANNUALIZE-SUB-YEAR`. It binds the
since-inception money-weighted return too, which is the one place the natural
implementation returns an annualized rate by construction and must be
de-annualized or refused (`PORT-GIPS-C03`).

**Every rendered return carries its method, basis, and period.**
`PORT-GIPS-H04`. There is no shape in this API that carries a bare number: a
`ReturnValue` cannot be constructed without all three, and `render_return`
takes only a `ReturnValue`. A return without its basis is not interpretable, and
a reader given one will assume whichever basis flatters the manager.

```json
{ "value": "0.0842", "method": "twr", "basis": "net_of_fees",
  "period_start": "2024-01-01", "period_end": "2024-12-31",
  "period_days": 365, "is_annualized": false, "is_supplemental": false }
```

---

## Agent and MCP integration

The hooks are built; the server is backlog (P1).

```bash
pt introspect --format json
```

Emits the complete command tree — commands, arguments, types, defaults, help
text, the exit-code table, and every stable error code — **sufficient for a
generator to produce MCP tool definitions without parsing `--help`**. Anything
derived from help text is a scraper, and a scraper breaks silently the first
time the help is reworded.

Everything else an agent needs is already true:

- every command is **non-interactive-capable** — anything that could prompt has
  a flag that supplies the answer, and refuses rather than blocking on a
  non-TTY;
- `--dry-run` on every mutating command prints the exact effects, because it is
  the same code path with the write suppressed rather than a separate estimate;
- exit codes are meaningful and documented;
- output is deterministic, so a diff of two runs shows the change and nothing
  else.

---

## Do not double-deduct fund expenses

Worth stating on this page because it is a presentation-layer temptation: ETF
and mutual fund NAVs are **already net** of the fund's expenses.
`PORT-GIPS-D05` requires those expenses to be reflected, and they are.
"Correcting" for an expense ratio on top of a NAV-based return double-counts it
— a silently wrong number of exactly the class this repository fears most.
