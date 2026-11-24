# Reconciliation rehearsal

Issue 58 reconciles `pert` to the custodian's reported performance on the
owner's portfolio. That is the control that matters, and it needs the owner's
file and statements. This rehearses the same comparison on sample portfolios
of real securities, where the right answer can be worked out from outside
`portable`. It checks that the engine is right in general, not that it fits
one portfolio.

`scripts/rehearse_reconciliation.py` runs it. It needs `duk` reaching the
fafnir warehouse, so CI cannot run it.

```bash
python scripts/rehearse_reconciliation.py --work <a directory outside the repository>
```

It exits 1 when any period differs from its reference by more than 0.01 bp,
when any period is refused, or when the estimate checks below fail.

## What it builds

Four portfolios, from 2015-12-31, built through `pt` as the owner would: the
static one, D, E and E's priced twin. Prices
come through `portable`'s own fafnir provider (`pt price load`). Splits and
dividends come through `pt ca sync`, are recorded with `pt ca split` and
`pt income dividend`, and are paid on the ex-date, since fafnir has no pay
dates. Everything is valued daily with `pt value`.

| Account | Holds | Dividends | Reference |
|:---|:---|:---|:---|
| A | SPY | Reinvested at the ex-dividend reference price | SPY's dividend-adjusted series |
| B | NVDA (split 4:1 in 2021 and 10:1 in 2024), AGG (monthly income), VFIAX (a mutual fund) | Reinvested likewise | The three adjusted series, held from inception |
| C | SPY | Taken in cash | Raw closes plus the dividends received: ending over starting value, there being no flow |
| Portfolio | A + B + C | | The sum |
| D, a second portfolio | SPY | Reinvested as A | A day-by-day computation from its own ledger, at every basis |

D's ledger also holds:
- a deposit of 100,250.00 on 2020-03-23, of which 100,000.00 was invested at that day's close;
- a sale of 500 units on 2022-01-03, with the proceeds withdrawn the same day;
- a 250.00 custody fee on 2023-06-30.

Its reference applies ADR 0040's flow timing independently: an inflow counts
from the start of its day and an outflow from its end. The fee is added back
at the bases that do not deduct it.

**The money-weighted return (ADR 0046)** of every level is set beside a
reference solved by plain bisection, with years counted by anniversary in
the script's own code, so nothing of `portable`'s solver or year count is
shared.
- A, B and C have no flow after their funding at inception, so their
  reference is their growth annualized.
- D's is solved from its ledger, as ADR 0040 §3 and §8 time it. The deposit
  counts from the start of its day and the withdrawal from its end. At the
  bases that add the custody fee back, the fee is an outflow at the end of
  its day.

**Portfolio E rehearses the estimate tier (ADR 0044).** Its accounts are:
- E1, which holds 49 SPY and 370 AGG and takes dividends in cash;
- E2, which holds 215 VFIAX and 30 NVDA and reinvests.

SPY's and VFIAX's prices are withheld from 2024-12-10 to 2025-01-10, so
the month end 2024-12-31 cannot be measured. Both go ex-dividend inside
the stretch: SPY on 12-20, paid in cash, and VFIAX on 12-23, reinvested. A
twin has the same ledger and every price. The checks:

- **Without `--estimate`.** Every period that does not reach 2024-12-31
  equals the twin's, value for value. Every period that does is refused.
- **With `--estimate 0.5`.** The periods that were measured are unchanged,
  with no estimate. Every refused period is estimated, on 2024-12-31 alone.
- **Each carried price is the one the rules give**, worked out here from
  `duk`. SPY's is its 2024-12-09 close, less its 12-20 dividend per unit
  held. VFIAX's is the price its reinvestment paid on 12-23: a purchase at
  the fund's price, newer than any close before the stretch.
- **Each period's return equals the reference.** The reference is the
  values computed from `duk`, at those carried prices on 2024-12-31 and at
  real ones on every other day.

**Why A must match the adjusted series exactly.** fafnir adjusts for a
dividend by the factor (P_prev − D) / P_prev, so its ex-day return is
P_ex / (P_prev − D) − 1. That is the return of reinvesting the dividend at
P_prev − D, which is what A does. Receiving the dividend in cash on the
ex-date instead gives (P_ex + D) / P_prev − 1, which was 2 bp lower on SPY's
2016-03-18 ex-date.

**Why the reference reads `duk` itself.** `portable`'s fafnir path is part of
what is under test. A reference read through the same code would check it
against itself.

## What it found (through 2025-12-31)

**The engine passed.** 152 period returns compared, at five levels, from 242
corporate actions. The largest difference was 0.0004 bp. No period was
refused, and with ten years of history every row of `pert periods` was
measured, the 10-year one included.

| Level | Basis | Since inception | 10 years | 5 years | 3 years | 1 year | Month to date | Largest difference (bp) |
|:---|:---|---:|---:|---:|---:|---:|---:|---:|
| Static portfolio | net_of_fees | 3424.9391% | 3424.9391% | 812.7062% | 742.9595% | 36.7519% | 4.9151% | 0.0002 |
| A | net_of_fees | 294.6604% | 294.6604% | 95.3893% | 85.4976% | 17.7191% | 0.0798% | 0.0002 |
| B | net_of_fees | 7684.6617% | 7684.6617% | 1116.9505% | 1010.8571% | 38.2933% | 5.2507% | 0.0004 |
| C | net_of_fees | 263.3148% | 263.3148% | 85.3430% | 76.2624% | 16.1741% | 0.0707% | 0.0000 |
| D | net_of_fees | 297.2830% | 297.2830% | 95.2752% | 85.3868% | 17.7191% | 0.0798% | 0.0002 |
| D | gross_of_fees | 297.4863% | 297.4863% | 95.3752% | 85.4817% | 17.7191% | 0.0798% | 0.0002 |

The differences that should be there are there, each the size it should be:

- **Reinvesting against taking cash.** A − C is 31 percentage points over ten
  years, the cost of SPY's dividends left uninvested.
- **The deposit.** D is 0.86 points above A in 2020. Its deposit sat in cash
  through the 2020-03-23 fall before it was invested at the close, as ADR
  0040's timing requires.
- **The fee.** D's gross-of-fees return is above its net-of-fees from the
  custody fee on, and the two are equal before it. This is the basis split
  of `PORT-GIPS-D01`.

**The comparison bites.** Counting inflows at the end of their day instead of
the start, a single-line change to `returns.dietz`, fails the rehearsal:
- D's 2020 return moves 89 bp, and its ten years 297 bp;
- ten periods are refused, because the funding day's return becomes
  undefined, as ADR 0040 §3 predicts;
- A, B and C are untouched, having no flows.

### The money-weighted return (2026-11-24)

**It passed.** Every level's since-inception `pert mwr` matched its
reference to 0.0000 bp, D's at all four bases.

| Level | Money-weighted, annualized | Time-weighted, cumulative over ten years |
|:---|---:|---:|
| Static portfolio | 42.7818% | 3424.9391% |
| A | 14.7113% | 294.6604% |
| B | 54.5511% | 7684.6617% |
| C | 13.7661% | 263.3148% |
| D, net-of-fees | 16.9295% | 297.2830% |
| D, gross-of-fees | 16.9341% | 297.4863% |

- **A, B and C** are their growth annualized, since nothing moved after
  their funding. The portfolio's rate is not an average of theirs: B's
  growth dominates the combined value, as `PORT-GIPS-C05` expects.
- **D's money-weighted rate is above its time-weighted.** A's time-weighted
  return, the same holding with no flows, is 14.7% a year. D's 16.9% is the
  2020 deposit, invested at the close of 2020-03-23 near the low, earning
  more than the money that was already in.
- **Each rate was proven the only one** by ADR 0046 §3's test.

**The comparison bites.** Counting inflows from the end of their day in the
money-weighted solve alone fails every money-weighted line.
- D's rate moves 0.65 bp at every basis.
- The static levels move 0.40 to 1.84 bp, because their inception deposit,
  a day late, shortens the period by a day.
- The time-weighted lines are untouched.

### The estimate tier, on portfolio E (2026-11-24)

**It passed.** 57 periods were compared at three levels, the largest
difference 0.0001 bp.
- 33 periods that do not reach the withheld days equal the twin's, with and
  without the flag.
- 24 that do are refused without it. With it, all 24 are estimated on
  2024-12-31 alone, and the gaps either side are bridged.
- Every carried price, its date and its source were exactly what the rules
  give.
- The largest share carried was 45.43%, which is why the rehearsal uses a
  limit of 50%. The limit is not what it tests.

**What an estimate costs, against the priced twin.** SPY fell 3.1% from
its last loaded close to the year end. E1 carried 38% of its value at that
close.

| Level | Share carried | 2024, priced | 2024, estimated | 2025, priced | 2025, estimated | Since inception |
|:---|---:|---:|---:|---:|---:|:---|
| E1 | 38.34% | 8.9279% | 10.0929% | 9.9624% | 8.7988% | equal |
| E2 | 45.43% | 77.3526% | 77.9978% | 29.3799% | 28.9109% | equal |
| Portfolio | 43.99% | 57.4004% | 58.1971% | 25.4615% | 24.8296% | equal |

- **The error is about the share carried times the price's drift**, and
  falls on the periods that begin or end on the estimated day. E1's is
  38% × −3.1%, so 2024 is 116 bp too high and 2025 116 bp too low.
- **A period across the day is not moved.** With no external flow, the
  carried value cancels out of the linked return. A flow inside it would
  weight the carried value, and the error would not cancel.
- **At the owner's working limit of 5%**, the same drift would move a year
  by about 15 bp. That is why the footnote names the oldest price's age as
  well as the share: the limit bounds how much was carried, and only the
  age shows how far it could have drifted.

**The estimate comparison bites.** Remove the lowering of a carried price by
the distributions since it, a one-line change to `estimates._lowering`.
- SPY is then carried at its 12-09 close, 604.68, and its dividend is
  counted twice: in cash and in the price.
- E1's 2024 return moves 13.76 bp, and the portfolio's 4.01 bp.
- 16 periods fail the carried-price check.

## What it cannot test

- **Whether a carried price is a good estimate.** The rehearsal checks that
  `pert` applies ADR 0044's rules exactly. It cannot say whether a last
  price was a reasonable guess for a fund no one quoted. The table above
  shows how wrong one can be.

- **Bad vendor data.** The ledger and the reference both read fafnir's
  dividends, so a wrong or missing dividend in the warehouse is wrong in
  both. The rehearsal tests `portable`'s mechanics, not the data.
- **The owner's own file.** It cannot test the import mapping, the
  custodian's fee basis, flow timing and method, or instruments fafnir does
  not carry. Issue 58 tests those, which is why it stays open.

## Learned for issue 58 and for benchmarks

- **fafnir carries mutual funds.** VFIAX and VTSAX have daily NAVs, and
  VFIAX's dividends are in `duk ca`. This is a general answer to part of ADR
  0020's open question. Whether it carries the owner's own funds and
  delisted symbols is still issue 58's to find out.
- **fafnir's prices are unadjusted and its dividends as paid.** NVDA's closes
  drop tenfold on 2024-06-10, and its dividends read 0.16, then 0.04, then
  0.01 across the two splits. So `pt`'s unadjusted accounting gets exactly
  what it needs.
- **fafnir's dividends carry no pay dates.** Taken alone, they cannot place
  income between ex-date and pay-date. The owner's import takes pay dates
  from the custodian's rows.
- **fafnir's adjusted series assumes reinvestment at P_prev − D,** not at the
  ex-date's close or a custodian's pay-date price. If the benchmark ADR
  (issue 6) considers an ETF's adjusted series as a total-return benchmark,
  this is the convention it would inherit. ADR 0006 has fafnir declare no
  benchmark capability, and that stands until such an ADR says otherwise.
