# ADR 0013 — A cash sweep vehicle is cash, not an instrument

- **Status:** Proposed
- **Date:** 2026-09-08
- **Milestone:** v0.2
- **Governs:** `PORT-GIPS-A07`; `CLAUDE.md` invariant 4

## Context

Every brokerage account parks uninvested cash in a sweep vehicle. The owner's
accounts use two — a bank deposit sweep and a government money market fund — and
the broker reports both as positions in the holdings snapshot and as a stream of
transfers in the transaction history.

That stream is not small. In the sample export, **404 of 1,163 rows (35%) are
sweep bookkeeping**: `BANK DEPOSIT SWEEP PROGRAM MORNING TRADE @ 1`,
`TAS: Money Transfer from <SWEEP> to CASH`, `BANK DEPOSIT SWEEP PROGRAM NET INT
REINVEST`. They appear in pairs with the event that caused them: a dividend
credited to the account is followed, the same day and for the same amount, by a
transfer of that amount into the sweep.

Recording both halves of that pair credits the account twice. Recording neither
loses the cash. Recording the sweep as an instrument means every dividend,
every trade, and every fee generates a second ledger row whose only content is
that money moved between two representations of the same dollar.

`account.sweep_instrument_id` exists in the schema and no engine reads it, so
the question is genuinely open rather than settled by precedent.

## Decision

**The sweep vehicle is cash.** It is not an instrument, it holds no position, it
has no lots, and its movements are not ledger events.

Concretely:

1. **Sweep transfer rows are discarded at import**, marked as dropped with the
   rule that dropped them (ADR 0012), and counted in the import report. The
   causing event — the dividend, the sale, the fee — is the ledger row, and its
   `net_cash_effect` is the whole of the cash movement.
2. **Sweep income is income on the cash balance.** A money market distribution
   becomes `interest` or `dividend` against the account, not a purchase of
   additional fund shares. The broker reports it as a reinvestment producing
   fractional shares; those shares are a representation of cash and buying them
   is not an investment decision.
3. **`account.sweep_instrument_id` is dropped from the schema** in the migration
   that lands this, rather than left as a column no code reads. Invariant 10's
   reasoning applies to schema as much as to functions: a field that looks
   load-bearing and is not is a landmine.
4. **Reconciliation adds the broker's sweep positions to the broker's cash line**
   before comparing against `cash_balance`. This is the one place the decision
   has to be undone, and it is one line in the reconciler rather than a thousand
   rows in the ledger.

### Why this does not distort return

`PORT-GIPS-A07` requires that returns from cash be included in every return
calculation, and `portable` has no ex-cash basis. That requirement is satisfied
identically under either modelling: the sweep balance is in market value either
way, and its income is in the return either way. What changes is only whether the
dollar is labelled "cash" or "shares of a fund priced at 1.00".

Because the sweep vehicles hold a constant unit price, treating them as cash
cannot produce a valuation difference. Were the owner to hold a money market
fund whose NAV floats, or to hold one as a deliberate allocation rather than as
the account's sweep, that fund is an ordinary instrument and this ADR does not
apply to it. The distinguishing question is whether the vehicle is the account's
**automatic** destination for uninvested cash, not what kind of security it is.

## Consequences

- The ledger is roughly a third smaller and contains only rows corresponding to
  something the owner or the adviser actually did.
- Cash conservation (invariant 4) becomes checkable against the broker, because
  one balance faces one number rather than being spread across a cash line and
  two fund positions.
- `pt holdings` will not show the sweep vehicles as lines. The cash is in the
  cash row. This differs from the broker's own statement presentation, which is
  a reporting difference to be aware of when eyeballing the two side by side —
  and the reason reconciliation folds them together explicitly.
- The sweep's income appears as `interest` or `dividend` with no instrument
  attached. `classify` already returns `INCOME` for both at every level, so
  nothing downstream needs to know.
- Dropping `sweep_instrument_id` is a schema change: migration, `schema_version`
  bump, `CHANGELOG.md` entry.

## Alternatives considered

- **Model the sweep as an ordinary instrument.** Rejected: triples the row count
  for no analytical gain, makes every dividend a two-row event, and creates lots
  and holding periods for a cash balance. Its one advantage — that
  `pt holdings` matches the broker's statement line for line — is bought by
  making the ledger a transcription of the broker's bookkeeping rather than a
  record of what happened.
- **Model it as an instrument but suppress it in reporting.** Rejected: the worst
  of both. The rows exist, the cost is paid, and the presentation lies about it.
- **Keep the sweep rows as `journal` transactions.** Rejected: `journal` is
  correctly classified as internal at both levels, so this is *safe*, but it
  fills the ledger with 404 rows that say nothing, and `pt activity` becomes
  unreadable for the periods that matter most.
