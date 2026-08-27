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
| Wash sales | | | ✗ until v0.2 |
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

Note the separate question this does *not* answer: for **performance** purposes
return of capital is also not an external cash flow. Both facts are true, they
are about different things, and conflating them gets both wrong.

### Option premium

The rule depends on how the option resolves, and getting it wrong both
double-counts the premium and applies the wrong rate.

| Event | Where the premium goes |
|---|---|
| Long call **exercised** | Into the acquired stock's **basis** |
| Written call **assigned** | Into the stock sale's **proceeds** |
| Written option **expires worthless** | **Short-term gain**, however long it was open |
| Long option expires worthless | Loss of the premium, at its own holding period |

An option that resolves into stock produces **no independent P&L**. The stock's
own holding period governs the disposition — which is why a covered call
assigned against a two-year-old position is a long-term gain, premium included.

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
| Spec-ID that does not add up | `PT-E-LOT-SELECTION-INVALID` | Topping up from another lot changes the tax treatment silently. |
| Average cost mixed with spec-ID | `PT-E-TAX-METHOD-CONFLICT` | Either resolution restates basis on shares already sold. |
| No rate schedule in force | `PT-E-TAX-NO-RATE-SCHEDULE` | A defaulted zero produces a plausible number that is wrong. |
| A fractional share the account cannot hold | `PT-E-FRACTIONAL-SHARE` | Cash in lieu is a taxable disposition; rounding it away hides one. |

---

## Wash sales — not implemented

**`pt tax` does not account for wash sales.** Detection is deferred to v0.2
(backlog, P0), and until it lands every tax output says so on its face, in every
format, as an envelope field that a consumer cannot drop without noticing.

This is stated prominently rather than in a footnote because of what wash sales
are: **the 30-day window spans *all* of the taxpayer's accounts, including
IRAs**, and covers substantially identical securities and options. A tax report
that quietly omitted them would be wrong in a way that looks entirely normal —
the "silently wrong number" failure mode by definition.

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
