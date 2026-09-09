# ADR 0014 — Advisory fees paid by one account for another

- **Status:** Proposed
- **Date:** 2026-09-08
- **Milestone:** v0.2
- **Governs:** `PORT-GIPS-B02`, `PORT-GIPS-D01`; ADR 0007
- **Scope:** a **custodian-specific** arrangement. [ADR 0018](0018-minimum-broker-dataset.md)
  places it in an optional per-custodian post-pass, expressed as a declarative pairing
  rule; most custodians will not need it. The fee *classification* below is general.

## Context

The owner's adviser bills a quarterly fee against each account and settles all
of them from the taxable brokerage account. The shape of one quarter, as the
broker reports it — **amounts illustrative**, the structure exact:

```
BROKERAGE  Expense (Management Fee)              +400.00   its own fee
BROKERAGE  Expense (Transfer to Cover Mgmt Fee)  +150.00   FEE FOR <IRA>
BROKERAGE  Expense (Transfer to Cover Mgmt Fee)  +120.00   FEE FOR <ROTH>
BROKERAGE  Expense (Transfer to Cover Mgmt Fee)   +60.00   FEE FOR <not in portfolio>
BROKERAGE  Expense (Transfer to Cover Mgmt Fee)   +50.00   FEE FOR <not in portfolio>
IRA        Expense (Transfer to Cover Mgmt Fee)  -150.00   FEE PAID BY OTHER
IRA        Expense (Management Fee)              +150.00
ROTH IRA   Expense (Transfer to Cover Mgmt Fee)  -120.00   FEE PAID BY OTHER
ROTH IRA   Expense (Management Fee)              +120.00
```

Three things in that block are traps.

**The fee appears twice.** The IRA's fee is reported in the brokerage (as the
transfer that funds it) and in the IRA (as the fee itself). Both rows carry the
activity word *Expense*. Mapping on that word charges the fee to two accounts and
understates two returns.

**Two of the brokerage's five rows are not fees at all.** They cover accounts
that are not in this portfolio. Across the sample export the brokerage's
transfer-to-cover rows exceed what the IRA and Roth receive by about 28%, and
that difference leaves the portfolio entirely. Classified as a fee it would
depress the brokerage's net-of-fees return by money that was simply removed.

**The sign convention inverts.** `Amount` is unsigned everywhere else in the
export, with direction implied by the activity — but the transfer-to-cover rows
are positive in the paying account and negative in the receiving one. An
importer that derives direction from the activity alone gets the receiving side
backwards, and the error is invisible until cash fails to reconcile.

## Decision

The quarterly block decomposes into **four kinds of `portable` event**, decided
in the activity map and not inferred at commit time:

| Broker rows | `portable` |
|---|---|
| Fee charged to an account that pays it itself | `fee`, `fee_class = external_mgmt_fee` |
| Transfer-to-cover pair, both accounts in the portfolio | one `transfer`, payer → payee |
| Fee charged to an account funded by that transfer | `fee`, `fee_class = external_mgmt_fee` |
| Transfer-to-cover with no counterpart in the portfolio | `withdrawal` |

So the quarter above becomes: a `fee` of 400.00 in the brokerage; a `transfer` of
150.00 brokerage → IRA and a `fee` of 150.00 in the IRA; a `transfer` of 120.00
brokerage → Roth and a `fee` of 120.00 in the Roth; and a `withdrawal` of 110.00
from the brokerage. Nine broker rows, seven ledger rows, no fee counted twice.

### Why `transfer` and not a withdrawal-plus-deposit

ADR 0007 already settles this and it binds here exactly: a transfer is **one**
ledger row with a `counter_account_id`, external at account level and no flow at
all at portfolio level. Entered as a withdrawal from the brokerage plus a
deposit into the IRA it becomes two genuine external flows at portfolio level —
four times a year, forever — which rewrites the portfolio's track record with
money that never left.

There is a second reason specific to retirement accounts. A deposit into an IRA
reads as a **contribution**, and contributions are capped and reportable. Paying
an IRA's advisory fee from outside it is a deliberate arrangement, not a
contribution, and modelling it as one would corrupt any contribution tracking
built later. (Nothing here is tax advice; the point is only that the two events
are different and must not share a representation.)

The two settlements to accounts outside the portfolio are **withdrawals**, and
those accounts stay outside. Bringing them in would convert each into a
`transfer` and remove the outflow from portfolio-level results, so this is a
scope decision, not a bookkeeping one: the portfolio is these three accounts, and
money paid on behalf of anything else has left it.

### Why the pairing is matched, not assumed

The importer pairs a transfer-to-cover row in the paying account with the
negative row in the receiving account by **amount, date window, and the account
reference in the note**. An unpaired row is *not* silently reclassified: it is a
`withdrawal` only when the note names an account that is demonstrably not in the
portfolio, and a refusal otherwise. The failure being guarded against is an
export that omits one side of a pair, which would otherwise turn a transfer into
a withdrawal and manufacture an external flow.

### The fee class

Every advisory fee here is `external_mgmt_fee`: it is an outside adviser's fee on
a separately managed account. Under the Asset Owner ladder `portable` follows
(`PORT-GIPS-D01`) it reduces net-of-external-costs-only and net-of-fees, and
does not touch gross-of-fees.

Two related notes for the same activity map. Custody and account-level
administrative charges are **not** transaction costs — the trap named in
`CLAUDE.md` — and are `internal_mgmt_cost` or `other_admin` respectively. And
the owner's accounts are a wrap arrangement in which the commission column is
empty on every row of the sample export, so **no** imported row carries
`transaction_cost` at all. That makes gross-of-fees and
net-of-external-costs-only numerically identical for these accounts, which
`ReturnBasis` anticipates; `pert` must report the figure once and label it,
rather than printing two identical columns.

## Consequences

- The activity map cannot be a flat string-to-type table. `Expense (Management
  Fee)` and `Expense (Transfer to Cover Management Fee)` both require the
  *other* rows in their quarter to be interpreted, so the adapter needs a
  pairing pass over the whole export before it emits a batch.
- That pairing pass is the most intricate part of any adapter, so it is a pure
  function over rows with its own fixture, tested on a full quarter including
  the unpaired outside-account case.
- Cash reconciliation catches the sign error and the double count, and nothing
  else does. It has to exist before the first real import.
- The quarterly transfers make the brokerage's account-level external flows
  genuinely lumpy. That is correct and it is what account-level TWR is for.

## Alternatives considered

- **Charge each fee to the account the broker charged it to and ignore the
  funding transfers.** Rejected: cash then fails to reconcile in all three
  accounts, and the brokerage's outflow to the two outside accounts disappears
  entirely.
- **Treat the whole arrangement as a single brokerage fee.** Rejected: it is
  arithmetically tidy and economically false. The IRA and the Roth genuinely bear
  their own fees; loading all of it onto the brokerage overstates the retirement
  accounts' net-of-fees returns and understates the brokerage's.
- **Record the outside-account payments as fees.** Rejected: they buy the owner
  nothing inside this portfolio. Money that leaves the portfolio is a withdrawal,
  whatever it is spent on.
