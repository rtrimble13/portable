# ADR 0015 — In-kind transfers, and how a position that predates the ledger enters it

- **Status:** Accepted — implemented in schema 0003, `pt transfer in` / `pt transfer out`
- **Date:** 2026-09-08
- **Milestone:** v0.2
- **Governs:** `PORT-GIPS-B02`, `PORT-GIPS-C02`; `CLAUDE.md` invariants 2, 3, 5
- **Amended by:** [ADR 0017](0017-cutover-reconstruction-and-basis-provenance.md) —
  the refusal on averaged basis, below, is replaced by a recorded provenance ladder

## Context

The owner's IRA and Roth were funded by an in-kind transfer from a previous
custodian: on one day in May 2022, thirty-odd fund and ETF positions arrived,
carrying their original acquisition dates. The holdings snapshot shows one lot
acquired in 2018, four years before the adviser relationship began. No
transaction the adviser will ever produce describes that purchase.

The same gap exists at a smaller scale for every position opened before the
transaction export's window: in the sample export, **44 of 75 current security
positions**, holding the majority of the portfolio's cost basis.

`portable` has no way to express any of this. Every transaction type that
creates a lot also moves cash, so the only available route is a back-dated `buy`
— which invents a cash outflow, which then needs an invented `deposit` to fund
it. That invented deposit is an **external cash flow**, and inventing external
flows is precisely how a track record is silently rewritten (ADR 0007). The
schema also carries `is_in_kind` on `snapshot_flow` and a `classify` parameter
for in-kind valuation, both currently reachable only by a stock dividend.

## Decision

Two new transaction types, `transfer_in` and `transfer_out`, for securities
crossing the portfolio boundary without being bought or sold.

### What they carry

A `transfer_in` records **two different numbers that must not be conflated**:

- **The lot's basis and acquisition date** — what the owner paid, and when. These
  come from the delivering custodian, are unrelated to the transfer, and are
  what the tax engine uses forever after.
- **The market value on the transfer date** — what arrived, valued when it
  arrived. This is the flow amount and has nothing to do with the tax basis.

Conflating them is the failure mode this ADR exists to prevent: use market value
as basis and every future sale reports the wrong gain; use basis as the flow
amount and the return for the transfer period is wrong by the whole unrealized
gain.

So the ledger row carries `quantity`, `price` (market on the transfer date) and
`gross_amount` (their product, the flow amount) in the existing columns, plus two
new nullable columns bound by a `CHECK` to these two types:

- `original_basis` — total cost basis of the transferred quantity;
- `original_acquired_date` — when the transferred shares were acquired.

`net_cash_effect` is `'0.00'`. Nothing moved but securities.

### Holding period is preserved, not restarted

The lot is created with `open_date = original_acquired_date`. A change of
custodian is not a disposition; the holding period runs from the original
acquisition. Restarting it would convert long-term gains into short-term ones on
the next sale — a wrong number in the direction of a larger tax bill, arrived at
silently.

One consequence worth stating plainly: `transaction.trade_date` and the lot's
`open_date` are no longer the same date for these rows. Replay ordering still
uses `(trade_date, seq)` — the ledger's order is the order things were recorded,
not the order shares were originally bought — while lot ageing uses `open_date`.

### Flow classification

`classify` gains two arms. A `transfer_in` or `transfer_out` is **`EXTERNAL`,
`is_in_kind = True`, at both levels**, valued at the transfer date
(`PORT-GIPS-C02`; the `docs/gips-standard.md` §6 matrix row for an in-kind
transfer in or out, which is a flow where it crosses the portfolio boundary).

With one exception, decided here: **a transfer on the account's `opened_date`
establishes the account's beginning market value rather than a flow into it.**
There is no prior period for capital to flow *from*. Treating the initial
funding as a first-day contribution would make the first period's return a
division against a zero beginning value. This exception is stated as
`portable`'s rule and must be re-checked against `docs/gips-standard.md` when the
return engine lands in v0.2; if the standard's treatment differs, the standard
wins and this paragraph changes.

An in-kind movement between two accounts *inside* the portfolio is **out of
scope and refused**. It cannot occur between the owner's accounts as they stand,
and the case that would need it — an in-kind Roth conversion — has tax
consequences no part of `portable` currently models. Refusing is honest;
accepting it and getting the conversion's taxable amount wrong is not.

### Seeding a position that predates all available records

A position whose purchase no export describes is entered as a `transfer_in`
dated at the account's opening (or at an agreed cutover), with
`original_acquired_date` and `original_basis` taken from the broker's lot detail
report. This is the *same* mechanism as a real transfer, because it is the same
event: shares that exist, whose history began elsewhere.

Where the broker supplies only a position-level average cost rather than lot
detail, a single averaged lot standing in for several real ones imposes
average-cost relief on an account whose method is spec-ID or FIFO, changing both
the gain and its holding-period character on every subsequent sale.

This ADR originally refused such a seed outright. **[ADR 0017](0017-cutover-reconstruction-and-basis-provenance.md)
supersedes that**, on evidence that no lot-detail report will be available for
these accounts and that refusing therefore declines to build the portfolio at all
rather than declining to guess. The rule is now that an averaged block may be
seeded, and that its basis carries a `basis_source` recording how it was arrived
at, which every report that consumes it must disclose. What remains refused is a
seed that cannot be told apart from an exact one.

## Consequences

- Schema change: two columns, two `CHECK` constraints, two new enum members in
  `txn_type`. Migration, `schema_version` bump, `CHANGELOG.md` entry.
- `classify` is total over `TransactionType`, so the two new members fail
  `mypy --strict` until they are classified. That is the mechanism working.
- `ReplayEngine.apply_transaction` gains a lot-creating path that does not
  consume cash, and `tests/property/test_replay_reproduces_state.py` gains the
  types in the same commit (invariant 3).
- Cash conservation (invariant 4) is unaffected: zero cash effect on both sides.
- `sum(lot.remaining_quantity) == position.quantity` (invariant 5) holds by the
  same path a `buy` uses.
- A new `pt transfer in` / `pt transfer out` command pair, and the batch format
  gains the two fields.
- `pt tax` gains a real obligation: a lot whose basis came from anywhere other
  than `portable`'s own ledger is marked as such in the lot record, so a tax
  report can say which figures rest on an external assertion or a reconstruction.
  ADR 0017 specifies the column and the disclosure. Covered vs. non-covered status
  is the broker's to state and `portable`'s to carry, not to infer.

## Alternatives considered

- **Back-dated `buy` plus an offsetting `deposit`.** Rejected: fabricates an
  external cash flow for every seeded position, which is the one error class
  ADR 0007 exists to prevent. It also dates the acquisition correctly only by
  dating the fictional cash movement wrongly.
- **A `lot_seed` configuration table outside the ledger.** Tempting — it looks
  like config, it is effective-dated, and it avoids touching `txn_type`.
  Rejected by invariant 3: lots are **derived** state, and derived state must be
  reproducible from the ledger. A lot with no ledger row behind it cannot survive
  `pt rebuild`, so either the rebuild learns to read a second source of truth, or
  the lots vanish. Both are worse than a transaction type.
- **A single `transfer` type extended to carry securities.** Rejected: `transfer`
  means an internal movement that nets to zero at portfolio level, and these
  cross the boundary and do not net. Overloading it would put two opposite flow
  classifications behind one type and make ADR 0007's single classification
  function branch on the presence of an instrument.
- **Recording the transfer at market value as basis, with a note.** Rejected: it
  is the single most common way this is got wrong elsewhere, and the note is not
  read by the tax engine.
