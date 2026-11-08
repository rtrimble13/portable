# ADR 0040 — Time-weighted return: daily sub-periods from stored snapshots, Modified Dietz across gaps

- **Status:** Accepted (owner, 2026-11-08), with the recommended answers to questions 1, 3 and 4. To question 2 the owner holds individual coupon bonds. Accrued interest (§13.1) turned out to need a bond model the code lacks, proposed in [ADR 0041](0041-bonds-at-face-with-accrued-interest.md); until it lands, §12's bond refusal stands
- **Date:** 2026-11-08; accepted 2026-11-08
- **Milestone:** v0.2, issue 2 (P0)
- **Governs:** `PORT-GIPS-A03`, `A06`, `B01`–`B07`, `D01`, `D04`, `D05`; `CLAUDE.md` "TWR vs. MWR", "External cash flow classification is level-dependent", "Do not annualize sub-one-year returns"
- **Depends on:** [ADR 0005](0005-decimal-representation-and-rounding.md) (rates are not rounded until the formatter), [ADR 0007](0007-cash-flow-classification.md) (one level-aware classifier), [ADR 0024](0024-valuation-snapshots-are-valued-state.md) (stored snapshots, checked), [ADR 0028](0028-instrument-with-no-market-price.md) (unpriced instruments at zero), [ADR 0031](0031-price-acceptance-against-statements.md) (unresolved statement lines block returns)

## Context

`pert` is a stub. The roadmap's first `pert` item is a daily time-weighted
return chain-linked from `valuation_snapshot`, with Modified Dietz as the
gap-day fallback and the method recorded on every result
(`docs/gips-standard.md` §6.B; issue 2).

What already exists:

- **`pt value` stores a snapshot per account per calendar day.** It holds
  ending market value (securities + cash − margin loan + accrued income), the
  day's external flow at account level and at portfolio level, a
  completeness flag, an estimates flag, and the level-5 value. Prices it
  consumed are recorded in `valuation_snapshot_price` and individual flows in
  `snapshot_flow`, each with its level, sign, `is_large` and `is_in_kind`.
  ADR 0024 keeps stored snapshots through a rebuild, removes them when a
  ledger write makes them stale, and `pt validate` re-derives them.
- **Flow classification** is one function, `services/cash_flow.classify(txn,
  level)`, tested against the `PORT-GIPS-B02` matrix. An internal transfer
  is external at account level and nets to zero at portfolio level.
- **`return_policy`** is effective-dated and holds the large-flow threshold.
  `repos.policies.in_force(day)` returns `None` when no policy is in force,
  and `PT-E-GIPS-NO-FLOW-POLICY` exists.
- **`fee_class`** is stored on every fee-bearing row (schema-enforced), with
  the five `PORT-GIPS-D01` buckets.
- **ADR 0031** already says `pert` refuses an account's returns for any
  period ending on or after an unresolved statement line
  (`price_acceptance.blocked_from`).

What reading the code turned up. None of it is wrong today, because nothing
computes a return. Each would produce a plausible wrong number the day
something did:

1. **Snapshots carry no accrued income.** `ValuationEngine.accrued_interest`
   and `accrued_dividends` exist and are never called. `Holding` never gets
   coupon dates, so accrued interest would be zero even if they were called.
   `accrued_interest`, `accrued_dividends` and `accrued_income` are always
   `0.00`. For a coupon bond that breaks `PORT-GIPS-A06`, a requirement: the
   return series saw-tooths between coupons.
2. **`income_amount` and `fees_amount` are always `0.00`.** The columns exist
   and nothing fills them. A zero that means "not computed" is the
   blank-equals-zero trap.
3. **Margin interest is stored with `fee_class = internal_mgmt_cost`**
   (`pt cash margin-interest`). An engine that read the class alone would add
   margin interest back at gross-of-fees and net-of-external-costs-only.
   `PORT-GIPS-D01` says margin interest reduces return at every basis.
4. **A snapshot built with no policy in force records `is_large = 0`** for
   every flow. "Not large" and "no threshold defined" look the same.
5. **Snapshots go stale from a row's trade date** (`_remove_stale_snapshots`).
   A dividend accrued from its ex-date would be in snapshots dated before its
   ledger row, and entering the row would not remove them.
6. **There is no CLI plumbing for `pert`.** `portable_pt/state.py` is `pt`'s
   own, and no CLI imports another. What `pert` needs has to live in
   `portable_core/cli`.

## Decision

### 1. Where it lives

The arithmetic and the rules go in `portable_core/services/returns.py`. The
result types are frozen dataclasses in `portable_core/domain/returns.py`, with
no I/O. `pert` is a thin Typer CLI over them. Shared CLI setup, such as the
portfolio path and global options, moves from `portable_pt/state.py` into
`portable_core/cli` where `pert` needs it. Pure Python only. If a C++ path
comes later, it keeps this as its reference (`CLAUDE.md`, C++).

### 2. Input: stored snapshots, checked, never built by `pert`

`pert` reads `valuation_snapshot` and `snapshot_flow` for the range. It does
not price anything and it writes nothing.

- **A missing snapshot is refused** (`PT-E-SNAPSHOT-MISSING`). This covers any
  day an account needs a snapshot for and has none. The remedy names the
  `pt value --from … --to …` that fills it. Nothing is interpolated
  (`PORT-GIPS-A03` test).
- **Every snapshot consumed is checked first** with ADR 0024's two checks,
  limited to the range. One that no longer holds is refused
  (`PT-E-SNAPSHOT-STALE`).

Why: the return is computed from exactly the values and prices that are on
record (`PORT-GIPS-J03`). Two runs over the same file give the same bytes.
`pert` also stays read-only. The cost is a replay walk over the range on every
run, the same walk `pt validate` makes. Open question 3 asks whether that is
acceptable.

An account needs a snapshot for every day from its **inception** to the end
of the period. Inception is the first day it holds capital at the start of
the day or receives an inflow. Before inception the account contributes
nothing; it is not a zero return.

### 3. One formula, with day-resolution flow timing

Each sub-period runs from the end of day *s* to the end of day *e*, so
*D = e − s* calendar days:

```
          V_E − V_B − Σ CF_i                       D − t_i
   r  =  ─────────────────────── ,       w_i  =  ─────────
          V_B + Σ (w_i × CF_i)                        D
```

- `V_B` is the stored ending value of day *s*, the previous usable
  valuation. `V_E` is the stored ending value of day *e*.
- `CF_i` is each `snapshot_flow` row of the level in *(s, e]*, signed:
  positive in, negative out. In-kind flows are included at their recorded
  value (Firms 2.A.29.c).
- `t_i` is the elapsed calendar days at which the flow is taken to occur. An
  inflow on day *d* counts **at the start of the day**, so `t = d − s − 1`.
  An outflow counts **at the end of the day**, so `t = d − s`.

This is Modified Dietz in calendar days, as the archived 2011 *Guidance
Statement on Calculation Methodology* writes it. Flow timing is resolved to
the day. With *D = 1* it is the exact daily return under that timing: an
inflow has weight 1 and an outflow has weight 0. One function serves both
cases. The method recorded is `daily` when *D = 1* and `modified_dietz` when
*D > 1* (§6).

**Why inflows at the start of the day and outflows at the end (open
question 1).** These are the two days a personal account actually has.

| Day | Values | Start in, end out | All at end of day | All at start of day |
|---|---|---|---|---|
| Funding | `V_B` 0, deposit 10,000, `V_E` 10,050 | (10,050 − 10,000) / 10,000 = **0.50%** | denominator 0: undefined | 0.50% |
| Full withdrawal | `V_B` 10,050, withdraw 10,080, `V_E` 0 | (0 − 10,050 + 10,080) / 10,050 = **0.30%** | 0.30% | denominator −30: meaningless |

Mid-day weights (½) avoid both failures. The cost is that *D = 1* stops being
an exact daily return.

### 4. Undefined days are refused, idle days are skipped

- **The denominator is zero and nothing moved** (`V_B` 0, no flow, `V_E` 0).
  The account held nothing that day. The day is skipped (it contributes no
  factor) and listed on the result as idle. This covers an account emptied
  and later re-funded.
- **The denominator is zero or negative and anything moved.** Examples are a
  short or margin position with negative equity, or an outflow larger than
  the value. This is refused (`PT-E-RETURN-UNDEFINED`), naming the day and
  the values. Nothing is clamped.

### 5. Levels

- **Account level.** That account's snapshots and its `level = 'account'`
  flows.
- **Portfolio level.** For each day, `V_B` and `V_E` are summed across every
  account past inception, with the `level = 'portfolio'` flows. A transfer
  between the owner's accounts is zero there. The `PORT-GIPS-B02` test holds
  it to that: adding a transfer leaves the portfolio return bit-identical and
  changes both account returns.

A portfolio day is usable only when every account past inception has a
usable snapshot that day.

### 6. Gaps and the Modified Dietz fallback (`PORT-GIPS-B06`)

A day is **usable** when its snapshot is complete. A day is not usable when
its snapshot is incomplete or when ADR 0031 blocks returns from a date on or
before it. Sub-periods run between consecutive usable days, and a sub-period
longer than one day is Modified Dietz by the same formula. The first and last
days of the period must be usable.

A multi-day sub-period is refused (`PT-E-RETURN-GAP`) when:

- **a large flow falls inside it.** "Inside" means any large flow before day
  *e*, or a large inflow on day *e* itself, which would need a value at the
  end of *e − 1*. The policy requires a valuation at that flow, and the
  valuation is missing (`PORT-GIPS-B03`, A03.c).
- **it spans a calendar month end.** Month-end valuation is a floor
  (`PORT-GIPS-A03`, a and b).

So the fallback bridges at most a few unpriced days within a month and no
large flow. A period for which only monthly prices exist works month by
month, provided each month-end is usable and no large flow falls mid-month.

### 7. The return policy is required for every day

For every day in the period, a `return_policy` must be in force
(`PT-E-GIPS-NO-FLOW-POLICY`, `PORT-GIPS-B03`), even though daily valuation
makes the threshold informational. A day with no policy cannot be told
apart from a day without a large flow (Context, item 4). The report states
that daily valuation satisfied the large-flow revaluation requirement, and
for which days. Where Modified Dietz was used, it states that no large flow
fell inside a bridged gap.

### 8. Return bases, from `fee_class` (`PORT-GIPS-D01`)

The actual market values give the return **net of all costs**. Each other
basis treats the fees it does not deduct as though they were never paid. A
fee is taken as an external outflow at the end of its day, weighted by the
same formula. On a daily sub-period that is exactly adding it back to `V_E`.

| Basis | Adds back | Presented |
|---|---|---|
| Net of all costs | nothing | supplemental, labelled so |
| **Net-of-fees** | `other_admin` | **first**: the required presentation for a total fund (AO 24.A.1.b) |
| Net-of-external-costs-only | the above + `internal_mgmt_cost` | alongside |
| Gross-of-fees | the above + `external_mgmt_fee` | alongside |

- **`transaction_cost` is never added back.** It is in the trade's cash and
  basis already, and every basis deducts it (`PORT-GIPS-D02`).
- **Margin interest is never added back**, whatever class its row carries.
  It is a financing cost at every basis (`PORT-GIPS-D01`, `D04`). The rule
  keys on `txn_type = margin_interest`, because the stored class says
  `internal_mgmt_cost` (Context, item 3). The ledger is append-only, so those
  rows stay as written, and the class on them is ignored with a reason in the
  code.
- **A cash row classed `embedded_fund_fee` is refused**
  (`PT-E-FEE-EMBEDDED-IN-CASH`). That fee is inside the fund's NAV and is
  never paid in cash. A cash row with that class is a double deduction
  (`PORT-GIPS-D05`).
- **The amount is the row's `fees + commissions`, and the class is the
  row's.** The same rule applies to trades and to fee rows. Rows come from
  the effective ledger of each day (reversals netted, ADR 0021) through one
  function, `fee_addbacks(rows, basis)`, never re-derived at a call site.
- Where nothing in the period is an `external_mgmt_fee`, gross-of-fees and
  net-of-external-costs-only are equal. They are shown once, labelled with
  both names (`PORT-GIPS-D01`).

ADR 0014's advisory fee paid by one account for another works without a
special case. It is a transfer, neutral at portfolio level, plus an
`external_mgmt_fee` in the account paid for, which gross-of-fees adds back.

### 9. Linking, precision, tolerance (`PORT-GIPS-B04`)

`R = ∏(1 + r) − 1`, in `Decimal`, under a context fixed in `returns.py`
(precision 34, `ROUND_HALF_EVEN`) rather than the thread's. Nothing is
rounded until the formatter (ADR 0005). The property test's tolerance is
stated in the test, with no flows: linked equals direct to 1e-28 relative.

### 10. The result records its method (`PORT-GIPS-B05`), and is not stored

A `TimeWeightedReturn` carries the following:

- the level and account;
- the requested period, and the actual period with the inception date if
  inception moved the start;
- the cumulative return per basis;
- every sub-period with its dates, method, flows and large flows;
- counts by method, and the idle days;
- the facts the report must disclose: snapshots using estimates or stale
  prices, the level-5 share, holdings valued at zero for want of a market
  price (ADR 0028), margin interest deducted at every basis, reclaimable
  withholding not accrued (`PORT-GIPS-A06` 3, a recommendation), and the
  large-flow statement of §7.

**It is computed on demand, not stored.** The snapshots it derives from are
stored and checked, and determinism makes a re-run the comparison
`PORT-GIPS-J01` error detection needs. A `return_result` table would be one
more derived class to invalidate. On acceptance, the "Data model" lines of
`PORT-GIPS-B05` and `D01` in `docs/gips-standard.md` are amended to say the
method and basis are recorded on the result object, in the same commit.
Revisit when tearsheets publish numbers that must be held fixed.

### 11. Annualisation (`PORT-GIPS-B07`)

This engine reports cumulative returns only. The result carries its period
length, and the formatter refuses to render an annualised figure for a
period under one year (`PT-E-GIPS-ANNUALIZE-SUB-YEAR`). The guard lands
with the first rendered return. How a period of a year or more is
annualised, and how a 365-day period in a leap year counts, is decided with
multi-period reporting (issue 5).

### 12. Other refusals

- **A trade for cash in an instrument valued at zero for want of a market
  price** (ADR 0028), on any day in the period, is refused
  (`PT-E-RETURN-UNPRICED-TRADE`). The purchase would show as a loss of its
  whole cost on the day, and a sale as a gain. Receiving such an instrument
  in kind, or a cash payout on one (`merger_cash`), is not refused: the
  first moves nothing and the second is real return.
- **A coupon bond held in the period while snapshots cannot accrue its
  interest** is refused (`PT-E-ACCRUAL-UNAVAILABLE`), until the
  prerequisite in §13 is done.

### 13. Prerequisites, in `pt`, before the engine

These change `pt`'s snapshots, not `pert`. None needs a schema change.

1. **Accrued interest** (`PORT-GIPS-A06`, required). Derive each bond
   holding's last and next coupon dates from `maturity_date`,
   `coupon_frequency` and `first_coupon_date`, and include
   `accrued_interest` in every snapshot. Open question 2 asks whether to do
   this now or refuse coupon bonds for now.
2. **Accrued dividends on the ex-date** (`PORT-GIPS-A06`, recommended, not
   required). A dividend whose ex-date is on or before the snapshot date and
   whose pay-date is after it is a receivable in ending value. Its ledger row
   is dated the pay-date, so:
   - the snapshot reads declared rows dated after it from the ledger;
   - `_remove_stale_snapshots` removes snapshots from `min(trade_date,
     ex_date)`, so entering the row removes the snapshots that should have
     accrued it.
3. **`income_amount` and `fees_amount`** are filled with the day's income
   and fees, informational only, so that a stored zero means zero.

Existing stored snapshots stop re-deriving once 1–3 land. `pt validate`
names them, and `pt value` over the range rebuilds them. A `CHANGELOG` entry
says so.

### 14. The command

```
pert twr [--from DATE] --to DATE [--account NAME] [--daily]
         [--format table|json|csv|markdown] [--as-of DATE] [-v|-vv] [--quiet] [--no-color]
```

- **Level.** Portfolio by default; `--account` gives account level.
- **Dates.** `--to` defaults to `--as-of`, which defaults explicitly
  (invariant 6). `--from` defaults to inception.
- **`--daily`.** Emits the sub-period series. Every basis is always
  computed, and the table leads with net-of-fees.
- **JSON.** Validated against `schemas/twr-1.0.json`. The envelope carries
  `GIPS_DISCLAIMER` as a field (`CLAUDE.md`, Output). `Decimal`s are
  strings, and an absent figure is `null` with a reason.
- **What it is not.** It is TWR only. MWR (issue 4) is a separate command
  and is never shown without TWR beside it (`PORT-GIPS-B01`).

## Open questions for the owner

1. **Flow timing.** Inflows at the start of the day and outflows at the end
   (recommended, §3), all flows at the end of the day, or mid-day? It is the
   documented method on every result, and changing it later restates every
   return.
2. **Bonds.** Do any accounts hold individual coupon bonds, as opposed to
   bond funds?
   - If yes, accrued interest (§13.1) is done before the engine.
   - If no, the engine refuses coupon bonds (§12) and accrual waits.

   Dividend accrual on the ex-date (§13.2) is recommended either way.
3. **Stored and checked, or built on demand?** Recommended: `pert` reads
   stored snapshots and checks each one, refusing a missing day (§2). The
   alternative is for `pert` to build snapshots in memory from the prices on
   file: no `pt value` step, but nothing on record says which values a
   return used.
4. **Bases.** Net-of-fees first, with gross and net-of-external-costs-only
   beside it and net of all costs as supplemental (recommended, §8)? Or
   gross-of-fees first, the Firms convention?

## Owner's answers (2026-11-08)

1. **Flow timing:** inflows at the start of the day, outflows at the end, as
   recommended.
2. **Bonds:** the owner holds individual coupon bonds, so accrued interest
   goes into every snapshot (§13.1). Starting on it showed that the bond
   model is missing more than accrual:
   - the stored quote basis is never read;
   - a bond's quantity has no defined unit;
   - a bond trade cannot record the accrued interest it exchanges;
   - import cannot create a bond.

   [ADR 0041](0041-bonds-at-face-with-accrued-interest.md) proposes the
   model. Until it lands, the engine refuses any period holding a coupon
   bond (§12). Dividend accrual on the ex-date (§13.2) and the day's income
   and fees (§13.3) do not depend on it and go first.
3. **Snapshots:** stored and checked, as recommended. `pert` is read-only.
4. **Bases:** net-of-fees first, as recommended.

## Consequences

- **`pert twr` is the first command that computes a return.** Every refusal
  above is a place a plausible number would otherwise have been printed.
- **On the real portfolio it computes nothing until the history is priced**
  (ADR 0020, still proposed). It refuses missing and incomplete days rather
  than bridging them, so building it first is safe, and its refusals are a
  list of what the price import must cover.
- **The prerequisites change stored snapshots.** `pt validate` reports them
  until they are rebuilt.
- **Gross-of-fees exists only in the ledger's fee rows.** A fee billed to an
  account outside the portfolio never appears, so gross and net are equal
  for it. That is correct, and the disclosure block says which fees each
  basis deducts (`PORT-GIPS-I04`).

## Alternatives considered

- **Modified Dietz monthly as the primary method.** This is a weaker
  approximation where daily values exist, and `PORT-GIPS-B06` permits it
  only where daily returns are not calculated. Rejected.
- **Interpolating a missing or incomplete day.** `PORT-GIPS-A03`'s test says
  refuse. Rejected.
- **Bases by adjusting a hypothetical gross market value that compounds**
  (as if fees had never been paid, ever). This differs from the per-day
  add-back by the return on the fees not paid. The per-day add-back measures
  the actual portfolio with each fee undone in its own period, which is what
  the bases describe. Rejected.
- **Reading fees from `valuation_snapshot.fees_amount`.** It is a single
  number with no class, and is never filled today. It is filled
  informationally (§13.3), but the bases read the ledger by class.
- **A `return_result` table now.** See §10. Deferred, not rejected.
