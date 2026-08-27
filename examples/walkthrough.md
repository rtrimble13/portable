# A worked example

Twenty minutes, start to finish. By the end you will have built a portfolio,
recorded the transactions that are easy to get wrong, and asked it the questions
it exists to answer.

Everything here runs against a file you create. Nothing needs a market data
warehouse, a network connection, or a compiler.

---

## Before you start

```bash
git clone https://github.com/rtrimble13/portable
cd portable
scripts/bootstrap.sh          # or scripts\bootstrap.ps1 on Windows
source .venv/bin/activate     # or .venv\Scripts\Activate.ps1
pt --version
```

Two things worth knowing up front, because they will save you a puzzled minute:

- **Global flags work anywhere.** `pt holdings --format json` and
  `pt --format json holdings` do the same thing.
- **Dates are always `YYYY-MM-DD`**, in every command and every format. No
  locale-dependent input, because that would make the *meaning* of a command
  depend on where you are sitting.

---

## 1. Create a portfolio

A portfolio is one file.

```bash
pt init demo.port \
  --name "Walkthrough" \
  --inception 2024-01-02 \
  --description "A worked example."
```

`--description` is not decoration: it is a required disclosure on every report
that will eventually come out of this file.

From here on, every command needs to know which file. Rather than typing
`--port demo.port` fifty times:

```bash
export PORTABLE_PORT=demo.port          # PowerShell: $env:PORTABLE_PORT="demo.port"
```

---

## 2. Open an account and tell it about tax

```bash
pt account add --name Brokerage --type taxable \
  --custodian "Example Broker" --opened 2024-01-02 \
  --relief-method fifo

pt account tax-rates set --account Brokerage \
  --short 0.37 --long 0.20 --state 0.05 --niit 0.038 \
  --effective-from 2024-01-01
```

Two decisions worth understanding.

**The rate is stored in components**, not as one blended number. When a report
later says your effective rate is 28.8%, you can see that it is 20 + 5 + 3.8
rather than having to take it on trust.

**It is effective-dated.** Set a new schedule next year and last year's
dispositions keep the rate that was in force when they happened. A rate change
never restates history.

**Note the relief method.** `--relief-method fifo` here rather than the default
`spec`, because spec-ID means *you* identify the shares — and a sale that does
not designate any is refused rather than quietly falling back. We will use
spec-ID deliberately in step 6.

---

## 3. Fund it and buy something

```bash
pt cash deposit --account Brokerage --amount 100000 --date 2024-01-02

pt instrument add AAPL --type equity --name "Apple Inc." \
  --exchange NASDAQ --sector Technology

pt buy AAPL --qty 100 --price 185.64 --date 2024-01-03 \
  --account Brokerage --fees 1.00 --fee-class transaction_cost
```

Try leaving `--fee-class` off. You get:

```
error [PT-E-FEE-CLASS-MISSING]: this trade has fees but no fee classification
```

That is deliberate. The three return bases — gross-of-fees,
net-of-external-costs-only, net-of-fees — are *derived* from how each fee is
classified, so an unclassified fee makes all three unanswerable. A brokerage
commission is `transaction_cost`. A **custody fee is not** — it is an
`internal_mgmt_cost`, and it reduces net-of-fees returns only. `portable` will
not guess which one you meant.

Buy a bit more, later and higher:

```bash
pt buy AAPL --qty 50 --price 210.00 --date 2024-08-15 \
  --account Brokerage --fees 1.00 --fee-class transaction_cost
```

---

## 4. Take a dividend

```bash
pt income dividend AAPL --account Brokerage --amount 24.00 \
  --ex-date 2024-02-09 --pay-date 2024-02-15 --qualified
```

Both dates, because they answer different questions. **Entitlement** is fixed on
the ex-date; **cash** arrives on the pay-date. Between them the portfolio is owed
money, and that receivable belongs in market value. Accruing on the wrong date
moves return across a period boundary — which shows up as one good quarter and
one bad one rather than as an error.

---

## 5. Look at your lots

```bash
pt lot list AAPL --as-of 2025-06-30
```

```
Lot  Account    Symbol  Acquired    Qty      Basis  Basis/Unit  Holding  Days
---  ---------  ------  ----------  ---  ---------  ----------  -------  ----
  1  Brokerage  AAPL    2024-01-03  100  18,565.00      185.65  long      544
  2  Brokerage  AAPL    2024-08-15   50  10,501.00      210.02  short     319
```

This is the table to read before selling anything. The January lot is already
long-term; the August one is not. Note the basis includes the $1 commission —
on an opening trade a commission is part of what the shares cost.

---

## 6. Sell, choosing your lots

```bash
pt sell AAPL --qty 60 --price 232.50 --date 2025-06-30 \
  --account Brokerage --fees 1.00 --fee-class transaction_cost \
  --method spec --lots 1:60
```

```
Lot  Acquired    Qty  Basis Relieved  Holding  Days
---  ----------  ---  --------------  -------  ----
  1  2024-01-03   60       11,139.00  long      544
```

You designated 60 shares from lot 1, and got long-term treatment. Had you sold
from lot 2 the gain would have been short-term and taxed at 45.8% rather than
28.8% — which is the entire reason specific identification exists.

Try `--lots 1:50` and watch it refuse: a designation that does not add up to the
traded quantity is an error, not something to top up from another lot.

---

## 7. Price it, and see where you stand

```bash
pt price set AAPL --price 205.17 --date 2025-06-30 \
  --valuation-level 1 --source "broker statement"

pt holdings --as-of 2025-06-30
```

`--valuation-level 1` says this is an observable quoted price. The **default is
5** — a subjective, unobservable input — because a price typed at a terminal
with no documented basis is exactly that, and recording it as an exchange close
would understate the share of your portfolio priced on unobservable inputs.

Now ask what it cost you:

```bash
pt tax --year 2025
```

Read the disclaimer at the bottom. It is not boilerplate: it says what the
estimate does not model, and it says plainly that **this report does not account
for wash sales**. That matters because the 30-day window spans *all* of your
accounts, including IRAs. Detection lands in v0.2; until then the report says so
on its face and you cannot turn it off.

---

## 8. The part that surprises people

Open a second account and move money between them.

```bash
pt account add --name IRA --type tax-deferred --opened 2024-01-02
pt cash transfer --from Brokerage --to IRA --amount 20000 --date 2025-07-01
```

Now ask the same question at two different levels:

```bash
pt cash-flows --level account   --external-only --from 2024-01-01 --to 2025-12-31
pt cash-flows --level portfolio --external-only --from 2024-01-01 --to 2025-12-31
```

At **account** level the transfer is there: money left the brokerage. At
**portfolio** level it is **absent**, because it never left the portfolio — it
nets to zero.

This is the single easiest way to produce a wrong return. Count that transfer as
an external flow at portfolio level and a $20,000 shuffle between your own
accounts silently rewrites your track record, with a number that is
arithmetically defensible and economically meaningless. Notice also that the
dividend appears in neither: **income is never an external cash flow.**

---

## 9. Corporate actions

```bash
pt instrument add ACME --type equity
pt buy ACME --qty 100 --price 60.00 --date 2024-02-01 \
  --account Brokerage --fees 1.00 --fee-class transaction_cost

pt ca split ACME --ratio 3:1 --ex-date 2024-06-03
pt lot list ACME --as-of 2025-03-01
```

300 shares, basis still $6,001 in total, and — the important part — **the
holding period did not reset**. `Acquired` still reads 2024-02-01 and the lot is
long-term. A split divides the same claim differently; it does not restart the
clock. Resetting it would turn a long-term gain into a short-term one, which is
a rate error that looks like nothing at all on the report.

---

## 10. Make a mistake, then fix it properly

```bash
pt buy AAPL --qty 1000 --price 210.00 --date 2025-08-01 \
  --account Brokerage --fees 1.00 --fee-class transaction_cost
```

Wrong — that should have been 100. You cannot delete it, and the database will
not let you:

```bash
pt trade reverse <TXN_ID> --note "wrong quantity" --date 2025-08-01
pt buy AAPL --qty 100 --price 210.00 --date 2025-08-01 \
  --account Brokerage --fees 1.00 --fee-class transaction_cost
```

```bash
pt activity --from 2025-08-01 --to 2025-08-31
pt holdings --as-of 2025-08-31
```

History shows all three entries. Your position shows the net. That is what makes
the trail defensible: an auditor can see what happened *and* what you did about
it, and nothing was quietly rewritten.

---

## 11. Prove it to yourself

```bash
pt validate
pt rebuild
pt validate
```

`pt rebuild` throws away every derived figure — positions, lots, dispositions,
balances — and reconstructs them by replaying the ledger from inception. The
numbers do not change, which is the point: derived state is a cache of the
ledger, not a second source of truth.

This is the standard response to any surprising number, and to any bug fix.

---

## 12. Machine-readable, all the way down

```bash
pt holdings --format json | jq '.data.rows[] | {symbol, market_value}'
pt tax --year 2025 --format csv > tax-2025.csv
pt pnl --format markdown          # paste into notes or an LLM context window
pt introspect --format json | jq '.data.commands | length'
```

Structured output goes to stdout; logs and warnings go to stderr, so a pipeline
keeps working with `-v` on. `Decimal` serializes as a **string**, never a float —
a JSON number cannot round-trip a decimal, and a consumer parsing one as a float
loses the exactness the whole tool is built on.

A missing value is `null`, never `0`. An unpriced holding says "we do not know",
not "it is worthless".

---

## 13. Look at the real thing

```bash
pt --port examples/sample.port info
pt --port examples/sample.port holdings --as-of 2025-06-30
pt --port examples/sample.port tax --year 2025
pt --port examples/sample.port lot list --as-of 2025-06-30
```

`examples/sample.port` is generated, not hand-made — `make fixtures` rebuilds
it. One hundred transactions across three accounts and five years, including a
split, a spinoff, a covered call written and assigned, a bond bought between
coupons, a large cash flow, an inter-account transfer, a reversed trade, and a
mid-history tax rate change.

---

## Where to go next

| If you want to | Read |
|---|---|
| Understand the concepts properly | [`docs/domain-model.md`](../docs/domain-model.md) |
| Know what the tax figures do and do not model | [`docs/tax-methodology.md`](../docs/tax-methodology.md) |
| Connect real market data | [`docs/market-data.md`](../docs/market-data.md) |
| Consume the output from a script or an agent | [`docs/output-formats.md`](../docs/output-formats.md) |
| Know why anything is the way it is | [`docs/adr/`](../docs/adr/) |

---

## One thing to remember

When `portable` cannot tell whether an answer is right, it **refuses and
explains** rather than producing a number. A command that stops is doing its
job. The exit code tells you which kind of problem it was:

`0` ok · `1` generic · `2` usage · `3` portfolio/file · `4` validation ·
`5` data unavailable · `6` reconciliation break
