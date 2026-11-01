# ADR 0033 — A row's identity spells its account as the portfolio does

- **Status:** Accepted — implemented in `services/import_extract.py` (`_history_ref`) and `importers/tabular.py`
- **Date:** 2026-11-01
- **Milestone:** v0.2
- **Governs:** `CLAUDE.md` invariants 2, 9; `docs/broker-import.md` §5 (identity) and §6 (`[account_aliases]`)
- **Amends:** [ADR 0012](0012-broker-import-pipeline.md) — "Identity, since the broker supplies none"

## Context

Where a custodian supplies no transaction id, ADR 0012 synthesizes
`external_ref` from the source row's raw text, so that revising the activity
map never changes the identity of a row already committed, and an overlapping
export is recognised at extract as `ledger:already-recorded` rather than
appended again.

`[account_aliases]` exists for the custodian that spells one account
differently across exports — `Brokerage` in the activity export, `BROKERAGE` in
the holdings export — and `docs/broker-import.md` §6 says the alias table
resolves the account column of every document "so a custodian that upper-cases
the account in one export and not in another needs no edit to its files". The
adapter applied the alias to `record.account`. The identity hash, though,
also hashed every source cell verbatim, the account's included.

The reference custodian then did exactly what §6 anticipates. Its activity
export, in mixed case through August 2026, upper-cased the brokerage and Roth
account names in the year-to-date export downloaded on 2026-11-01. Run
`--incremental` over the Jan–Aug overlap, every brokerage and Roth row
already in the ledger acquired a new reference; 223 of them would have been
appended a second time. Nothing at commit can refuse that: `UNIQUE
(account_id, external_ref)` refuses a *repeated* reference, and these were
new ones. Reconciliation would have caught it afterwards as a break of the
whole window's cash and units. That is the failure invariant 9 exists to
prevent upstream.

## Decision

**The account cell enters the identity material as the portfolio spells it.**
`TransactionRecord.account_column` names the source column the account was
read from; `_history_ref` substitutes the resolved account for that one cell
and hashes everything else verbatim, as before. The source row itself is not
touched: the batch still shows the reviewer what the custodian wrote.

Only the account cell, because it is the only cell the adapter already
resolves through a table the owner maintains for exactly this purpose. Every
other cell stays raw text, for ADR 0012's reason.

## What this does and does not change

- **A row whose account cell already read as the portfolio's name keeps its
  reference**, byte for byte: the substitution replaces the cell with the
  value it already had. A test pins that material against the ADR 0012
  formula. For the reference custodian's ledger this is every imported row —
  checked on 2026-11-01 by recomputing each Jan–Aug 2026 row of the new export
  both ways: verbatim, only the IRA (whose name did not change case) matched;
  with the cell resolved, every row the ledger carries matched.
- **A row committed from a cell that was an alias** (the export read
  `BROKERAGE` and the portfolio calls it `Brokerage`) was hashed under the old
  rule with the alias spelling. A re-import of that same export now computes
  the resolved spelling and will not match. No such row exists in any ledger
  this project knows of; if one appears, it shows at extract as the whole
  overlap appended instead of skipped — the same symptom as the defect, and
  visible in the batch before commit. The remedy then is an export window that
  starts after the ledger's last row, not a second identity rule.
- The **ordinal** is computed over the new material, so two rows identical
  except for the account's spelling are now one row repeated, and get
  ordinals 0 and 1. That is correct: they are the same event reported twice
  only if they are otherwise identical, which is ADR 0012's premise.

## Alternatives rejected

- **Edit the CSV's account column.** The batch would then quote a source row
  the custodian never wrote, its file hash would be of the edit, and the next
  export would need the same edit. §2 already says aliases, not edits.
- **Drop the account cell from the material** (the resolved account is already
  its first component). Cleaner, and it changes every reference in every
  ledger, so the very next overlapping import would append everything.
- **Check both the verbatim and the resolved reference at extract.** Covers
  the alias-committed case above, at the price of two identities per row and
  a rule nobody can state in a sentence. Rejected until a ledger needs it.
- **Case-fold the cell.** Fixes this export and not the next custodian, which
  writes the account by number in one file and by name in the other.
