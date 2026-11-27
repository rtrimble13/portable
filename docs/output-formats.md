# Output formats

Every command speaks four formats and is usable by a human at a terminal *and*
by an agent parsing stdout.

**Structured results go to stdout; logs, warnings, and progress go to stderr.**
That is what makes `pt holdings --format json | jq` work while `-v` is on.

A result's warnings are part of it where the format can carry them: the JSON
envelope's `warnings` field, and a **Warnings** section in Markdown. Table and
CSV cannot, so each warning is written to stderr as a `warning: ...` line,
`DRY RUN: nothing was written to the portfolio.` included. `--quiet` suppresses
those lines.

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
  "data": { "columns": [...], "rows": [...], "total_market_value": "103372.30", "is_complete": true },
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
Nor may an empty list look like a missing one: in `table` and `markdown` an empty
list or object reads `(none)`, never `—`. A wash-sale check that found nothing
and a figure nobody supplied are different facts.

### `disclaimer` is a field, not a footer

Always **present**, and `null` when the command emits no return. Absent and null
must not look the same to a consumer: one means "this command has no
disclaimer", the other means it went missing.

Carrying it as a named key rather than a rendered string means a consumer who
drops it has to delete `payload["disclaimer"]`, which shows up in their code. A
footnote at the end of a formatted block can be lost by accident.

`pt holdings` (`holdings-2.0`) values as a snapshot does, so its
`total_market_value` is what `pt mark` gives for the same accounts, accrued
income included. A total of the part that could be priced is not the
portfolio's, so when any line cannot be valued:
- the total and every weight are null, and `is_complete` is `false`;
- `valued_market_value` gives what could be valued, under its own name;
- `unvalued` names each missing line and its error code.

The schema enforces all three.

`bmk levels` (`bmk-levels-1.0`, ADR 0050) is refused unless the index's
build is current, and carries the hash of the inputs it was built from. Its
`return_types` labels `total_return_level` `total_return` and `price_level`
`price_only`, and `is_theoretical` is fixed `true`, so a consumer cannot take
the price level for a benchmark or the index for a track record unawares.
`bmk holdings` (`bmk-holdings-1.0`) gives each security's units, close,
points and weight on a built day.

`pt allocation drift` (`allocation-drift-1.0`, ADR 0048) values as
`pt holdings` does, so its `total_market_value` is the snapshot market value
too. It never states a weight over a part of the portfolio, so a holding it
cannot value refuses the command rather than nulling the weights. Two flags
say how far to trust what it does state:
- `is_complete` is `false` while anything is unclassified, or a class held
  has no target. Each is named, in `unclassified` and `no_target`, and the
  schema holds a complete report to both being empty.
- `is_estimated` is `true` exactly when `--estimate` carried a price, and
  `estimate` then lists each one with its date and age; otherwise it is
  `null`.

A `no_target` row has a null target, band and difference, never a zero.

`pt tax` requires a non-empty disclaimer **in the schema**, and a
`wash_sales.not_detected` list with at least one entry (`tax-2.0`, ADR 0037).
Removing the statement of what wash-sale detection does not cover therefore
breaks CI rather than a filing.

`pt lot candidates` requires both too (`lot-candidates-1.0`, ADR 0047).
Each row is one lot sold alone, so the rows are alternatives, and
`is_additive` is fixed `false` for a consumer that would sum them.
`data.rows` is the table. `data.candidates` holds each row's full detail in
the same order:
- the price with its date, source, age and status;
- the wash check, with each replacement and whether it defers the loss,
  loses it for good, or is left for manual handling;
- the estimate and its rates;
- `unstated`, the reason for every null.

The schema enforces that a null gain has a null tax and a stated reason, and
that a long-term lot has no days to long-term.

`pert twr` requires a non-empty disclaimer in its schema too (`twr-2.0`,
ADR 0040). Each of its four returns names its method, basis and period. Its
`is_annualized` is fixed `false`, and `is_supplemental` is `true` for
`net_of_all_costs` and for no other basis. A consumer cannot take a
supplemental figure for a GIPS basis without the schema noticing.

Every return also carries `is_estimated`, and `data.estimate` says what was
estimated (ADR 0044). Both are required from `twr-2.0` on, so a consumer
reads them before it reads a value.

- **Only under `--estimate MAX_SHARE`.** Without the flag `is_estimated` is
  always `false` and `estimate` always `null`. With it, they stay so unless
  a day the period needs could not be priced. Then the day is valued at
  carried prices, and `estimate` is an object.
- **What the object holds.** The `limit` given; the `peak` share and its
  day; the counts of carried `holdings` and the `oldest_age_days`; and
  `days`. Each day has the level's `value`, the carried `share` of it, and
  every holding carried. A holding gives its account, symbol, quantity, the
  `price` carried and the `last_known_price` it came from, its
  `price_date` and `age_days`, and its `source`, which is `price`, `fill`
  or `reinvestment`, with that row's id. It also gives `lowered_by`, the
  distributions per unit taken off since, and its `value` and `share`.
- **The schema ties the marks together.** When `estimate` is an object,
  every return must say `is_estimated: true`, and when it is `null`, none
  may. A consumer that drops one mark fails validation. Each `--daily`
  sub-period carries `is_estimated` too.
- **In `table` and `markdown`** an estimated return reads
  `… (TWR, net-of-fees, ESTIMATED)`. The title begins `ESTIMATED`. A
  footnote, repeated as a warning on stderr, states the days, holdings,
  peak share, limit and the oldest price's age.

A track record ends where nothing is held, and `pert twr` never links one
record to the next (ADR 0045 §4, `PORT-GIPS-H06`). Two refusals follow:

- **`PT-E-RETURN-BREAK`**: the period spans one record's end and the next
  one's beginning. Its context names both dates.
- **`PT-E-RETURN-NOTHING-HELD`**: nothing was held at all in the period.

A period that starts or ends in days with nothing held is moved to the
record. `period.start` and `period.end` are what was measured, beside
`requested_start` and `requested_end`. `nothing_held_before` and
`nothing_held_after` name the days set aside. `twr-2.0` dropped `1.0`'s
`subperiods.idle_days`, since a measured period no longer holds such a day.

`pert periods` (`periods-1.0`, ADR 0045) carries every period as a row.

- **The rows.** `data.summary` holds month, quarter and year to date, 1, 3,
  5 and 10 years, and since inception. `data.fiscal_years` holds every
  fiscal year, oldest first. `data.track_records` lists the records they
  are clipped to.
- **Returns or a refusal, never both.** A row has either `returns` or a
  `refusal`, never both and never neither, and the schema enforces it. A
  refusal is the same object an error envelope carries.
- **A row says when it was clipped.** It names the period requested and
  the period measured. `partial` says why the two differ: `inception`,
  `restart` or `termination` (`PORT-GIPS-H02`).
- **Annualized or the reason it isn't.** Each basis has `cumulative`, and
  either `annualized` or `not_annualized`, the reason it has none: under a
  year, `PORT-GIPS-B07`, or a total loss.
- **Its own disclosures, and its own estimate.** Every row has its own
  `disclosures`, `twr-2.0`'s object for its own window. Under `--estimate`
  each row is estimated or not on its own (ADR 0044 §3). A row that could
  be measured is measured, with `estimate: null`. A row that needed a
  carried price has `twr-2.0`'s estimate object, and every figure in it is
  marked, which the schema enforces. A refused row has neither.

In `table` and `markdown` the rows are shown at one basis, `--basis`,
net-of-fees by default. Each row's detail stays in JSON, and footnotes give
what a reader needs: why each null or partial row is so, any break, and any
holding valued at zero.

`pert mwr` (`mwr-1.0`, ADR 0046) carries the money-weighted return, with the
time-weighted return over the same days beside it.

- **Never alone.** `data.twr` is required. A period the time-weighted return
  refuses is refused, with the same error (`PORT-GIPS-C01`).
- **Annualized from a year, by anniversary.** `period.is_annualized` fixes
  every money-weighted value's `is_annualized`. Under a year the figure is
  cumulative and `twr.annualized` holds only nulls (`PORT-GIPS-B07`, `C03`).
  The schema enforces both.
- **Since inception, or a period.** `period.is_since_inception` is true when
  the period starts where a track record does. Anything else is a period MWR,
  and says so.
- **The solve and the proof.** Each basis records its `solve`: the steps,
  the bracket and the residual. It also records `only_rate`: compounded at
  the rate, the lowest balance before the end and its day. A rate that
  cannot be shown to be the only one is refused with `PT-E-MWR-NOT-UNIQUE`,
  and a period with no rate in the bracket with `PT-E-MWR-NO-CONVERGENCE`
  (`PORT-GIPS-C04`).
- **Estimates.** `is_estimated` marks the money-weighted return when its
  beginning or ending value was carried, and nothing else does. The
  time-weighted return is marked on its own, and `twr.estimate` is
  `twr-2.0`'s estimate object.

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

**Every format is UTF-8, on every platform**, stdout and stderr both. Python
would otherwise encode a redirected or piped stdout in the console's code page
-- cp1252 on a Western Windows install -- which crashed on a `→` and wrote a `—`
as a different byte than Linux does. `pt` sets both streams to UTF-8 before it
writes anything, whatever `PYTHONIOENCODING` says
(`portable_core/cli/streams.py`). Line endings are the platform's. On Windows,
read a saved file as UTF-8 (`Get-Content -Encoding utf8` in Windows PowerShell
5.1, whose default is the ANSI code page).

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
with yours. A return is its value, and its column header names the method,
basis and whether it is annualized. Two marks stay in the cell itself,
because no header can carry them for one row: ` SUPPLEMENTAL`
(`PORT-GIPS-H08`) and ` ESTIMATED` (ADR 0044). A cell a spreadsheet cannot
read as a number is better than an estimate it takes for a measurement.

**`markdown`** — GitHub-flavored tables with numeric columns right-aligned, for
pasting into notes or an LLM context window.

**`table`** — the human default. Rich-rendered and colour-aware; degrades to
plain text automatically on a non-TTY or under `NO_COLOR`, so piping into a file
yields clean text without anybody remembering a flag.

**Nested data, in `table` and `markdown`.** A command's `data` often holds
structures: `pt tax` carries a list of per-rung provenance rows, the bonds it
held, and what its wash-sale check does not detect. An error's context can hold
a list of problems. All of these render through
`formatters/nested.py`, once, as indented key and value lines. In `markdown`
they render as a nested list. They never print as a Python `repr`:

```text
basis_provenance
  - basis_source  derived
    gain          15250.68
bonds
  held          T-4.25-2030
  not_computed
    - bond premium amortization (IRC §171)
unreportable      (none)
```

- Keys are sorted at every depth, as `json` sorts them. Lists keep their order.
- A list holds one item to a line. The exception is a list where every item is a
  single token, such as a date or a symbol. Those items run together, separated
  by commas, and wrap at a fixed 76 columns. The width is never read from the
  terminal, so the output stays deterministic. An item containing a space or a
  comma is never joined this way.
- A scalar is its canonical text (`6000.00`, never `Decimal('6000.00')`). A
  return goes through the return rules below, so a return nested in `data` is
  labelled and cannot be annualized under a year.
- `json` keeps the structure exactly. This rendering is for reading only.

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
Attempting it raises `PT-E-GIPS-ANNUALIZE-SUB-YEAR`, in every format: JSON
and CSV check it as `table` and `markdown` do. A year is counted by
anniversary, not as 365 days (ADR 0045 §5). It binds the
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
  the real write, rolled back, rather than a separate estimate (ADR 0026);
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
