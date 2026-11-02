# ADR 0035 — What a valuation snapshot accrues, and the bond it cannot yet value

- **Status:** Accepted — implemented in `services/valuation.py`, `services/snapshots.py` and `TransactionRepository.append`. Bond accrual is an accepted gap, below
- **Date:** 2026-11-02
- **Milestone:** v0.2
- **Governs:** `PORT-GIPS-A06`, `PORT-GIPS-D01`; `CLAUDE.md` invariants 3, 9, 10
- **Amends:** [ADR 0024](0024-valuation-snapshots-are-valued-state.md) — the rows that make a snapshot stale

## Context

`valuation_snapshot` carries `accrued_interest`, `accrued_dividends`,
`income_amount` and `fees_amount`, and its DDL comment says accrued income is
part of beginning and ending value and that "`portable` does both". The
engine could compute both accruals when handed the inputs, and its unit tests
handed them over. The one path that builds stored snapshots
(`snapshots.build`, behind `pt value` and `pt validate`) passed none of them.
Every stored snapshot carried zero for all four, whatever happened that day.
`accrued_interest` also returned zero for a coupon bond whose coupon dates
were missing, and the snapshot path never supplied them.

Issue #1 told `pert` all four were there. A return engine reading them would
have been correct only by luck: the reference portfolio holds no individual
bonds, and every distribution it imported carries no ex-date.

## Decision

1. **A distribution accrues from its ex-date to its pay-date.**
   `PORT-GIPS-A06` *recommends* this for dividends (Firms 2.B.3, AO 22.B.3);
   it is not required, and nothing here says it is. A row whose ex-date
   precedes the day it is dated (`record_income` dates it on its pay-date)
   is owed from its ex-date, so each snapshot from the ex-date until the
   pay-date includes it in `accrued_dividends`. The rule is not limited to
   any transaction type: an ex-date before the row's date is exactly what an
   entitlement before payment means. The rows come from the effective ledger,
   so a reversed distribution is owed to nobody.

2. **The amount accrued is what the pay-date delivers**: the gross less the
   tax withheld from it. On the pay-date the receivable becomes cash, or units
   for a reinvested distribution, of the same value, and that day's return is
   not moved. Accruing the gross would put the withholding into the pay-date
   instead of the ex-date. Reclaimable withholding is not carried as a
   receivable after the pay-date. That was true before this ADR, and it stays
   open. The reference portfolio reclaims none.

3. **A snapshot depends on a row dated after it**, and that row's arrival
   makes it stale. ADR 0024 removes every snapshot a ledger write changes, and
   measured that from the row's date. A row whose ex-date precedes its date
   now counts from its ex-date. Without that, recording a distribution when it
   is paid left the snapshots since its ex-date describing a book without it.
   `tests/property/test_snapshots_survive_ledger_writes.py` generates
   distributions and pins the case.

4. **No ex-date, no accrual.** `record_income` stores an omitted ex-date as
   the pay-date, so the row accrues nothing and is recognised when paid.
   Every imported distribution is such a row, because the reference
   custodian's export carries no ex-date. For that portfolio a distribution's
   price drop (ex-date) and its cash (pay-date) fall on different days. Across
   a month end that moves a month's return by roughly the distribution. This
   decision does not guess an ex-date from a typical lag. The real ex-dates can
   be brought in from fafnir's corporate actions (`duk ca`, ADR 0032). That
   would be its own change, and it would make every stored snapshot from the
   first ex-date stale. Until then `pert` must disclose that distributions
   without an ex-date are recognised when paid.

5. **`income_amount` and `fees_amount` are filled**, from the day's rows
   through the classifier (ADR 0007). Income is each income row's delivered
   value on the day it is delivered. Costs are every row the classifier calls
   a cost, fees of every class and margin interest together, as a positive
   amount charged. Both are memos: market value already reflects them. The cost
   memo **cannot derive a return basis**, which needs each fee's `fee_class`
   (`PORT-GIPS-D01`). `pert` derives bases from the rows.

6. **A coupon bond makes its snapshot incomplete. This is an accepted gap.**
   `PORT-GIPS-A06` *requires* accrued interest in market value. A snapshot
   cannot supply it honestly yet. The coupon schedule is derivable from the
   bond's maturity, frequency and first coupon date. What is missing is the
   ledger side: a buy records clean price times quantity, and nothing records
   the accrued interest the buyer paid the seller, or a seller receives. With
   the accrual in market value and not in the trade's cash, buying a bond
   between coupons is a one-day gain of the accrual and selling one is a
   one-day loss. So the snapshot values the bond at its price, is marked
   incomplete, and names it: *accrued interest on a coupon bond is not
   modelled yet*. An incomplete snapshot is never used for a return. A
   zero-coupon bond has nothing to omit, because its accretion is in its
   price. Closing this gap needs the trade's accrued interest on the ledger
   row (a schema change), the coupon schedule, and both in the
   cash-conservation property test. It is deferred until a bond is held. The
   reference portfolio holds bond ETFs, which accrue inside their NAV.

7. **An incomplete snapshot says why.** `ValuationSnapshot.unvalued` names
   each holding the snapshot could not value and the reason, for `pt value`
   to report. It is not stored: `is_complete` is what is kept, as before.
   `accrued_interest` with no coupon dates now raises instead of returning
   zero.

## Consequences

- **Stored snapshots change.** A snapshot dated on a day with income or a
  cost, or inside a dated distribution's accrual window, re-derives
  differently from what was stored. `pt validate` names each one, and
  `pt value` over its dates values it again. Nothing is removed by this
  change itself. A snapshot of an account holding a coupon bond re-derives as
  incomplete.
- The schema comment on `valuation_snapshot` overstates what is accrued and
  cannot be corrected in place, because SQLite keeps a table's text and a
  changed comment would change every file (ADR 0022). This ADR is the
  correction, and `docs/schema.md`, which is generated from that text, still
  reads as before.
- `pert`'s disclosure block must state decisions 4 and 6 where they apply.

## Alternatives considered

- **Accrue bond interest anyway, from the coupon schedule.** Rejected: it
  turns every purchase between coupons into a gain, which is the kind of
  plausible wrong number this repository exists to prevent.
- **Infer an ex-date from the pay-date** (a few days earlier). Rejected: it is
  a guess presented as data, and it would differ from the real ex-date often
  enough to move returns across month ends in both directions.
- **Record accruals as `accrual_income` ledger rows on the ex-date.**
  Rejected for now: it doubles every distribution in the ledger and makes its
  reversal two rows. The entitlement is already on the row, as its ex-date.
- **Drop `income_amount` and `fees_amount`.** A schema change for no gain,
  since both can be stated truthfully. Filled instead, and documented as
  memos.
