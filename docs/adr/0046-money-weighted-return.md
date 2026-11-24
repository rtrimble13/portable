# ADR 0046 — Money-weighted return: one solve from the walk's flows, shown beside the time-weighted return, and refused unless its rate is provably the only one

- **Status:** Accepted (owner, 2026-11-24), with the recommended answer to each of its five questions. See Owner's answers. `PORT-GIPS-C04` is amended to §3's test. **Implemented**: `portable_core/services/money_weighted.py`, `pert mwr`, `schemas/mwr-1.0.json`. See As built, which corrects two of the ADR's own examples
- **Date:** 2026-11-24; accepted 2026-11-24
- **Milestone:** v0.2, issue 4 (P1)
- **Governs:** `PORT-GIPS-C01`–`C05`, `B07`; `CLAUDE.md` "TWR vs. MWR", "Do not annualize sub-one-year returns", invariants 6 and 9
- **Depends on:** [ADR 0007](0007-cash-flow-classification.md) (one level-aware classifier), [ADR 0015](0015-in-kind-transfers-and-opening-positions.md) (in-kind transfers at their recorded value), [ADR 0040](0040-time-weighted-return.md) (§3 flow timing, §8 bases), [ADR 0044](0044-estimated-returns-over-unpriced-days.md) (§3, an MWR on an estimated value), [ADR 0045](0045-multi-period-returns.md) (§1 the walk, §4 track records, §5 the year count)
- **Relates:** issue 58 (reconcile to the custodian), issue 6 (benchmarks)

## Context

### What it is for

The time-weighted return measures the investments. It removes the timing
of the owner's deposits and withdrawals, which is what lets it stand beside
an index. The money-weighted return is the owner's own experience: the one
rate at which every dollar put in, compounded to the end, comes to what was
taken out plus what is left. The two answer different questions, and
neither is more correct. A custodian's "personal rate of return" is often
money-weighted. If the owner's is, setting it beside `pert twr` in issue 58
would show a break that is really a difference of method.

### What GIPS says

- **`PORT-GIPS-C01`, Firms 1.A.35.** An MWR may be presented only
  alongside a TWR, never as the headline or the only return. The
  owner controls his flows, but an open-ended liquid portfolio fails the
  second limb. `CLAUDE.md` says the same: never present MWR alone.
- **`PORT-GIPS-C02`, Firms 2.A.29, AO 22.A.23.** The MWR is
  since-inception and annualised, from daily external cash flows, with
  stock distributions as flows valued when distributed.
- **`PORT-GIPS-C03`.** A track record under a year shows the
  non-annualised MWR. This is the one place where the natural
  implementation, an IRR, returns an annualised rate by construction.
- **`PORT-GIPS-C05`, Firms 2.A.39, AO 22.A.29.** A portfolio's MWR is solved
  once from the accounts' combined flows and values. It is never an
  average of the accounts' rates.
- **`PORT-GIPS-C04` is `portable`'s own.** GIPS says nothing about solvers.
  C04 asks for Newton with a bisection fallback, a documented bracket, an
  iteration cap, and a refusal "on non-convergence or multiple sign changes
  in the flow series".

### What exists

Everything the MWR needs is already read and checked by the walk
(ADR 0045 §1), for any window:

- **The level's external flows, one by one, on their days.** A transfer
  between the owner's accounts is no flow at portfolio level, and the
  accounts' flows are already combined, so C05 is met by construction.
  They are stored on every snapshot, complete or not (`ValuationSnapshot.flows`).
- **The level's value at the end of each day**, and whether it is usable.
- **The fees each basis undoes**, by day (ADR 0040 §8).
- **The track records** (ADR 0045 §4).

An in-kind transfer is an external flow at its recorded value (ADR 0015,
`cash_flow.classify`), so C02's stock distribution is already one. No
`.port` change is needed.

### The flaw in C04

A change of sign in the flow series does not mean there is more than one
rate. Descartes' rule of signs makes the number of changes an upper bound
on the number of rates, not a count. A personal portfolio changes sign
every time a withdrawal is followed by a later deposit. So the rule as
written refuses nearly every real portfolio, and almost always wrongly.

Three flow series show the difference. Amounts are from the investor's side:
paid in is negative, taken out (and the ending value) positive. Each rate
was solved by the year count of §1.

| Series | Flows | Sign changes | C04 as written | Running total of the flows | The balance at the solved rate (§3) |
|:---|:---|---:|:---|:---|:---|
| **Typical** | −100,000 on 2020-01-01, +20,000 on 2022-06-30, −30,000 on 2024-03-15, +180,000 at 2025-12-31 | 3 | refused | −100k, −80k, −110k, +70k: changes sign once | 100,000; 105,342; 152,983; 0. Never negative: the only rate is **9.4829%** |
| **Drawdown** | −100,000 on 2016-01-01, +130,000 on 2020-01-02, −60,000 on 2022-01-03, +110,000 at 2025-12-31 | 3 | refused | −100k, +30k, −30k, +80k: changes sign three times | 100,000; 13,812; 76,565; 0. Never negative: the only rate is **9.5019%** |
| **Three rates** | −1 on 2021-01-01, +6, −11 and +6 a year apart | 3 | refused | −1, +5, −6, 0: changes sign twice, then ends at zero | 1, then −5, −4 or −3 after the 6 taken out, at 0%, 100% or 200%. All three are rates, and the balance goes negative at each |

## Decision

### 1. What is solved

The annualised rate *R* at which the level's flows balance. It runs from the
end of the window's anchor, the day before its first day, to the end of its
last day:

```
                         n
    0  =   V_B   +      Σ   a_i · (1 + R)^(−T_i)   −   V_E · (1 + R)^(−T_E)
                        i=1
```

- **`V_B` is the value at the anchor.** Since inception it is zero, and the
  first deposit is the first flow. For a period, `V_B` is the value the
  period starts with.
- **`a_i` is each external flow at the level, signed** as the time-weighted
  return signs it: positive in, negative out. At bases that undo a fee,
  each undone fee is a further outflow at the end of its day, exactly as in
  ADR 0040 §8. So the MWR has the same four bases as the TWR, with
  net-of-fees first.
- **`T_i` is in years, counted by anniversary** (ADR 0045 §5's `years`).
  It runs from the anchor to the moment the flow counts from, under ADR
  0040 §3's timing:
  - an inflow on day *d* counts from the start of *d*, that is, the end
    of *d − 1*;
  - an outflow counts from the end of *d*;
  - so the TWR and the MWR agree on when money moved.
- **With no flow inside a window of a year or more**, the solve gives
  `(V_E / V_B)^(1/T_E) − 1`. That is the annualised TWR of ADR 0045 §5,
  and a test holds the two equal.

### 2. The solver (`PORT-GIPS-C04`)

- **It solves for g = ln(1 + R)**, the continuously compounded rate. So every
  power is `exp(−g·T)`. `Decimal.exp` and `Decimal.ln` are correctly
  rounded and a non-integral `**` is not (ADR 0045 §5), which keeps the
  bytes fixed (invariant 6). All of it is in `RETURN_CONTEXT`.
- **The bracket is stated, and scaled to the window.** g·T_E runs from −40 to
  40, which is a growth over the window between e^−40 and e^40. If the
  present value of the flows has the same sign at both ends, there is no
  rate in the bracket, or there is more than one. The return is refused
  with `PT-E-MWR-NO-CONVERGENCE`, naming the bracket.
  - **The textbook two-rate series is refused here.** −100, +230, −132 a
    year apart solves at 10% and at 20%. It ends with money in, as no
    portfolio's window can.
  - **A portfolio's window always has a rate.** It begins with money in
    and ends with its value taken out. As the rate rises without bound, the
    first amount dominates and its present value turns positive. As it
    falls, the last amount dominates and the value turns negative. So its
    rates, counted with multiplicity, are odd in number: one, or three or
    more, which are §3's to catch. A window whose rate the bracket does not hold is refused, never
    guessed. No real portfolio grows by e^40 over its window.
- **Newton, guarded by bisection.** A Newton step is taken only if it lands
  inside the current bracket. Otherwise the bracket is halved. It stops when
  the bracket is narrower than 10^−30 in *g*, or the present value is under
  10^−28 of the flows' gross total. After 200 steps it stops and refuses
  with `PT-E-MWR-NO-CONVERGENCE`.
- **The result records its solve:** the Newton and bisection steps taken,
  the bracket, and the residual as a share of the gross flows.

### 3. The rate must be provably the only one, or it is refused

The rule replaces C04's sign-change trigger.

**The test.** Compound the flows forward at the solved rate. The balance after
each flow is what the money put in would have grown to, at that rate, less
what was taken out. If that balance is never negative before the end, the
rate is the only one.

This is a proof, not a heuristic. It holds whenever the first flow, or
`V_B`, is money in, which is how every record begins. Suppose the balances
are never negative at the solved rate *r\**. Then, by induction over the
flows:
- at any rate above *r\**, every balance is larger;
- at any rate below *r\**, every balance is smaller;
- in both cases, strictly so once the first flow has compounded.

So the final balance, which is zero only at a solution, is positive above
*r\** and negative below it. No other rate solves. The argument holds for
any day-count, since it uses only that `(1 + r)^t` rises with *r*. This is
Teichroew, Robichek and Montalbano's "pure investment" (1965).

**A window that does not begin with money in**, a negative `V_B` on a
margined account, cannot meet the proof's condition, and is refused with
`PT-E-MWR-NOT-UNIQUE`. The TWR refuses such a window as undefined in any
case (ADR 0040 §4).

**When the balance goes negative,** the return is refused with
`PT-E-MWR-NOT-UNIQUE`, naming the flow after which it does. That is the
withdrawal that took out more than the money had grown to at the rate.
The refusal does not say a second rate exists. It says the one found cannot
be shown to be the only one.

**For a portfolio**, the test's balance is a hypothetical account grown at
the rate. It goes negative only when a withdrawal exceeds what steady
growth at the money-weighted rate would have built. Then the timing of
gains and withdrawals is doing most of the work, and a single rate
misdescribes it. In the table above it passes the typical and drawdown
series and refuses the three-rate one.

`PORT-GIPS-C04` is amended on acceptance to say this. Its test,
`test_xirr_refuses_pathological_flows`, stands with the two-rate series.

### 4. Always beside the time-weighted return (`PORT-GIPS-C01`)

- **`pert mwr` computes the TWR over the same window first**, from the same
  walk, and shows both. If the TWR is refused, the MWR is refused with the
  TWR's refusal: shown alone, it would be the headline.
- **The window is the TWR's.** A start or end the TWR moved past days with
  nothing held moves the MWR's too, so the two cover the same days. A window
  across a break is refused (ADR 0045 §4). Without `--from`, it runs from
  the start of the current record.
- **Under `--estimate`**, both may be estimated (ADR 0044).
  - The TWR is estimated if any sub-period it links is.
  - The MWR is estimated if `V_B` or `V_E` is, and only then. It reads no
    other value, as ADR 0044 §3 anticipated.
  - Each is marked on its own.
- **Firms 2.A.28 and AO 22.A.22** (in `PORT-GIPS-C02`) ask for an MWR's
  portfolio to be valued at least annually and at the period's end. The
  TWR beside it already requires every month end.

### 5. Annualised from a year, and never under one (`PORT-GIPS-C02`, `C03`, `B07`)

- **A window of a year or more, by anniversary** (`at_least_a_year`): the MWR
  is the annualised rate *R*. Since inception, it is *the* MWR.
- **A window under a year:** the MWR is the non-annualised `(1 + R)^T_E − 1`,
  labelled so. The `ReturnValue` carries `method="mwr"` and the period, so
  the formatter's sub-year guard applies to it as to every return (ADR 0045
  §5).
- **A window that does not start at the record's inception** is a period MWR,
  labelled as one. It is never presented as *the* MWR.
- **The TWR beside it is shown both ways.** It is cumulative, and also
  annualised from a year, so the two rates can be compared directly.

### 6. The command, and where it lives

- **The command:** `pert mwr [--from] [--to] [--account] [--estimate]`. Its
  JSON is `mwr-1.0`, with the §9.3 disclaimer as an envelope field. It
  carries, per basis:
  - the MWR, and the TWR beside it;
  - the flows used: their count and total;
  - the solve (§2) and the test (§3): its lowest balance and that
    balance's date.
- **The table** shows the two rates per basis, net-of-fees first, each
  labelled with its method, basis and period (`PORT-GIPS-H04`). A footnote
  says what each measures.
- **The code:**
  - `services/money_weighted.py`, over a `Walk` and its `window`;
  - `MoneyWeightedReturn` in `domain/returns.py`;
  - two error codes: `PT-E-MWR-NO-CONVERGENCE` and `PT-E-MWR-NOT-UNIQUE`;
  - nothing in `pt`, and no `.port` change.

## Test obligations

- **`test_si_mwr_uses_daily_flows`** (C02): two series differing only in
  the day of a flow within a month give different rates.
- **`test_stock_distribution_is_a_flow`** (C02.c): an in-kind transfer out
  is an outflow at its recorded value.
- **`test_si_mwr_under_one_year_not_annualized`** (C03): under a year, the
  figure is non-annualised, and the formatter refuses an annualised one.
- **`test_xirr_refuses_pathological_flows`** (C04): the three-rate series
  is refused with `PT-E-MWR-NOT-UNIQUE`, naming the withdrawal after which
  the balance turns negative. The two-rate series is refused at the
  bracket, with `PT-E-MWR-NO-CONVERGENCE`.
- **Interleaved flows are not refused.** The typical and drawdown series
  of the table give 9.4829% and 9.5019%, checked against a solve done
  outside `portable`.
- **`test_portfolio_mwr_is_aggregated_not_averaged`** (C05): two accounts
  whose value-weighted average rate differs from the combined solve.
- **`test_mwr_never_presented_alone`** (C01): every output carries the TWR.
  A window the TWR refuses is refused for the MWR, with the TWR's
  refusal.
- **With no flow over a year or more, MWR equals annualised TWR**, to the
  last digits.
- **Under `--estimate`**, an MWR whose `V_E` was estimated is marked, and
  one whose ends were measured is not, even when its TWR is estimated.
- **The same file and command give the same bytes** (invariant 6), and the
  solve is recorded.
- **A property test** generates flow series from a positive account path.
  Each gets a rate that zeroes the present value and passes §3, or a
  refusal, and none gets a rate that fails §3 (as built, below).
- **The rehearsal.** Portfolio D's reference adds an MWR solved from its own
  ledger, by bisection alone, and is compared with `pert mwr`
  (`docs/reconciliation-rehearsal.md`).

## Open questions for the owner

1. **Accept §§1–6 as recommended?**
2. **The uniqueness test.** Which replaces C04's sign-change rule?
   - **(a)** C04 as written, which refuses the typical series.
   - **(b)** Norstrøm's rule: a running total of the flows that changes
     sign once proves one rate. It passes the typical series and refuses
     the drawdown one.
   - **(c)** The balance at the solved rate (§3, **recommended**), which
     passes both and refuses the three-rate series. It is also a proof, and it
     names the flow responsible.
3. **The year count.** Recommended: by anniversary, as ADR 0045 §5 annualises
   the TWR. With no flows, the MWR then equals the annualised TWR exactly.
   - **The alternative is Actual/365,** which a spreadsheet's `XIRR` uses,
     and perhaps the custodian.
   - **The difference is small but real:** at 10% a year for ten years with
     three leap days, Actual/365 reads 9.9914%, 0.86 bp lower.
   - **For issue 58,** know which the custodian uses before reading a
     difference of that size as a break.
4. **The bases.** Recommended: all four, by ADR 0040 §8's add-back, so
   each MWR has a TWR of the same basis beside it. The alternative is
   net-of-fees only.
5. **`pert periods`.** Recommended: no MWR column yet. `pert mwr` answers
   the question once per window, and C02 asks only for the
   since-inception figure. A column would put a period MWR on every row,
   and C02 asks that those be kept from reading as *the* MWR.

## Owner's answers (2026-11-24)

1. **§§1–6:** accepted.
2. **The uniqueness test:** (c), the balance at the solved rate, which
   replaces `PORT-GIPS-C04`'s refusal on more than one sign change.
3. **The year count:** by anniversary, as ADR 0045 §5 annualises the TWR.
4. **The bases:** all four, by ADR 0040 §8's add-back.
5. **`pert periods`:** no MWR column yet.

## As built

- **The example of more than one rate was wrong as first written.** The
  table showed the textbook two-rate series, −100, +230, −132, as refused by
  §3. That series ends with money in, which no portfolio's window does. Its
  present value has the same sign at both ends of the bracket, so §2
  refuses it first, as no rate or more than one. Writing the test found
  this.
  - **§3's example is now a three-rate series shaped like a portfolio**, and
    the two-rate series is §2's example.
  - **The decision is unchanged**, and so are the owner's answers.
  - **`PORT-GIPS-C04`'s test names both series.**
- **The property test was mis-stated too.** The obligation said every series
  from a positive account path passes §3. It does not. Big early gains
  withdrawn before later losses can leave the balance at the overall rate
  negative, and refusing that is §3 working. As built, the property is:
  - every generated series gets a rate that zeroes the present value with
    no negative balance, or a refusal, and never a rate that fails §3;
  - a series of deposits alone is always measured;
  - a measured rate is the only change of sign of the present value across
    the bracket, on a grid.

  About one series in forty is refused, so both outcomes are exercised
  (`tests/property/test_mwr_single_rate.py`).
- **"Since inception" is read off the beginning value.** A window starts
  where a track record does when the level held nothing at the end of the
  day before (ADR 0045 §4), so its beginning value is zero. The walk's
  records cannot say this: a walk that starts at the window sees a record
  begin there.

## Consequences

- A new command, `pert mwr`, and a new schema, `mwr-1.0`. `twr-2.0` and
  `periods-1.0` are unchanged.
- Two error codes, `PT-E-MWR-NO-CONVERGENCE` and `PT-E-MWR-NOT-UNIQUE`.
- `PORT-GIPS-C04` in `docs/gips-standard.md` is amended to the test of §3.
  That is a change to a requirement's obligation, so it carries this ADR
  and a `CHANGELOG.md` entry (`docs/gips-standard.md` §13).
- No `.port` change, no migration, nothing in `pt`.
- **Wherever the TWR is refused, the MWR is too**, until the history is
  priced (issue 58) or estimated. This is C01's price: the MWR needs only its
  two ends, but it may not be shown without the TWR, and the TWR needs every
  month end.

## Alternatives considered

- **Refuse on more than one sign change** (C04 as written). It refuses
  nearly every real portfolio, and almost always wrongly. Rejected.
- **Norstrøm's running-total rule.** It is a proof too, but it refuses a
  portfolio that once withdrew more than it had been paid. Kept as
  question 2's option (b).
- **Scan the bracket for a second root and refuse only if one is found.**
  Finding no second root on a grid proves nothing: two close roots can sit
  between the grid points. Rejected.
- **Average the accounts' rates for the portfolio.** C05 forbids it, and it
  is a different number. Rejected.
- **A floating-point solver, or a library such as `scipy`.** It breaks
  invariant 1, and the bytes would depend on the platform. Rejected.
- **Solve for R directly.** It needs a lower bound of −100%, and the
  bracket for a sub-year window spans many orders of magnitude. Solving for
  *g* makes the bracket symmetric and scaled to the window. Rejected.
- **Show the MWR alone where the TWR is refused,** since the MWR needs only
  its two ends. C01 forbids it, and `CLAUDE.md` names it. Rejected.
