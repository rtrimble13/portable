# ADR 0041 — A bond is held at face, priced by its quote basis, and carries its accrued interest

- **Status:** Accepted (owner, 2026-11-08), with the recommended answers, and schema 12 approved. No bonds are in the owner's file yet, so nothing is re-entered. Implemented: schema 12, `Instrument.value_of` and `price_of`, the trade rules, accrual and the coupon check in snapshots, the tax report's statement, the batch column, and the sample fixture. Two changes from the text as accepted are noted where they fall (§3 and §5)
- **Date:** 2026-11-08; accepted 2026-11-08
- **Milestone:** v0.2, a prerequisite of [ADR 0040](0040-time-weighted-return.md) §13.1
- **Governs:** `PORT-GIPS-A06` (accrued interest is required), `A05` (trade date), `A01`; `CLAUDE.md` "Accrued interest is part of market value"; `docs/tax-methodology.md`
- **Depends on:** [ADR 0005](0005-decimal-representation-and-rounding.md), [ADR 0019](0019-migrations-that-rebuild-a-table.md), [ADR 0031](0031-price-acceptance-against-statements.md)

## Context

ADR 0040 puts accrued interest into every valuation snapshot before any
return is computed, because the owner holds individual coupon bonds. The
code for it was started, and the bond model turned out to be thinner than
accrual alone.

1. **The quote basis is stored and never read.** `instrument_bond.quote_basis`
   defaults to `percent_of_par`. Valuation values every holding at
   `price × quantity × contract_size`, and a trade's gross amount is the
   same product. So a bond quoted at 99.50 percent of par is valued at 99.50
   per unit of quantity, whatever the unit is. Integration scenario 5 buys
   10 bonds at 1012.40 under the default `percent_of_par`. It passes only
   because it uses a price per bond, while the instrument says
   percent-of-par.
2. **No one has decided what a bond's quantity is.** `accrued_interest()`
   computes `face_value × coupon_rate × quantity`, which treats quantity as
   a number of bonds. Custodians report a bond position as face amount
   (25,000) priced as a percent of par (99.50). Nothing in the repository
   says which one `portable` uses, and nothing converts between them.
3. **A bond trade cannot record the accrued interest it exchanges.** A buyer
   between coupons pays the seller the interest accrued to settlement, and
   that is not basis (`CLAUDE.md`). The ledger has no field for it.
   - **The cash is wrong.** A buy's cash debit is short by that amount, and
     a sale's credit is short too.
   - **The first coupon is overstated.** The whole coupon is recorded as
     income, including the part that only repays what the owner paid at
     purchase.
   - **Accrual would make it worse.** The snapshot would start accruing
     interest the cash never paid for, and the purchase would read as a
     gain.
4. **Coupon dates are never derived.** `Holding.last_coupon_date` and
   `next_coupon_date` exist and nothing fills them. The schema stores
   `maturity_date`, `coupon_frequency` and `first_coupon_date`, but not the
   dated date that starts an odd first coupon period.
5. **Import cannot create a bond.** Identity comes through `duk` (ADR 0032).
   It classifies equities, ETFs and funds, and refuses anything else with
   "add it by hand". The batch format has no accrued-interest column.

No return has been computed yet, so none of this has produced a wrong
number. ADR 0040's engine would, starting with its first bond.

## Decision

### 1. Quantity is face amount; the quote basis decides the price

A bond's **quantity is its face amount in currency**, as custodians report
it: 25,000 means $25,000 of par. One method, `Instrument.value_of(quantity,
price)` (with its inverse `price_of`), is used by valuation, trades, replay,
reconciliation, P&L and price acceptance, and nothing else multiplies a
price by a quantity. For a bond it reads the quote basis:

| `quote_basis` | Market value |
|---|---|
| `percent_of_par` | `quantity × price / 100` |
| `price_per_bond` | `quantity / face_value × price` |

`face_value` becomes the denomination. A trade or position that is not a
whole multiple of it is refused. Open question 2 offers the alternative,
quantity as a number of bonds.

### 2. A bond trade states the accrued interest it exchanges

A new nullable `transaction.accrued_interest` column holds the interest
accrued to settlement and exchanged with the trade, signed like cash. This
is schema 12, added with `ALTER TABLE ADD COLUMN`: no rebuild under
ADR 0019, and the append-only triggers are untouched.

- **Cash.** A buy debits price + accrued + fees, and a sale credits
  price + accrued − fees.
- **Not basis, not proceeds.** Basis and proceeds stay as they are, and the
  tax engine never sees the accrued amount as either.
- **Stated, never computed.** Accrued interest runs to settlement, which
  differs by market and date. A bond trade without the field is refused,
  and an explicit `0` is allowed (a trade on a coupon date, or a bond
  trading flat). `portable` computes its own figure and warns when the
  stated one differs by more than a cent per $1,000 face. Open question 3
  offers a separate ledger row instead.
- **Replay** applies it as cash. The cash-conservation property (invariant 4)
  gains the term.

### 3. Snapshots accrue interest to the valuation date

Per `PORT-GIPS-A05`, accrual runs to the valuation date, not to settlement.

- **The coupon schedule** steps back from `maturity_date` by
  `12 / coupon_frequency` months. A maturity on a month-end keeps the
  month-end. `first_coupon_date` bounds the schedule.
- **A holding inside an odd first coupon period is refused**
  (`PT-E-ACCRUAL-UNAVAILABLE`), because the dated date that starts the
  period is not stored. This is rare, and the refusal names the bond.
- **A coupon is entitled on the scheduled date it pays**, and is a
  receivable from there until it is paid. The coupon row holds that date in
  `ex_date`. `pt income coupon` defaults it to the bond's last scheduled
  coupon on or before the pay-date, or takes `--coupon-date`. A coupon due
  on a Saturday and paid on the Monday is in value all weekend. Entering the
  row removes the snapshots from its coupon date, as a dividend's does
  (ADR 0040 §13.2).
- **A coupon the account was owed and that no row records makes the
  snapshot incomplete** (`PT-E-COUPON-UNRECORDED`), naming the bond and the
  date. "Owed" means a lot that arrived in the account before the coupon
  date is still open. That is the opening row's date, not the lot's
  holding-period start, which an in-kind lot carries back. An unrecorded
  coupon is a missing ledger row, not a market move.

  *Changed from the text as accepted*, which accrued a missing coupon for
  five days as a computed receivable. That figure rests on the quantity
  held on the coupon date, which open lots alone do not give. The row's
  arrival removes and revalues the days it covers anyway, so nothing is
  lost by refusing until it comes.
- **The amount.** ACT/ACT, the bond-market convention (ICMA), is
  `coupon_rate / coupon_frequency × quantity × days elapsed / days in the
  period`. The other conventions use `coupon_rate × quantity ×` their own
  year fraction. `domain/dates.accrual_fraction` took the ratio of two ISDA
  year fractions, which is not how a bond accrues across 1 January, and was
  never called. It is removed, and `domain/bonds.py` replaces it.

### 4. Tax

- **Purchased accrued interest** is not basis. It reduces the taxable
  interest of the coupon that repays it.
- **Accrued interest received on a sale** is interest income, not proceeds.

`pt tax` reports capital gains only today, so neither changes a reported
figure yet. Both are written into `docs/tax-methodology.md` so the interest
report, when it comes, starts from them.

**Bond premium amortization (§171), market discount (§1276) and OID are
not computed.** For any bond held, the tax report says so in its list of
what it does not handle. It does so in the footnotes and in a `bonds` field
of the JSON, which is null in a year with no bond.

*Changed from the text as accepted:* it proposed flagging the gain on a
tax-exempt bond bought at a premium as an estimate. Nothing records that a
bond is tax-exempt, so the statement covers every bond instead.

### 5. Import

A bond is added by hand (`pt instrument add --type bond --quote-basis …`),
which is the remedy the import already gives. The batch format gains an
optional `accrued_interest` column, required on a bond trade as it is from
the command line.

**Not built: an `accrued_interest` field in the activity mapping grammar**,
the step that turns a custodian's export into batch rows. Until it exists, a
bond trade from an export is refused at the batch, which states the missing
column. A bond trade is entered by hand, or the batch row is edited at the
review gate (ADR 0012). The import refuses, so nothing is guessed.

## Open questions for the owner

1. **Are your bonds in the `.port` file yet?** Import refuses them, so they
   are either missing or were entered by hand. If they were entered by
   hand, how: quantity as face amount or as a number of bonds, and price as
   a percent of par or per bond? That decides whether existing rows need
   reversing and re-entering under §1.
2. **Quantity as face amount** (recommended, §1, matching the custodian and
   ADR 0031's statement checks), or as a number of bonds of `face_value`
   each?
3. **Accrued interest at trade** as a column on the trade row (recommended,
   §2, schema 12), or as a separate ledger row (`accrued_interest_paid` /
   `_received`) beside the trade? A separate row needs a new transaction
   type, which rebuilds the ledger table (ADR 0019), and a pairing rule
   between the two rows.

## Owner's answers (2026-11-08)

1. **No bonds are in the file yet.** Import refused them and none were
   entered by hand, so there is nothing to reverse. The model applies from
   the first bond added.
2. **Quantity is face amount**, as recommended.
3. **Accrued interest is a column on the trade row**, as recommended:
   schema 12.

## Consequences

- **ADR 0040's engine** refuses any period in which a coupon bond is held,
  until this lands. Its §12 refusal stays in place until then.
- **Scenario 5** is rewritten under §1, with the bond's quote basis stated.
- **No existing rows change.** The owner's file holds no bonds (answer 1).
  Any other file that does is valued under §1 from schema 12, and its
  snapshots re-derive differently, so `pt validate` names them.

## Alternatives considered

- **Quantity as a number of bonds.** This matches `face_value`'s current
  use, but every custodian export would need dividing on import, and a
  statement check would compare different units. Open question 2.
- **Computing the accrued interest at trade** from the schedule and a
  settlement lag. It is fragile across markets and holidays, and the
  custodian's confirm states the figure. Computed only as a check.
- **Treating purchased accrued interest as basis.** This is the trap
  `CLAUDE.md` names. Rejected.
