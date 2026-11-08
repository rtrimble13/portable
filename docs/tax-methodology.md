# Tax methodology

What `portable` computes exactly, what it estimates, and what it refuses.

**Nothing here is tax advice, and `portable` is not a substitute for a broker's
1099-B.** The design decision behind this document is
[ADR 0011](adr/0011-tax-estimation-boundary.md).

---

## The line

The single most important thing in this document is where the line falls
between an exact figure and an estimate. A tool that blurred it would be worse
than one with no tax features at all, because the numbers would look
authoritative.

| | Computed exactly | Estimated | Refused |
|---|---|---|---|
| Realized gain or loss | ✓ | | |
| Holding period | ✓ | | |
| Lot selection under a relief method | ✓ | | |
| Basis adjustments | ✓ | | |
| Tax liability | | ✓ | |
| Wash sales, for identical and declared instruments | ✓ | | |
| Wash sales through options, undeclared securities, short sales | | | ✗ listed, not applied |
| Bracket progressivity, carryforwards, AMT | | | ✗ |

**Exact** means every input is a ledger fact and the arithmetic is defined.
**Estimated** means gain × rate, and nothing more. **Refused** means `portable`
stops and says so rather than producing a number.

---

## Holding period

**Long-term requires *more than one year*, measured from the day after
acquisition.**

Three consequences, each of which has been got wrong somewhere:

- **Exactly one year is short-term.** Buy 2024-03-14, sell 2025-03-14: short.
  Sell 2025-03-15: long.
- **Leap years matter**, which is why `portable` counts calendar anniversaries
  rather than 365 days. Buy 2023-03-01, sell 2024-03-01 — that is 366 days and
  still short-term, because it is exactly one year.
- **29 February has no anniversary in a common year.** A lot acquired
  2024-02-29 turns long-term on 2025-03-01, not 2025-02-28. `portable` clamps
  the anniversary to the last day of the same month, which is the only
  convention that neither invents a date nor moves the boundary by a day.

There are tests on every one of these boundaries. They are not to be
"simplified".

**Short sales are always short-term**, however long the position is held.

---

## Relief methods

How a closing trade decides which lots it consumes. This determines the basis
relieved, the holding period, and therefore the rate — it is not a detail.

| Method | Consumes |
|---|---|
| **`spec`** (default) | Exactly the lots you designate with `--lots` |
| `fifo` | Oldest first |
| `lifo` | Newest first |
| `hifo` | Highest basis per unit first — realises the smallest gain |
| `lofo` | Lowest basis per unit first |
| `avg` | Average basis across all open lots; see below |

Set per account with `--relief-method`; override per trade with `--method`.

### Specific identification is refused when it is not specific

`spec` with no `--lots` designation **stops the command**. It does not fall
back to FIFO. Falling back would change the tax treatment of the trade without
telling anybody, and a designation that does not add up to the traded quantity
is refused for the same reason.

Run `pt lot list SYMBOL --as-of DATE` first: it shows which lots are already
long-term and what each carries, which is the table you need in order to choose.

### A designation names the lot, and nothing can re-point it

`--lots 12:100` takes the lot ids `pt lot list` shows, but the sale does not
record them. A lot id is a row number, and every rebuild renumbers lots: before
schema 10, a back-dated buy or a reversal of an earlier one quietly moved a
recorded sale onto a different lot, with a different basis and a different
gain. The sale now records each lot by its **lineage key**, which `pt lot list`
shows beside the id: the transaction that opened the lot (`1234`), or the
corporate action that derived it and the parent's key (`1500.1234`). No later
entry can change a key. You may designate by key directly, `--lots '@1234:100'`.

A sale recorded before schema 10 still says `12:100`. `pt migrate` works out
which lots those ids named when the sale was written, by replaying exactly the
rows that existed then, and compares that with how they read today. Where the
two agree, that is recorded. Where they differ, the sale had already drifted;
where the first cannot be worked out, it is unknown. Either way the sale is
refused until you decide it: `pt lot designation list --pending` shows both
readings and the gain each gives, and `pt lot designation resolve` records your
choice and its reason, once ([ADR 0036](adr/0036-a-lot-is-designated-by-its-lineage.md)).

### Average cost: two things are true at once

1. **Basis is averaged** across every open lot of the instrument, so each
   disposed share carries the same cost.
2. **The holding period is still determined lot by lot, FIFO.** Average cost
   averages the *basis*, not the *dates*.

A sale spanning old and new lots therefore splits between long-term and
short-term. Reporting it wholly as long-term because the average lot "looks
old" is a wrong tax rate, not a presentational choice.

**Average cost may not be mixed with specific identification for the same
instrument.** Once averaged, a share's individual basis is gone, so a later
spec-ID designation is designating something that no longer exists — and the
reverse leaves already-designated shares in an average that no longer describes
them. `portable` refuses (`PT-E-TAX-METHOD-CONFLICT`) rather than picking one,
because either choice silently restates the basis of shares already disposed of.

---

## Basis adjustments

Every change to a lot's basis writes a row explaining itself. That log is the
difference between a basis you can defend and one you can only assert; read it
with `pt lot show LOT_ID`.

### Splits

**Total basis is unchanged. Quantity and per-share basis move. The holding
period is not reset.**

That last clause is the trap. Resetting it turns a long-term gain into a
short-term one — a rate error that looks like nothing on the report. `portable`
records the holding-period start on the adjustment row as well as on the lot,
so the assertion is checkable after the fact rather than implicit.

### Spinoffs

**Basis is allocated by relative fair market value** — not by share count, and
not by the spinoff ratio. If the parent is worth $90 and the spun shares $10
immediately after, 90% of the original basis stays with the parent.

Both fair market values are required arguments and are recorded on the
adjustment. They come from the company's **Form 8937** or the post-spinoff
market prices. Without them `portable` refuses: an arbitrary allocation is
wrong on both sides and will not look wrong.

**The spun shares inherit the parent's holding period.** They are not newly
acquired, so a spinoff from a five-year-old lot is long-term on day one.

### Return of capital

**Reduces basis; it is not income.** Once basis reaches zero the excess becomes
capital gain, recognised immediately — there is no negative basis.

[ADR 0039](adr/0039-return-of-capital-reduces-basis-in-replay.md) sets out how
replay applies it:

- **Entitlement is fixed on the ex-date.** The shares in the account before
  that day are the ones whose basis is reduced. Shares bought on or after it
  are not.
- **The reduction is applied on the pay-date**, when the capital came back.
- **The total is allocated by quantity** across the entitled lots, by largest
  remainder, so the parts sum exactly to what was paid.
- **The excess is recorded as a capital gain** on the pay-date, at the lot's
  own holding period. It is a disposition of no units, with the excess as
  proceeds and no basis, so every report that shows a gain shows it. The
  holding period is never reset.
- **Shares disposed of between the ex-date and the pay-date are refused**
  (`PT-E-ROC-ENTITLEMENT`). They were entitled, but their basis is gone, and
  what their part reduces instead is not `portable`'s to decide.

Note the separate question this does *not* answer: for **performance** purposes
return of capital is also not an external cash flow. Both facts are true, they
are about different things, and conflating them gets both wrong.

### Short sales

[ADR 0038](adr/0038-short-sales-and-the-option-lifecycle.md).

- **Cash.** A short sale **receives** its gross less costs, and a cover
  **pays** its gross plus costs.
- **The lot.** A short lot holds what the sale received.
- **The disposition.** At the cover, that credit is the **proceeds** and what
  the cover paid is the **cost basis**, which is Form 8949's shape for a short
  sale. The gain is short-term whatever the dates.
- **Value.** A short position is valued, and shown in `pt holdings`, as a
  negative quantity and a negative market value. Its basis is shown negative
  beside it, so value less basis is the unrealized P&L in either direction.

A closing trade relieves lots of its own direction only: a cover never
consumes a long lot, and a sale never a short one. A short sale in an account
holding the instrument long is refused, because it is a constructive sale
(IRC §1259) that `portable` does not record. A buy against a short position is
refused as well: it is a cover, made with `pt cover`.

### Option premium

The rule depends on how the option resolves, and getting it wrong both
double-counts the premium and applies the wrong rate.

| Event | Where the premium goes | Cash at the event |
|---|---|---|
| Long call **exercised** | Into the acquired stock's **basis** | the strike plus costs |
| Written call **assigned** | Into the stock sale's **proceeds** | the strike less costs |
| Written put **assigned** | Out of the acquired stock's **basis** | the strike plus costs |
| Written option **expires worthless** | **Short-term gain**, however long it was open | none |
| Long option expires worthless | Loss of the premium, at its own holding period | none |
| Long put **exercised** | **Refused** (`PT-E-OPTION-UNSUPPORTED`) | — |

The premium is what was actually received or paid when the option was opened,
net of that trade's costs. It entered cash then, so it never enters cash
again at the event.

An option that resolves into stock produces **no independent P&L**. The stock's
own holding period governs the disposition. That is why a covered call
assigned against a two-year-old position is a long-term gain, premium included.
Stock acquired by an exercise or an assignment is held from that date.

**Exercising a long put is refused.** Buying a put on stock held a year or
less is treated as a short sale (IRC §1233(b)), which can restart the stock's
holding period. Married puts are excepted. `portable` does not record which
case applies, so it refuses rather than guess.

Each event is one ledger row on the option, and replay derives its effects,
so a rebuild reproduces it exactly.

**Not handled**, and not to be assumed handled:
- straddles (§1092);
- a covered call that is not "qualified", which suspends the stock's holding
  period (§1092(f));
- payments in lieu of dividends on a short position.

### Commissions and fees

On an **opening** trade they are added to basis: they are a cost of acquiring.
On a **closing** trade they reduce proceeds: they are a cost of selling. Either
way they never form a third term, which is what makes
`gain = proceeds − basis` complete.

Allocation across lots uses **largest remainder**, so the parts sum exactly to
the whole. Rounding each lot's share independently loses or invents a cent, and
that cent becomes a basis error, then a realized-gain error, then a wrong tax
figure.

---

## The estimate

```
estimated tax = realized gain × (federal + state + NIIT)
```

The rate is the account's **effective-dated** schedule as at the disposition
date, and the components are stored separately so the effective rate is
explainable arithmetic rather than a number to take on trust. Effective-dating
is what stops a rate change next year from restating what last year's sale cost.

A schedule dated on or before a sale already recorded would change that sale's
estimate, so `pt account tax-rates set` refuses it with `PT-E-RESTATES-HISTORY`,
naming the gains and the estimate before and after. When the schedule corrects
the rate that was really in force, `--restate` records it and restates the
estimates in the same transaction (ADR 0025).

A schedule is never edited. One entered wrongly is replaced on its own date
with `--corrects` (with `--restate` if it changes a recorded estimate). The
wrong one is kept, so `pt account show` lists what was entered and what
replaced it, and no estimate reads it again.

### What the estimate does not model

- bracket progressivity
- the capital-loss limitation, the $3,000 ordinary offset, and carryforwards
- qualified-dividend rate stacking
- AMT
- state-specific treatment of federal gains
- the taxpayer's other income

`portable` cannot see any of that. The estimate is useful for **comparing two
dispositions** — which lot to sell, whether to wait for long-term treatment —
and useless as a filing figure.

A realized **loss** produces a negative estimate. That is the value of the
deduction *at this rate*, not a refund, and short and long are never netted
into one figure — the netting rules between them are exactly the part this
engine does not model.

### Sheltered accounts report "inapplicable", not zero

In a `tax_deferred` or `tax_exempt` account the result carries
`is_taxable: false` and `estimated_tax: null`. A zero would say "we computed it
and it came to nothing", which is a different and false statement — and would
be indistinguishable from a genuine zero-rate result.

---

## Refusals

Each of these stops the command rather than producing a number.

| Situation | Code | Why |
|---|---|---|
| No lot matches a closing trade | `PT-E-LOT-UNMATCHED` | A zero basis by default would overstate the gain by the entire proceeds. `--force-zero-basis` lets a human decide, and records the decision on the lot's adjustment log. |
| Closing more than is held | `PT-E-LOT-INSUFFICIENT` | A short position is opened with `pt short`, not by overselling. |
| A short sale against a long holding, or a buy against a short one | `PT-E-POSITION-DIRECTION` | The first is a constructive sale (IRC §1259) `portable` does not record; the second is a cover (ADR 0038). |
| A return of capital on shares disposed of between its ex-date and pay-date, or with none held | `PT-E-ROC-ENTITLEMENT` | The disposed shares' part has no basis left to reduce, and where it goes instead is not decided (ADR 0039). |
| Exercising a long put | `PT-E-OPTION-UNSUPPORTED` | The put may have restarted the stock's holding period (IRC §1233(b)), and which case applies is not recorded. |
| Spec-ID that does not add up | `PT-E-LOT-SELECTION-INVALID` | Topping up from another lot changes the tax treatment silently. |
| A sale entered before a wash-sale split divided the lot it names | `PT-E-LOT-DESIGNATION-DIVIDED` | The parts carry different bases and holding periods; which was meant is the owner's to say, by reversing and re-entering it (ADR 0037). |
| A sale recorded before schema 10 whose lots are not settled | `PT-E-LOT-DESIGNATION-UNRESOLVED` | Reading its lot ids as numbered today is the defect that re-pointed sales (ADR 0036). |
| Average cost mixed with spec-ID | `PT-E-TAX-METHOD-CONFLICT` | Either resolution restates basis on shares already sold. |
| No rate schedule in force | `PT-E-TAX-NO-RATE-SCHEDULE` | A defaulted zero produces a plausible number that is wrong. |
| A fractional share the account cannot hold | `PT-E-FRACTIONAL-SHARE` | Cash in lieu is a taxable disposition; rounding it away hides one. |

---

## Where a basis came from, and when a gain is not reported

[ADR 0017](adr/0017-cutover-reconstruction-and-basis-provenance.md). A basis
`portable` computed from its own ledger is exact. A basis that entered the file
from somewhere else is not necessarily, and the difference is recorded on the
lot as `basis_source` — `NOT NULL` with no default, so no writer can create a
lot without answering the question.

| `basis_source` | What it means | In a report |
|---|---|---|
| `derived` | Computed from this portfolio's ledger. Exact. | Counted, unmarked |
| `custodian_asserted` | Read from a custodian's lot-detail report | Counted, marked |
| `reconstructed` | Today's stated basis less every later addition. Exact as an aggregate, averaged within the block | Counted, marked |
| `estimated` | A partly consumed block solved backwards under an assumed relief method | Counted, marked |
| `unavailable` | Nothing survives to anchor a solve | **Excluded from every total** |

`pt tax` and `pt pnl` state the share of reported cost basis that did not come
from this portfolio's own ledger, and the per-disposition column says which rung
each figure rests on. The share is measured on **basis** rather than on gain,
deliberately: the basis is the approximate input — proceeds and dates come from
the ledger and are exact — and a proportion of a signed total that may be near
zero misleads more often than it informs.

### Why `unavailable` is excluded rather than reported

A lot on that rung was seeded at its market value on the cutover date. That
value exists so cash conservation closes (`CLAUDE.md` invariant 4) and the
position engine has a lot to relieve. **It is not a basis claim.** The
difference between it and the proceeds is the change since an arbitrary date,
wearing the units of a gain and carrying none of its meaning.

So the disposition is left out of every total, listed separately with its
proceeds — which are exact, they come from the sale — and its basis and gain
reported as **null, never zero**. The year is marked `is_complete: false`. This
is the treatment `valuation_snapshot.is_complete` already gives a snapshot built
from a position that could not be priced, and it is `CLAUDE.md`'s rule that
blank and zero must never mean the same thing, one level up.

For the affected years the custodian's 1099-B is the authority and always was.
What `portable` must not do is print a number that looks like a tax figure and
is the gain since an arbitrary cutover instead.

## Wash sales

[ADR 0037](adr/0037-wash-sales.md). IRC §1091 disallows a loss on stock or
securities when substantially identical ones are acquired within 30 days
before or after the sale. **The window spans every account in the portfolio,
IRAs included.** Replay pairs each loss with its replacements when the later
of the two rows is applied, so a rebuild and a live entry agree.

**What is matched:**

- **Loss sales:** a loss on a long lot in a taxable account, by a sale, an
  expiration, or the stock a written call delivers.
- **Replacements**, in any account:
  - a buy;
  - a reinvested dividend;
  - a distribution taken in units;
  - stock acquired by an option.

  The law has no de minimis, so a small reinvestment disallows its share of
  a loss.
- **Instruments:** the same instrument, or one declared substantially
  identical with `pt instrument equivalent`. `portable` never infers it.

**Matching order:** losses are matched in ledger order, and within one sale
the earliest-acquired sold lot first. Replacement shares are matched in
order of acquisition. Each replacement share washes at most one sold share,
and only that share of the loss is disallowed. Shares bought in one purchase
are not each other's replacement.

**What a pairing does:**

| Replacement in | Loss | Replacement's basis | Holding period |
|---|---|---|---|
| A taxable account | Disallowed (Form 8949 code W) | Increased by the disallowed loss | Absorbs the sold shares' period, counting no day twice |
| A sheltered account | Disallowed **permanently** | Unchanged (Rev. Rul. 2008-5) | Unchanged |
| Disposed of before the loss sale | Reported for manual handling | — | — |

The matched shares become their own lot, keyed
`<replacement>~<sale>.<sold lot>`, so no other lot is renumbered. A sale
entered before a split divided the lot it designates is refused
(`PT-E-LOT-DESIGNATION-DIVIDED`). It is reversed and entered again, naming
the parts as they now stand. A reconstructed block beside a deferral is
named by `pt validate`, since the custodian's basis may already include it. A purchase
in January that disallows a December loss restates December's year. The
purchase's own result says so, and so does a sheltered replacement.

**Not detected, and said so in every tax output:**

- securities not declared substantially identical, which
  `pt tax wash-candidates` lists;
- options and contracts to acquire, listed but not applied;
- lots transferred in or seeded with a custodian-stated basis, which may
  already carry the custodian's own adjustment;
- short sales (§1091(e));
- acquisitions outside this portfolio, such as an account not in the file.

Every account in the file is taken to be the taxpayer's or their spouse's.

The §1091 and §1.1091-1 citations were checked against secondary sources
only. ADR 0037 records which points are still to be read in primary text.

---

## Trade-date accounting

Recognition is on the **trade date**. Settlement dates are recorded and do not
drive position or P&L recognition.

This is stricter than the old T+3 accommodation. That accommodation lived in
Q&A 4874, whose effective range **ended 31 December 2019** at the 2020 edition
boundary and which has not been reissued; there is no published T+3
accommodation under the current standard. See `PORT-GIPS-A05`.

---

## After-tax performance is a different thing

When `pert` gains after-tax returns in v0.2, they will be **supplemental
information** and labelled as such.

**After-tax performance is outside the GIPS standards entirely.** It was removed
at the *2010* edition — not 2020 — and transferred to the US country sponsor,
because tax rules are jurisdiction-specific. The governing reference is the
**USIPC After-Tax Performance Standards** (revised effective 1 January 2011):
US-specific, voluntary, and the only extant reference. See
[`gips-standard.md`](gips-standard.md) §7.1.

Do not cite "AIMR/GIPS after-tax guidance". That lineage no longer exists.
