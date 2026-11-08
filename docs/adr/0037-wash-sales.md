# ADR 0037 — Wash sales are detected in replay, across every account

- **Status:** Accepted (owner, 2026-11-08), with the recommended answer to each open question, and schema 11 approved. Implemented in full: schema 11, matching in replay (decisions 1–6), the refusal of a designation a split divided (answer 2, by the reversal route the owner chose on 2026-11-08), decision 7, declarations and candidates (decision 8), notices (decisions 5, 9) and the report (decisions 10, 11). Citations: the statute, the regulations and Rev. Rul. 2008-5 were read in primary text on 2026-11-08; Rev. Rul. 56-602 only as quoted by Tax Notes (see Consequences)
- **Date:** 2026-11-06; accepted 2026-11-08
- **Milestone:** v0.2, issue 27 (P0)
- **Governs:** `CLAUDE.md` "Wash sales cross accounts", invariants 3, 9; `docs/tax-methodology.md`
- **Amends:** [ADR 0011](0011-tax-estimation-boundary.md). Wash sales move from *refused* to *computed exactly*, for what this ADR detects
- **Depends on:** [ADR 0036](0036-a-lot-is-designated-by-its-lineage.md). A replacement lot is split, and lineage keys keep that from renumbering anything

## Context

`pt tax` says on its face that it does not account for wash sales, and
`lot_disposition.wash_sale_deferred` is always `NULL`. The statement is
honest, but the report is still wrong. A loss the IRS disallows is reported
as deductible, by an amount that looks entirely plausible, in a document the
owner files.

The rule is IRC §1091 and Treas. Reg. §1.1091-1:

- **When it applies.** A loss on a sale of stock or securities is disallowed
  when the taxpayer acquires substantially identical stock or securities
  within 30 days before or after the sale, by purchase or by a fully taxable
  exchange. A contract or option to acquire them counts too.
- **Effect on the replacement.** The disallowed loss is added to the basis of
  the replacement shares (§1091(d)). Their holding period includes that of
  the shares sold (§1223(3)).
- **Matching.** Replacement shares are matched in order of acquisition. When
  fewer are acquired than were sold, only that share of the loss is
  disallowed.
- **IRA replacements.** When the replacement is bought in an IRA or Roth IRA,
  the loss is disallowed and the IRA basis is **not** increased (Rev. Rul.
  2008-5). The loss is lost, not deferred.
- **Spouses.** IRS Publication 550 applies the rule to a spouse's purchase
  too.

Five things make it harder than it sounds:

- The window spans **every** account, sheltered ones included.
- "Substantially identical" is a judgement.
- Options count.
- A purchase in January can disallow a loss realized in December, changing a
  tax year already reported.
- A custodian adjusts the bases it reports for the wash sales it can see.
  That is one account and one security. `portable` takes some bases from the
  custodian (ADR 0017), so an adjustment can already be inside a number this
  ADR would adjust again.

## Decision

1. **Derived state, computed in replay.** Every pairing is made when the
   *later* of the two rows is applied. A loss sale looks back 30 days for
   acquisitions. An acquisition looks back 30 days for losses not yet
   matched. A rebuild and a live append therefore make the same pairings
   (ADR 0010, ADR 0016).
   - Days are calendar days, and dates are trade dates (invariant 7).
   - The window is the sale date ±30 days, inclusive.
2. **What is matched.**
   - **Loss sales:** a disposition at a loss **in a taxable account**. A loss
     in a sheltered account is not deductible to begin with.
   - **Replacements**, in **any** account of the portfolio:
     - a buy;
     - a reinvested dividend;
     - a capital-gain distribution taken in units;
     - stock acquired by exercising a call or by assignment of a written put
       (ADR 0038 derives both in replay).

     The law has no de minimis, so a 3-share dividend reinvestment disallows
     its share of a loss.
   - **Not replacements:**
     - lots derived by a corporate action;
     - the shares being sold;
     - in-kind and custodian-based lots, under decision 7.
   - **Instrument identity:** the same instrument is matched automatically,
     across symbol changes. Any other instrument is matched only once it is
     declared substantially identical (decision 8).
   - **Accounts:** every account in the file is taken to be the taxpayer's
     or their spouse's (answer 5). A file holding anyone else's account
     needs a declared account scope first, which is a later ADR.
3. **Matching order.** The unit is a disposition: one sold lot's part of a
   sale. Lots sold together can carry different losses per share and
   different holding periods. Only dispositions at a loss are matched.
   - Loss dispositions are matched in ledger order. Within one sale, the
     earliest-acquired sold lot goes first, and ties go by lot key. Treas.
     Reg. §1.1091-1(b) fixes this order: losses in the order the shares were
     disposed of, and, where the order of dispositions on one day cannot be
     determined, in the order they were acquired. The first draft said the
     regulation left it open; the primary text corrected that, and the
     implementation already followed it.
   - Replacement shares are matched in order of acquisition, earliest first,
     and ties go by lot key.
   - Each replacement share washes at most one sold share. The disallowed
     amount is the matched shares times that disposition's loss per share.
4. **Effect on the sale.**
   - `lot_disposition.wash_sale_deferred` records the disallowed amount.
   - `realized_gain` keeps `gain` as proceeds less basis, and gains a
     `wash_sale_disallowed` column.
   - The estimate is taken on the allowed amount, `gain +
     wash_sale_disallowed`.

   This is the shape of Form 8949, which reports the adjustment beside the
   gain under code W.
5. **Effect on the replacement.**
   - **Taxable account:** the disallowed loss is added to its basis by a
     `lot_basis_adjustment` row with reason `wash_sale`, naming the loss
     sale. Its holding period then absorbs the sold shares' period, counting
     no day twice:
     - **Bought after the sale:** its start moves earlier by the days the sold
       shares were held. The days between the sale and the purchase are not
       counted.
     - **Bought before the sale:** the two periods overlap. Its start becomes
       the earlier of its own acquisition date and the sold shares'
       holding-period start.
   - **Sheltered account:** no adjustment. The loss is recorded as
     permanently disallowed, and the buy that causes it warns, naming the
     loss and its amount. It is the outcome the owner most needs to see
     before it happens. Rev. Rul. 2008-5 names IRAs and Roth IRAs. Treating
     every `tax_deferred` and `tax_exempt` account the same way is this ADR's
     reading, and the warning says so.
6. **A partly matched replacement lot is split.**
   - **The split.** The matched shares become their own lot, one per
     disposition they wash. That lot carries its share of the original
     basis, plus the deferral and the absorbed holding period. The rest keeps
     the original key.
   - **The key** is `<replacement key>~<sale txn>.<sold lot key>`, for
     example `1234~5678.901`. It is unique by construction:
     - a replacement is always a lot that an opening row created, so its key
       contains no `~`;
     - matched shares never wash a second loss, so no split lot is split
       again by this rule;
     - the sale's transaction and the sold lot's key identify the
       disposition.
   - **Only derived lots split.** No ledger row changes.
   - **A designation that a later split divided is refused**
     (`PT-E-LOT-DESIGNATION-DIVIDED`, answer 2). Only a back-dated row can
     cause this. The owner chose the reversal route on 2026-11-08: the row is
     reversed and entered again against the lots as they now stand, so its
     meaning is recorded in the ledger. The append that causes it says so at
     once.
   - **The `wash_sale` table.** A new derived table records each pairing:
     the disposition, the replacement lot's key, the quantity, the amount
     disallowed, whether it went into basis, and the days absorbed. It goes
     into `clear_derived`, the digest, and the replay test.
7. **A basis the custodian stated may already carry the custodian's
   adjustment.**
   - **No automatic match.** A lot whose `basis_source` is not `derived`,
     and any `transfer_in` lot, is never matched automatically, for two
     reasons:
     - its basis may already include the custodian's adjustment;
     - the ledger does not say whether its shares were bought. A gift or an
       inheritance is not a replacement under §1091(a).

     When its acquisition date, not its transfer date, falls in a loss
     sale's window, it is listed as a candidate, and that year's report says
     so.
   - **Reconstructed and estimated blocks.** These are anchored to the
     custodian's present basis. So when one shares an account and an
     instrument with a replacement this ADR adjusts, the block may already
     contain the same deferral. The rebuild after migrating, and every
     `pt validate`, name the block, the replacement and the amount as a
     warning. `pt import reconstruct` cannot: it runs on the custodian's
     files before replay has paired anything.
     - Whether the custodian's figure included it is read from the
       custodian's lot detail.
     - A seeded row that included it is corrected by a reversing entry and a
       new one (invariant 2).
     - Nothing is netted automatically, because the tool cannot see which
       case it is.
   - **Reconciliation.** `pt reconcile --realized` labels a basis break that
     equals a deferral from another account as one the custodian could not
     see. It is still shown as a break.
8. **Substantially identical is declared, never inferred.**
   - **Declaring.** A new reference table, `instrument_equivalence`, records
     the pairs the owner declares with `pt instrument equivalent A B
     --reason`.
   - **Scope.** A declaration applies to every past and future pairing. One
     that changes a recorded figure is refused unless `--restate` is given,
     under ADR 0025's rule.
   - **Candidates.** `pt tax wash-candidates` lists, for each loss sale,
     what is in its window but not matched automatically, across accounts,
     so nothing is invisible:
     - every acquisition of a *different* instrument, whatever its asset
       class (answer 4);
     - every option on the same underlying, which the statute names. Options
       are candidates only in this version (answer 3);
     - every lot decision 7 holds back.

     Candidates are proposed, never applied. The list can be filtered by
     instrument type for reading, and is complete without the filter.
9. **A purchase that changes a past tax year says so.** A January buy that
   disallows a December loss restates that year's figures. That is the law,
   not an error. The buy's result warns with the year and the amount.
10. **The report says what it covers.**
    - `TAX_DISCLAIMER`'s blanket statement becomes a precise one: what is
      detected, and decision 11's list of what is not.
    - The tax summary reports two totals separately: what is disallowed, and
      what is permanently disallowed.
    - `tax-1.0` pins `excludes_wash_sales: true` as a const, so this is
      **`tax-2.0`**. It replaces that field with `wash_sales: {detected,
      not_detected}` and adds the adjustment to each disposition.
11. **Not detected in this version, and said so in every tax output:**
    - securities not declared substantially identical;
    - options and contracts to acquire (listed as candidates only);
    - in-kind and custodian-based lots (decision 7; listed as candidates);
    - short sales, and the purchases that close them (§1091(e));
    - acquisitions outside this portfolio, such as an account not in the
      file;
    - a replacement disposed of before the loss sale (answer 1). The
      pairing is reported for manual handling, and that year's report is
      marked incomplete.

## Owner's answers (2026-11-08)

The owner accepted the recommended answer to each question. They are
folded into the decisions above.

1. **A replacement sold before the loss sale:** (a). The pairing is
   reported for manual handling and the year is marked incomplete. Applying
   the deferral to the replacement's own sale can make that sale a loss
   that washes against the very shares the loss sale disposed of. That
   circular case is one the regulation does not resolve (decision 11).
2. **A designation naming a lot that a later split divided:** (a). It is
   refused. The two parts carry different bases and holding periods, so a
   fixed order would guess at which shares were meant (decision 6).
   - **How the meaning is recorded.** The recommendation said in
     `lot_designation`. That table's checks cannot hold such a row without
     another schema change, so on 2026-11-08 the owner chose the reversal
     route instead: the row is reversed and entered again. The meaning is
     then in the ledger itself.
3. **Options:** (a). Candidates only in this version. Matching long calls
   alone would be partial, and whether an option is substantially identical
   is a judgement (decision 8).
4. **Candidate scope:** (a). Every different-instrument acquisition, with a
   filter for reading. `instrument.asset_class` is optional free text, so
   scoping by it would hide every candidate whose class is blank
   (decision 8).
5. **Whose accounts are in the file:** this question carried no
   recommendation. Decision 2's assumption stands: every account is the
   taxpayer's or their spouse's. A file holding anyone else's account needs
   a declared scope before this lands, and that is a later ADR.

## Consequences

- **Schema 11** adds the `wash_sale` derived table,
  `realized_gain.wash_sale_disallowed` and `instrument_equivalence`. A
  rebuild fills the first two.
- **Gains change.** Realized gains change on the next rebuild wherever a wash
  sale exists. The rebuild after migrating says which years moved, and by
  how much. It also names any reconstructed block that decision 7 says to
  check.
- **Prerequisite met: the option lifecycle is replayable** (ADR 0038). Before
  it, `pt option assign` wrote its stock sale outside replay, and a rebuild
  erased it, so a loss on an assigned covered call would have been invisible
  to detection built in replay.
- **Tax-loss harvesting** in `po` (v0.3) can be built on decision 8's table.
- **Citations, read in primary text on 2026-11-08** (Cornell LII's copies of
  the US Code and the CFR). The verification standard of
  `docs/gips-standard.md` applies here too.
  - **§1091(a): confirmed.** "Acquired (by purchase or by an exchange on
    which the entire amount of gain or loss was recognized by law), or has
    entered into a contract or option so to acquire". "Stock or
    securities" includes contracts or options to acquire or sell them.
  - **§1091(d): confirmed, in an equivalent form.** The statute states the
    replacement's basis as the sold shares' basis adjusted by the
    difference between the purchase price and the sale price. Per share
    that equals the purchase price plus the disallowed loss, which is what
    decision 5 does. Treas. Reg. §1.1091-1(h), Example 2 is reproduced to
    the cent by a test.
  - **§1091(e): confirmed.** Short sales have their own rule, not detected
    here.
  - **Treas. Reg. §1.1091-1(b)–(f): confirmed.**
    - Losses are matched in order of disposition, and replacement shares
      in order of acquisition.
    - An acquisition that disallows one loss is disregarded for any other.
    - "Acquired" is defined as in the statute.
    - Decision 3's sentence about the order of sold lots is corrected
      above.
  - **§1223(3) and Treas. Reg. §1.1223-1(d): confirmed.** The replacement's
    holding period includes the period the sold shares were held. Neither
    text addresses overlapping periods. Decision 5's "no day counted twice",
    and converting the absorbed days into the calendar-year test, remain
    this ADR's reading.
  - **Rev. Rul. 2008-5, 2008-3 I.R.B.: confirmed** (read at www.irs.gov).
    - The holding: a loss on a sale followed by a purchase in the
      taxpayer's IRA or Roth IRA "is disallowed under § 1091", and the IRA's
      basis "is not increased by virtue of § 1091(d)".
    - It names IRAs and Roth IRAs only and draws "no inference" beyond
      them. Treating every `tax_deferred` and `tax_exempt` account the same
      way (decision 5) remains this ADR's reading, as the warning says.
  - **Rev. Rul. 56-602, 1956-2 C.B.: consistent, as quoted.** It is the
    basis of decision 2's rule that shares from one purchase are not each
    other's replacement.
    - The IRS does not publish the 1956 Cumulative Bulletin, and
      www.taxnotes.com, which carries the text, is outside the session's
      network policy. It was read only as quoted from Tax Notes in a search
      result.
    - As quoted: a taxpayer acquires 200 shares and, less than 30 days
      later, sells 100 at a loss. "The loss will not be disallowed unless
      during the period prescribed in section 1091(a) ... he acquires, or
      enters into a contract or option to acquire, additional shares".
    - The implementation matches that.
    - To be read in full before relying on the case the ruling does not
      address: every share of one purchase sold at a loss within 30 days
      of buying it. The implementation does not treat those shares as each
      other's replacement.

## Alternatives considered

- **A pass over all dispositions after replay.** Simpler to write, but it
  cannot follow a chain, where a washed replacement is itself sold at a loss
  and washed. It would also give the live path a different answer from a
  rebuild. Rejected.
- **Detect only when a sale is applied.** This misses every replacement
  bought after the sale, which is most of them. Rejected.
- **Record "substantially identical" decisions in `lot_basis_adjustment`**,
  as issue 27 suggested. That table is derived and a rebuild deletes it
  (ADR 0010). Rejected for a reference table.
- **Treat an IRA replacement as a deferral.** Wrong under Rev. Rul. 2008-5,
  and it would understate the loss of a real deduction. Rejected.
- **Infer substantially identical securities** from names, issuers or
  indexes. This is a judgement the tool cannot make, and a wrong guess
  silently disallows or allows a loss. Rejected.
- **Trust a custodian-stated basis as unadjusted** and match those lots like
  any other. This counts every deferral the custodian already applied a
  second time. Rejected.
