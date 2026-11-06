# ADR 0038 — A short sale credits cash, and an option resolves in replay

- **Status:** Accepted (owner chose this approach, 2026-11-06: fix short sales first, then the option lifecycle on top). The details below are the implementer's and were surfaced to the owner with the change
- **Date:** 2026-11-06
- **Governs:** `CLAUDE.md` invariants 3, 4, 9, 10; "Option premium" and "Short sales" in "Domain traps"; `docs/tax-methodology.md`
- **Extends:** [ADR 0010](0010-derived-state-and-replay.md), [ADR 0016](0016-out-of-order-appends-and-validation.md). Every derived effect comes from replay, including the option lifecycle's

## Context

Reproduced against the current code, every one silently:

- **A short sale was booked as a purchase.**
  - `TradingService.plan` debited cash for `sell_short` and credited it for
    `buy_to_cover`. Imports go through the same planner.
  - A short lot's basis was the gross *plus* costs, and a cover's
    disposition subtracted that from what the cover paid.
  - Valuation never applied the leg's sign, so a short position counted as an
    asset.

  Shorting 100 at 100.00 and covering at 80.00 is a gain of 2,000.00. Here
  cash fell by 2,000.00 and `pt tax` reported a short-term loss of 2,000.00.
  Every written option is a short sale, so it inherited all of this.
- **Nothing stopped a closing trade relieving the wrong direction.**
  - `pt cover` would relieve long lots, and `pt sell` short ones.
  - `pt short` against a long holding opened a short lot beside it. That is a
    constructive sale (IRC §1259), which `portable` cannot represent.
- **The option lifecycle derived its effects outside replay.**
  - `pt option assign` wrote its stock sale's dispositions itself, and replay
    had no branch for the row. `pt validate` failed straight after, and
    `pt rebuild` erased the sale: in the covered-call scenario, 2,420.00 of
    long-term gain vanished and 100 shares reopened.
  - `pt option expire` and the option half of `pt option exercise` did the
    same.
  - Every back-dated row, reversal and `--incremental` import rebuilds
    (ADR 0016), so any of them erased these.
- **The premium was counted twice in cash.**
  - Exercising a long call debited strike plus premium, although the premium
    was paid when the call was bought. The stock's basis then left the
    premium out.
  - Assignment credited strike plus premium, the premium having been received
    when the call was written.
  - Cash and the tax proceeds are different numbers, and both commands used
    the second for the first.
- **Puts were treated as calls.** Exercising a long put *bought* stock.

## Decision

1. **A short sale receives cash and a cover pays it.**
   - A `sell_short` credits its gross less costs.
   - A `buy_to_cover` debits its gross plus costs.
   - One planner computes this for typed trades and imports alike.
2. **A closing trade relieves lots of its own direction, and opening against
   the other direction is refused.**
   - A sale or a transfer out relieves long lots only. A cover relieves short
     lots only.
   - A short sale in an account holding the instrument long is refused,
     naming §1259. A buy in an account short the instrument is refused too:
     it is a cover.
   - Both checks run in the planner and again in replay, so no row reaches
     derived state around them.
3. **A short lot holds what the sale received.**
   - Its basis column is the credit: gross less costs.
   - At a cover, the disposition's proceeds are that credit, relieved by
     quantity. Its cost basis is what the cover paid, gross plus costs.
   - The gain is proceeds less basis, as for any disposition, and is always
     short-term, as before. This is Form 8949's shape for a short sale.
4. **A short position is negative wherever it is valued.**
   - One function signs a leg's quantity by the leg's sign. Lots and legs
     keep unsigned quantities, so invariant 5 is unchanged.
   - It is used by snapshot market value, the market value and cost basis in
     `pt holdings`, unrealized P&L, and the quantities `pt reconcile`
     compares.
5. **An option lifecycle row is an event on the option, derived in replay.**
   - **The row.** Its instrument is the option and its quantity is
     contracts. For an assignment or an exercise, the price is the strike,
     and the gross is the strike times contracts times multiplier. The cash
     is what moved. The stock side is derived from the option's detail: its
     underlier, multiplier and strike.
   - **The premium** is the basis of the option lots relieved, contracts
     taken in order of opening. It is the net premium actually paid or
     received, never the quoted gross.
   - **Each event:**

| Event | Stock | Option | Cash |
|---|---|---|---|
| Expiration, written | — | Disposition: proceeds = premium received, basis 0. Short-term, however long it was open | 0 |
| Expiration, long | — | Disposition: proceeds 0, basis = premium paid, by the option's own holding period | 0 |
| Written call assigned | Sold under the row's designation or method. Proceeds = strike × shares + premium − fees | Closed, no disposition of its own | strike × shares − fees |
| Written put assigned | Lot opened. Basis = strike × shares + fees − premium (`option_premium_assignment` adjustment). Held from the assignment date | Closed, no disposition | −(strike × shares + fees) |
| Long call exercised | Lot opened. Basis = strike × shares + fees + premium (`option_premium_exercise` adjustment). Held from the exercise date | Closed, no disposition | −(strike × shares + fees) |
| Long put exercised | **Refused** | | |

   **Why a long put's exercise is refused.** Buying a put on stock held a year
   or less is treated as a short sale (§1233(b), Treas. Reg.
   §1.1233-1(c)(3)), which can restart the stock's holding period. Married
   puts are excepted. Which case applies is not recorded. Refusing is honest
   (invariant 10); the command used to buy stock.
6. **No command writes derived state.** `pt option expire`, `assign` and
   `exercise` append the row and derive from it through `apply_or_rebuild`,
   as every trade does. A dry run is the real write, rolled back (ADR 0026).
7. **Rows already written.** None of the owner's files contain any (owner,
   2026-11-06). For other files:
   - **An assignment recorded on the underlier**, the old shape, is refused
     by replay, naming the row. It is to be reversed and re-entered.
   - **An exercise** was recorded as a buy of the stock, and replays as one.
     Its cash includes the premium and its basis does not. It cannot be told
     apart from a buy, but it leaves the option open past its expiry.
     `pt validate` therefore reports every option still open after its
     expiry, which an unrecorded expiration also leaves.
   - **Expiration rows** already had the new shape, and now replay.
   - **A short sale that debited cash, or a cover that credited it,** is
     reported by `pt validate`. It is corrected by a reversing entry and a
     new one (invariant 2), never edited.
8. **No schema change.** The ledger already has the columns and types.

## Consequences

- Realized gains on every cover, and every written option's figures,
  restate on the next rebuild. This includes the sample fixture.
- The sample's covered call (AAPL Jan-2024 200 call) had no closing row and
  stayed open past its expiry. AAPL closed below 200 on 2024-01-19, so the
  generator now records it expiring worthless. Its docstring, which said
  "assigned", is corrected. Assignment is covered by the scenario tests.
- Not handled, and said so in `docs/tax-methodology.md`:
  - straddles (§1092);
  - a covered call that is not "qualified", which suspends the stock's
    holding period (§1092(f));
  - payments in lieu of dividends on a short position;
  - long-put exercise (decision 5).
  - Rolls, which the options module's docstring named, have no command and
    no longer claim one.

## Alternatives considered

- **Store a short lot's credit as a negative basis.** The arithmetic of a
  sale then serves a cover unchanged. But every disposition would carry
  negative proceeds and a negative basis, and every report summing them
  would have to flip the signs back. Rejected for one rule at valuation.
- **Keep the old shape of the assignment row** (instrument = the underlier).
  The row would not say which option was assigned, except in its note text.
  Replay would have to read the note. Rejected.
- **Fix the option commands and leave short sales for later.** Changing the
  assignment's cash alone would have made the covered call's net cash error
  larger, from 420 to 840: the write's inverted debit stays. Rejected by the
  owner.
