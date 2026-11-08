# ADR 0041 — A bond is held at face, priced by its quote basis, and carries its accrued interest

- **Status:** Proposed. Records what the bond model lacks and three open questions for the owner. No code yet
- **Date:** 2026-11-08
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

## Decision (proposed)

### 1. Quantity is face amount; the quote basis decides the price

A bond's **quantity is its face amount in currency**, as custodians report
it: 25,000 means $25,000 of par. One function,
`bond_market_value(bond, quantity, price)`, is used by valuation, trades,
reconciliation and price acceptance, and nothing else multiplies a bond's
price:

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
- **A coupon due and not yet recorded is still accrued.** A scheduled
  coupon dated on or before the valuation date, with no coupon row for it
  yet, stays a receivable in accrued interest. This covers a coupon falling
  on a weekend and paid on the Monday. A coupon still unrecorded after the
  staleness tolerance (5 days, the price tolerance) makes the snapshot
  incomplete, naming the bond and the date. An unrecorded coupon is a
  missing ledger row, not a market move.
- **The amount** is `coupon_rate / coupon_frequency × quantity ×
  accrual_fraction`, using the existing day-count functions.

### 4. Tax

- **Purchased accrued interest** is not basis. It reduces the taxable
  interest of the coupon that repays it.
- **Accrued interest received on a sale** is interest income, not proceeds.

`pt tax` reports capital gains only today, so neither changes a reported
figure yet. Both are written into `docs/tax-methodology.md` so the interest
report, when it comes, starts from them.

**Bond premium amortization (§171), market discount (§1276) and OID are
not computed.** For any bond held, the tax report says so in its list of
what it does not handle. For a tax-exempt bond bought at a premium,
amortization is mandatory and changes basis, so the gain on such a bond is
reported as an estimate, flagged (ADR 0011).

### 5. Import

A bond is added by hand (`pt instrument add --type bond --quote-basis …`),
which is the remedy the import already gives. The batch format gains an
optional `accrued_interest` column, and the broker-import skill asks for it
by interview like any other mapping field.

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

## Consequences

- **ADR 0040's engine** refuses any period in which a coupon bond is held,
  until this lands. Its §12 refusal stays in place until then.
- **Scenario 5** is rewritten under §1, with the bond's quote basis stated.
- **Existing hand-entered bond rows**, if there are any, may need reversing
  and re-entering: the ledger is append-only, and replay reads quantity
  under the convention in force.

## Alternatives considered

- **Quantity as a number of bonds.** This matches `face_value`'s current
  use, but every custodian export would need dividing on import, and a
  statement check would compare different units. Open question 2.
- **Computing the accrued interest at trade** from the schedule and a
  settlement lag. It is fragile across markets and holidays, and the
  custodian's confirm states the figure. Computed only as a check.
- **Treating purchased accrued interest as basis.** This is the trap
  `CLAUDE.md` names. Rejected.
