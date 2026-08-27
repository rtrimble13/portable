# GIPS Standard for `portable`

**A provision-traceable engineering guideline for performance measurement in the `portable` platform.**

| Field | Value |
|---|---|
| Document ID | `docs/gips-standard.md` |
| Status | **Draft 1 — for review.** Not yet ratified as a repo standard. |
| Anchored edition | Global Investment Performance Standards, **2020 edition** (Firms; Asset Owners) |
| Posture | **GIPS-modelled methodology. No claim of GIPS compliance, now or later, is made or implied.** See §4 and §9. |
| Applies to | `portable_core` (valuation, cash-flow, and return paths), `pt` (`value`, `mark`, `cash-flows`, `holdings`, `pnl`), and all of `pert` (v0.2) |
| Supersedes | The one-line GIPS note in `portable_bootstrap_prompt.md` §4.7 |
| Author | Research pass, 27 August 2026 |

---

## 0. How to use this document

Every normative item in §6 has the same shape:

> **`PORT-GIPS-xxx` — short name**
> **Source** — the GIPS provision(s), with `(A)` for a Requirement and `(B)` for a Recommendation, plus any effective-date footnote.
> **What GIPS says** — a faithful restatement.
> **What `portable` must do** — the engineering obligation.
> **Data model** — the schema or domain-object consequence, where there is one.
> **Test** — the acceptance criterion. A requirement with no test is not implemented.

`PORT-GIPS-xxx` identifiers are **stable**. Cite them in code comments, ADRs, issue titles, and test names. When a requirement is superseded, mark it superseded here — never renumber.

Two conformance profiles are used:

- **CORE** — required before `pert` may emit any return number. Failing a CORE item is a bug of the "silently wrong number" class that `CLAUDE.md` names as the worst failure mode in this repo.
- **EXT** — required only when the corresponding feature exists (composites, private markets, multi-currency). Until then, the feature must refuse rather than approximate (`CLAUDE.md` invariant 10).

---

## 1. Purpose

`portable` computes returns that its owner will use to make and defend real investment decisions. The GIPS standards are the only globally recognised, professionally governed answer to the question *"is this return number computed honestly?"* — they exist precisely because the same portfolio can be made to show wildly different returns by defensible-looking choices about valuation timing, cash-flow treatment, and fee handling.

This document does three things:

1. **Fixes the golden source.** It names the exact CFA Institute documents that govern, their editions and effective dates, and what is archived (§2, §3).
2. **Translates the standard into engineering requirements** the `portable` codebase can be built and tested against, provision by provision (§6).
3. **Draws the compliance boundary hard**, so that no artefact `portable` produces can be read as a compliance claim (§4, §9).

It deliberately does **not** attempt to make `portable` GIPS-compliant. Compliance is not a property a piece of software can have — see §4.

---

## 2. Golden source register

All of the following are published by CFA Institute at `gipsstandards.org`. Confirmed current as of **August 2026**: there is **no 2025 or 2026 edition** of the Firms, Asset Owners, or Verifiers standards. The 2020 edition is the only effective edition; the site's *Work in Process* page carries a single non-authoritative exposure draft (return attribution), and the 2010, 2005, and 1999 editions are archived.

### 2.1 Primary — normative

| # | Document | Edition | Effective | URL |
|---|---|---|---|---|
| **S1** | Global Investment Performance Standards (GIPS®) **for Firms** | 2020 | 1 Jan 2020; reports covering periods ending on or after 31 Dec 2020 must use this edition | [PDF](https://www.gipsstandards.org/wp-content/uploads/2021/03/2020_gips_standards_firms.pdf) |
| **S2** | GIPS® **for Asset Owners** | 2020 | as above | [PDF](https://www.gipsstandards.org/wp-content/uploads/2021/02/2020_gips_standards_asset_owners.pdf) |
| **S3** | GIPS® **for Verifiers** | 2020 | 1 Jan 2020 | [PDF](https://www.gipsstandards.org/wp-content/uploads/2021/03/2020_gips_standards_verifiers.pdf) |
| **E1** | Errata — GIPS Standards for Firms | Jul 2020 | amends 1.A.20, 1.A.21, 8.A.6, Appendix A Sample 3 | [PDF](https://www.gipsstandards.org/wp-content/uploads/2021/03/errata_gips_standards_for_firms.pdf) |
| **E2** | Errata — GIPS Standards for Asset Owners | Nov 2020 | amends 22.B.9, 22.B.10, 24.A.1 footnotes, 24.A.1.j, 24.C.8, 24.C.30, and five glossary entries (`INVESTMENT MANAGEMENT COSTS` and `NET-OF-EXTERNAL-COSTS-ONLY` amended; `CLOSED-END`, `STANDARD DEVIATION`, `WRAP FEE` added) | [PDF](https://www.gipsstandards.org/wp-content/uploads/2021/03/errata_november_2020_gips_standards_for_asset_owners.pdf) |

**Read the errata.** E2 in particular is substantive: it is what puts the words "using monthly returns" into the asset-owner standard-deviation requirement (24.A.1.j) and adds the "36 monthly returns unavailable" disclosure (24.C.30).

### 2.2 Primary — explanatory

| # | Document | Note | URL |
|---|---|---|---|
| **H1** | GIPS Standards **Handbook for Firms** | Provision-by-provision explanation. Absorbed most pre-2020 guidance statements. | [HTML](https://www.gipsstandards.org/standards/gips-standards-for-firms/gips-standards-handbook-for-firms/) |
| **H2** | GIPS Standards **Handbook for Asset Owners** | as above | [PDF](https://www.gipsstandards.org/wp-content/uploads/2021/03/gips_standards_handbook_for_asset_owners.pdf) |
| **C1** | GIPS Reports for Firms: Comparison of Sections 4–7 | Restates provisions 4–7 verbatim in a side-by-side grid. The most machine-readable rendering of the report sections. | [PDF](https://www.gipsstandards.org/wp-content/uploads/2025/04/gips_standards_firms_report_comparison-1.pdf) |
| **C2** | GIPS Asset Owner Reports: Comparison | Same for AO sections 24–25. | [PDF](https://www.gipsstandards.org/wp-content/uploads/2025/04/gips_asset_owners_report_comparison.pdf) |
| **Q1** | GIPS Q&A Database | **Authoritative** — "Q&As are considered to be authoritative guidance and must be followed in order to claim compliance." Every entry carries a Status (Current/Archived) and an effective-date range. **Always check both.** | [Database](https://www.gipsstandards.org/standards/q-a-database/) |

### 2.3 Guidance Statements currently in force under the 2020 edition

Only seven are live. The 2010-lineage guidance statements on Calculation Methodology, Fees, Composite Definition, Error Correction, Portability, Verification, and Carve-Outs are **all archived** — their content moved into the numbered provisions and the Handbooks.

| Guidance Statement | Effective | Relevance to `portable` | URL |
|---|---|---|---|
| **Benchmarks for Firms** (rev. Jul 2023) | 1 Apr 2021 | High — §6.G | [PDF](https://www.gipsstandards.org/wp-content/uploads/2023/08/gs_benchmarks_firms.pdf) |
| **Benchmarks for Asset Owners** | 30 Jun 2023 | High — §6.G | [PDF](https://www.gipsstandards.org/wp-content/uploads/2023/04/guidance-statement-benchmarks-asset-owners.pdf) |
| **Wrap Fee Portfolios** | 1 Oct 2021 | Medium — the only current authority on **bundled fees** | [PDF](https://www.gipsstandards.org/wp-content/uploads/2021/09/gs_wrap_fee_portfolios.pdf) |
| **Overlay Strategies** | 1 Jan 2022 | Low — no overlay mandates in scope | [PDF](https://www.gipsstandards.org/wp-content/uploads/2022/01/gs_overlay_2022.pdf) |
| **OCIO Portfolios** | 31 Dec 2025 | None | [PDF](https://www.gipsstandards.org/wp-content/uploads/2024/12/gs-for-ocio-porfolios.pdf) |
| **Firms Managing Only Broad Distribution Pooled Funds** | 1 Jul 2024 | None | [PDF](https://www.gipsstandards.org/wp-content/uploads/2023/12/gs-firms-managing-only-bdpf.pdf) |
| **Verifier Independence** | 30 Jun 2020 | None | [PDF](https://www.gipsstandards.org/wp-content/uploads/2021/03/verifier_independence_gs_2020.pdf) |

### 2.4 Archived but still the best available statement of a formula

| Document | Status | Why it is still cited here |
|---|---|---|
| **Guidance Statement on Calculation Methodology** (2011) — [PDF](https://www.gipsstandards.org/wp-content/uploads/2021/03/calculation_methodology_gs_2011.pdf) | **Archived.** Superseded in form by the 2020 provisions + Handbook. | The 2020 provisions specify the *outcome* ("returns that adjust for daily-weighted external cash flows") but never name a method. This archived document is the last place CFA Institute wrote the **Modified Dietz** formula down. See `PORT-GIPS-B06`. |

### 2.5 Outside GIPS but load-bearing for `portable`

| Document | Status | Note |
|---|---|---|
| **USIPC After-Tax Performance Standards** — [PDF](https://www.gipsstandards.org/wp-content/uploads/2024/10/usipc-after-tax-performance-standards.pdf) | Current, US-specific, **voluntary**. Effective 1 Jan 2011. | After-tax reporting was removed from the GIPS standards at the **2010** edition and handed to the US country sponsor. There is **no** after-tax content anywhere in the 2020 GIPS standards. This is the only reference for `portable`'s after-tax ambitions. See §7.1. |

### 2.6 Retrieval note

The full-text PDFs (**S1**, **S2**) truncate in automated markdown conversion before reaching their glossaries and appendices. Fourteen glossary terms could not be recovered verbatim by any permitted route; they are listed in §11.2. **Before this document is ratified, someone should read the printed glossary and appendices and close those gaps.** Everything in §6 that depends on an unrecovered definition is flagged.

---

## 3. Reading GIPS: conventions the spec depends on

**Numbering.** Firms provisions are Sections **1–8**. Asset Owner provisions are Sections **21–26** — the numbering deliberately continues rather than restarting, so a bare provision number is globally unambiguous. There is no Section 9, and no Section 27.

| Firms | | Asset Owners | |
|---|---|---|---|
| 1 | Fundamentals of Compliance | 21 | Fundamentals of Compliance |
| 2 | Input Data and Calculation Methodology | 22 | Input Data and Calculation Methodology |
| 3 | Composite and Pooled Fund Maintenance | 23 | Total Fund and Composite Maintenance |
| 4 | Composite Time-Weighted Return Report | 24 | Total Fund and Composite Time-Weighted Return Report |
| 5 | Composite Money-Weighted Return Report | 25 | Additional Composite Money-Weighted Return Report |
| 6 | Pooled Fund Time-Weighted Return Report | 26 | GIPS Advertising Guidelines |
| 7 | Pooled Fund Money-Weighted Return Report | | |
| 8 | GIPS Advertising Guidelines | | |

**Sub-lettering is not uniform. This trips people up.**

- In Sections **1, 2, 3, 21, 22, 23**: `A` = Requirements, `B` = Recommendations.
- In Sections **4, 5, 6, 7, 24, 25**: `A` = Presentation & Reporting *Requirements*, `B` = Presentation & Reporting *Recommendations*, `C` = Disclosure *Requirements*, `D` = Disclosure *Recommendations*.
- Section **8** uses yet another scheme: `8.A`, `8.B`, `8.C`, `8.E`, `8.G` are Requirements; `8.D`, `8.F`, `8.H` are Recommendations.

Any tooling that classifies a provision as required/recommended **must key off `(section, letter)`, never the letter alone.**

**MUST / SHOULD.** `MUST` = mandatory. `SHOULD` = recommended best practice, not required. GIPS sets these in small caps throughout; this document uses **bold `(A)`** and **`(B)`** instead.

**Effective-date footnotes.** The 2020 edition preserves historical phase-ins as numbered footnotes hanging off individual provisions and even individual sub-items — not as inline text. A provision read without its footnote is frequently wrong for historical periods. All footnotes material to `portable` are reproduced in §6.

**Minimum effective compliance date.** 1 January 2000 generally; 1 January 2006 for real estate, private equity, and wrap fee composites. This is the earliest date from which a compliance claim can reach.

---

## 4. Applicability: `portable` cannot be GIPS-compliant, and neither can its owner

This section exists so that nobody later has to relitigate it.

### 4.1 Compliance is entity-wide, never portfolio-wide

> **Firms 1.A.1 (A)** — "The GIPS standards must be applied on a firm-wide basis. Compliance must be met on a firm-wide basis and cannot be met on a composite, pooled fund, or portfolio basis."

> **Asset Owners 21.A.1 (A)** — "Compliance must be met on an asset owner–wide basis and cannot be met on a total fund, composite, pooled fund, or portfolio basis."

Both regimes exclude the single-portfolio case by construction. There is no third regime.

### 4.2 The claiming party must be an entity

> **Asset Owners 21.A.2 (A)** — an asset owner is "an entity that manages investments, directly and/or through the use of external managers, on behalf of participants, beneficiaries, or the organization itself." The enumerated list — pension funds, endowments, foundations, family offices, provident funds, insurers, sovereign wealth funds, fiduciaries — is non-exhaustive but uniformly organisational.

The **Guidance Statement on the Application of the GIPS Standards to Asset Owners (2018)**, under *Target Audience*, is explicit:

> "Please note that the term asset owner applies to organizations, such as pension plans and foundations, and not to individuals."

*Precision note:* that sentence lives in the 2018 Guidance Statement, which the 2020 edition supersedes in part. Inside the 2020 standard the exclusion is carried structurally — by 21.A.1 (no portfolio-basis compliance) and 21.A.2 (entity definition) — rather than by an express anti-individual sentence. The outcome is identical either way.

### 4.3 Compliance is a property of an organisation's policies, not of software

Compliance requires documented firm-wide policies and procedures (1.A.5 / 21.A.6), an error-correction policy with a self-defined materiality threshold, records supporting every reported figure (1.A.25 / 21.A.19), notification to CFA Institute via the annual compliance form (1.A.38 / 21.A.27), and — as best practice, not requirement — independent verification (1.B.3 / 21.B.3). Software can *support* every one of those. It cannot *be* any of them.

### 4.4 Which regime `portable` models

`portable` models the **Asset Owner** regime as its primary reference and borrows from the **Firms** regime where the Asset Owner standards are silent. The structural fit:

| GIPS (Asset Owners) | `portable` |
|---|---|
| Asset owner | the portfolio's owner (an individual — hence no compliance claim) |
| **Total fund** | the `.port` file: one portfolio, one investment mandate, multiple asset classes |
| Portfolio within the total fund | an **account** |
| **Additional composite** | a future grouping of accounts by strategy or asset class — `v1.0` backlog item "Performance composites" |
| **Oversight body** | the owner |
| GIPS Asset Owner Report | a `pert` tearsheet |
| Total fund benchmark (policy-weight blend of asset-class benchmarks) | the portfolio-level blended benchmark |

Why Asset Owners rather than Firms:

- The reporting unit is the **total fund**, not a composite. `portable` reports on one portfolio. (Firms 3.A.2 would demand every discretionary portfolio sit in at least one composite, which is meaningless here.)
- Composites are **optional** for asset owners (Handbook: "Asset owners are not required to present composites in compliance with the GIPS standards but may choose to do so"). That matches `portable`'s roadmap, where composites are a `v1.0` item.
- The minimum initial track record is **one year** (21.A.4), not five (1.A.3).
- Section 24 has **no internal-dispersion requirement** — 24.A.1 runs a–j and sub-item (i) is *total asset owner assets*, not dispersion. (This is a genuine trap: Firms 4.A.1.**i** *is* internal dispersion. The sub-letters do not line up between the two regimes. Verified and settled — see §11.1.)

Where the Asset Owner standards are silent and the Firms standards are not — most notably the internal-dispersion and five-or-fewer-portfolios machinery in Firms 4.A.1.f/i, and the whole of composite maintenance in Section 3 — this document cites the Firms provision and marks it **EXT**, deferred until `portable` grows composites.

---

## 5. Where the requirements land in the codebase

| GIPS concern | `portable` module | Milestone |
|---|---|---|
| Fair value, valuation frequency, price source and staleness | `portable_core/providers/`, `services/ValuationEngine` | v0.1 (built) |
| `valuation_snapshot` substrate: BMV, EMV, accrued income, external flows and timing | `portable_core/schema/`, `services/ValuationEngine`, `pt value` | v0.1 (built) |
| External vs. internal cash-flow classification | `portable_core/domain/`, `pt cash-flows --external-only` | **v0.1 — audit required, see `PORT-GIPS-B02`** |
| Large-cash-flow policy and revaluation | `portable_core/config/`, `services/ValuationEngine` | **v0.2** |
| TWR: sub-period returns, geometric linking, Modified Dietz fallback | `portable_pert` | v0.2 |
| MWR / XIRR | `portable_pert` | v0.2 |
| Return bases (gross / net-of-external-costs-only / net) and fee classification | `portable_core/domain/`, `services/` | **v0.2 — schema change, see `PORT-GIPS-D01`** |
| Risk measures, 3-year ex-post σ | `portable_pert` | v0.2 |
| Benchmarks, blends, rebalancing | `portable_core/schema/benchmark`, `portable_pert` | v0.2 |
| Report contents and disclosure block | `portable_core/formatters/`, `portable_pert` tearsheet | v0.2 |
| Compliance-language lint | `portable_core/` lint rule + CI | **v0.1 — add now, see `PORT-GIPS-J05`** |
| Composites | new | v1.0 |

Three items above are marked as needing attention **before** `pert` starts: external-flow classification, the fee-classification schema, and the compliance-language lint. Each is cheap now and expensive later — the first two are schema changes, and the third is the guard that stops a wrong claim shipping.

---

## 6. The normative requirements

### 6.A — Input data and valuation

---

**`PORT-GIPS-A01` — Fair value is the valuation basis · CORE**

**Source** — Firms **2.A.19 (A)**, fn 7: required for periods beginning on or after 1 Jan 2011. Asset Owners **22.A.16 (A)**, same date.
**What GIPS says** — "Portfolios must be valued in accordance with the definition of fair value." Prior to 2011, fair value *or* market value was acceptable.
**What `portable` must do** — Every price used in a valuation snapshot must be a fair-value price for the measurement date. For exchange-traded instruments this is the unadjusted closing price on the security's exchange calendar. `portable` already mandates **unadjusted** prices with explicit corporate-action transactions (`CLAUDE.md`, "Adjusted vs. unadjusted prices") — that is correct and is *required*, not merely preferred: adjusted prices are not fair values on the measurement date and double-count splits.
**Data model** — `price` carries source and as-of timestamp (already specified in `docs/market-data.md`). Add a `valuation_basis` enum so a price can record whether it was an exchange close, a model price, or an estimate.
**Test** — `test_valuation_uses_unadjusted_prices`: given a split mid-period, a valuation snapshot built from `mart.*` adjusted prices and one built from `core.*` unadjusted prices plus the split transaction must produce the same ending market value; the adjusted path must be refused by the provider layer, not silently accepted.

---

**`PORT-GIPS-A02` — The valuation hierarchy · CORE**

**Source** — Firms **2.B.6 (B)**; Asset Owners **22.B.6 (B)**. *Recommendation, not requirement.*
**What GIPS says** — five levels, in order: (a) objective, observable, unadjusted quoted market prices for **identical** investments in **active** markets; (b) observable quoted prices for **similar** investments in active markets; (c) quoted prices for identical or similar investments in **non-active** markets; (d) market-based inputs other than quoted prices that are observable; (e) **subjective, unobservable inputs**, used only when nothing above is available or appropriate.

Note the printed inconsistency: item (a) reads "must be valued" inside a recommendation. That is how CFA Institute prints it. The hierarchy as a whole is `(B)`.
**What `portable` must do** — Adopt the hierarchy explicitly and record the level used for every price. `portable`'s "fail loudly on ambiguity" invariant means a level-(e) price must be visible, not silent.
**Data model** — `price.valuation_level` ∈ {1,2,3,4,5} mapping to (a)–(e). Default 1 for provider EOD closes; 5 for any manually-set price entered via `pt price set` without a documented basis.
**Test** — `test_price_records_valuation_level`; and a report-level assertion that the percentage of portfolio market value at level 5 is computed and available (needed by `PORT-GIPS-H05`).

---

**`PORT-GIPS-A03` — Valuation frequency for time-weighted returns · CORE**

**Source** — Firms **2.A.23 (A)**; Asset Owners **22.A.20 (A)**. Footnotes 9, 10, 11.
**What GIPS says** — portfolios other than private market investment portfolios must be valued:

| Sub-item | Requirement | Effective |
|---|---|---|
| a | at least **monthly** | periods beginning on/after 1 Jan 2001; **at least quarterly** before that |
| b | as of **calendar month end or the last business day of the month** | periods beginning on/after 1 Jan 2010 |
| c | **on the date of all large cash flows** — and the firm/asset owner must define "large cash flow" for each composite or total fund | periods beginning on/after 1 Jan 2010 |

Recommendation **2.B.1 / 22.B.1 (B)** goes further: value on the date of **all** external cash flows, not just large ones.

**What `portable` must do** — `pt value` already builds snapshots for a date or range. The GIPS floor is monthly + month-end + large-flow dates. `portable` has daily prices available from fafnir and should **value daily** where prices exist, which satisfies the floor, satisfies the recommendation, and removes the need for any within-period approximation (see `PORT-GIPS-B06`). Monthly-only valuation must remain available and correct for historical periods where daily prices do not exist.
**Data model** — no change; `valuation_snapshot` is already per-account per-date.
**Test** — `test_valuation_frequency_floor`: for a fixture with a large flow mid-month, assert a snapshot exists at the prior day and the flow date; assert month-end snapshots exist for every month in the range; assert `pert` refuses to compute a period return if a required snapshot is missing rather than interpolating.

---

**`PORT-GIPS-A04` — Consistent annual valuation dates · CORE**

**Source** — Firms **2.A.22 (A)**, fn 8 (periods beginning on/after 1 Jan 2006); Asset Owners **22.A.19 (A)**.
**What GIPS says** — beginning and ending annual valuation dates must be consistent, and unless reporting on a non-calendar fiscal year, must fall at calendar year end or on the last business day of the year.
**What `portable` must do** — The portfolio's fiscal year is a stored property, defaulting to calendar. Annual period boundaries are derived from it and never from an ad-hoc `--as-of`.
**Data model** — `meta.fiscal_year_end` (default `12-31`).
**Test** — `test_annual_boundaries_are_stable`: annual returns computed on two different run dates for the same historical year are byte-identical.

---

**`PORT-GIPS-A05` — Trade-date accounting · CORE**

**Source** — Firms **2.A.9 (A)**, fn 6 (periods beginning on/after 1 Jan 2005); Asset Owners **22.A.6 (A)**.
**What GIPS says** — "Trade date accounting must be used." Settlement-date accounting is not permitted.
**Important currency note** — Historically, Q&A 4874 permitted recognition anywhere in the window trade date through T+3. **That Q&A is archived; its effective range ended 31 December 2019**, exactly at the 2020 edition boundary, and it has not been reissued. Under the 2020 edition there is no published T+3 accommodation. Treat strict trade-date recognition as the rule.
**What `portable` must do** — Nothing new. `CLAUDE.md` invariant 7 already mandates trade-date accounting with settlement dates recorded but not driving recognition. That invariant is now **stricter than the archived accommodation and exactly aligned with the current standard.** Say so in `docs/tax-methodology.md`.
**Test** — existing invariant tests suffice; add `test_settlement_date_does_not_move_recognition` if not already present.

---

**`PORT-GIPS-A06` — Accrual accounting for income · CORE**

**Source** — Firms **2.A.10 (A)**, **2.B.3 (B)**, **2.B.5 (B)**; Asset Owners **22.A.7 (A)**, **22.B.3 (B)**, **22.B.5 (B)**.
**What GIPS says** —

- **(A)** Accrual accounting **must** be used for fixed-income securities and all other investments earning interest income. Interest on cash and cash equivalents **may** be recognised on a cash basis. Accrued income **must be included in beginning and ending portfolio values** when performance is calculated.
- **(B)** Dividends **should** be accrued as of the **ex-dividend date**.
- **(B)** Returns **should** be net of **non-reclaimable** withholding taxes on dividends, interest, and capital gains; **reclaimable** withholding taxes should be accrued.

**Note the asymmetry** — interest accrual is a *requirement*; dividend accrual is only a *recommendation*. `portable` should adopt both, but must not describe dividend ex-date accrual as required.
**What `portable` must do** —
1. Accrued bond interest must be part of market value in every snapshot. `CLAUDE.md` already names this trap ("Accrued interest is part of market value for bonds"). This provision is its authority.
2. Accrue dividends on the **ex-date**, with cash arriving on the pay-date. `portable` already records both; the accrual must feed the valuation snapshot, not just the ledger.
3. Classify withholding taxes as reclaimable or non-reclaimable, accrue the reclaimable portion, and deduct the non-reclaimable portion from return.
**Data model** — `valuation_snapshot.accrued_income` (already specified). Add `transaction.withholding_reclaimable` (nullable Decimal) alongside the existing taxes-withheld field.
**Test** — `test_accrued_interest_in_market_value` (bond bought between coupons — already scenario 5 in the bootstrap's integration list; extend it to assert the snapshot, not just the ledger); `test_dividend_accrues_on_ex_date`: a dividend with ex-date inside the period and pay-date after it must appear in ending market value.

---

**`PORT-GIPS-A07` — Cash is always in the return · CORE**

**Source** — Firms **2.A.11 (A)**; Asset Owners **22.A.8 (A)**.
**What GIPS says** — "Returns from cash and cash equivalents must be included in all return calculations, even if the firm does not control the specific cash investment(s)."

Asset Owners **22.B.9 / 22.B.10 (B)** (added by errata **E2**) carve out the opposite direction: **operating cash** not fully available for investment *should not* be included in assets or returns.
**What `portable` must do** — Every account's cash balance and sweep instrument participates in market value and in return. There is no "ex-cash" return basis. If the owner ever designates an account or sub-balance as operating cash, that is an explicit, stored flag with a documented effect — never an implicit exclusion.
**Data model** — `account.cash_treatment` ∈ {`invested`, `operating`}, default `invested`.
**Test** — `test_cash_drag_is_reflected`: a portfolio holding 50% cash over a period in which equities rise must show approximately half the equity return, not the equity return.

---

**`PORT-GIPS-A08` — Total returns only · CORE**

**Source** — Firms **2.A.8 (A)**; Asset Owners **22.A.5 (A)**.
**What GIPS says** — "Total returns must be used" — realised and unrealised gains and losses plus income, for the measurement period.
**What `portable` must do** — No price-only return may ever be labelled a return. This binds benchmarks too (see `PORT-GIPS-G01`).
**Test** — `test_return_includes_income`: a period in which the only economic event is a dividend must produce a non-zero return.

---

**`PORT-GIPS-A09` — Estimated and preliminary values · CORE**

**Source** — Firms **2.A.21 (A)**; Asset Owners **22.A.18 (A)**.
**What GIPS says** — if the last available historical price or a preliminary/estimated value is used as fair value, it must be considered the best approximation of current fair value, and the firm must assess the difference against the final value and **adjust when the final value is received**.
**What `portable` must do** — This is the GIPS authority for `portable`'s price-staleness policy. A stale price is permitted only within tolerance and only when flagged; when a better price arrives, affected snapshots must be rebuilt (which `pt rebuild` already does, since snapshots are derived state). Beyond tolerance, exit code 5 as already specified.
**Data model** — `price.is_estimate` boolean; snapshot records the set of prices it consumed.
**Test** — `test_stale_price_then_correction_rebuilds`: introduce an estimate, compute a return, supply the final price, rebuild, and assert the return changed and the change is reported.

---

### 6.B — Time-weighted return

---

**`PORT-GIPS-B01` — TWR is the default and the primary · CORE**

**Source** — Firms **1.A.35 (A)**, **1.A.36 (A)**; Asset Owners **21.A.25 (A)**, **21.A.26 (A)**.
**What GIPS says** — For asset owners: "must present time-weighted returns for all total funds"; money-weighted returns may be presented **in addition**. For firms: TWR is mandatory unless a two-part gate is met (see `PORT-GIPS-C01`). The choice is made per composite/total fund and applied **consistently**.
**What `portable` must do** — `pert` presents TWR for the portfolio as the primary number, always. MWR is presented alongside, never instead. `CLAUDE.md` already carries the right instinct ("neither is more correct… Label every return with its method"); this provision settles the *ordering*: TWR leads.
**Test** — `test_tearsheet_leads_with_twr`; golden-file test on report ordering.

---

**`PORT-GIPS-B02` — External cash flow: the definition, and the multi-account trap · CORE**

**Source** — GIPS Glossary, **EXTERNAL CASH FLOW**: "Capital (cash or investments) that enters or exits a portfolio. **Dividend and interest income payments are not considered external cash flows.**"
**What GIPS says** — Income is not an external flow. There is **no** glossary term "internal cash flow"; movements within a portfolio simply are not external flows and therefore trigger neither revaluation nor sub-period return calculation.
**What `portable` must do** — This is the highest-risk item in the document, because `portable` is multi-account and the answer **depends on the level at which the return is being computed**:

| Event | External flow at *account* level? | External flow at *portfolio (total fund)* level? |
|---|---|---|
| Deposit from outside | yes | yes |
| Withdrawal to outside | yes | yes |
| Transfer between two accounts in the portfolio | **yes**, for both accounts (one out, one in) | **no** — it nets to zero |
| Cash dividend received | **no** | **no** |
| Bond coupon received | **no** | **no** |
| Reinvested dividend | **no** | **no** |
| Return of capital | **no** (it is income for flow purposes; it is a basis reduction for tax purposes — do not conflate) | **no** |
| Fee or margin interest paid | **no** — it is a cost, not a flow (see §6.D) | **no** |
| Stock distribution / in-kind transfer in or out | **yes**, valued at the time of distribution (Firms 2.A.29.c) | yes if it crosses the portfolio boundary |
| Option assignment converting to stock | **no** — an internal transformation | **no** |

Getting the transfer row wrong is the classic error: a $100k transfer between the owner's own accounts, treated as an external flow at portfolio level, produces a return that is arithmetically defensible and economically meaningless.

**`pt cash-flows --external-only` must therefore take a level argument**, and its current behaviour must be audited against this table before `pert` consumes it. Income must never appear in its output.
**Data model** — `transaction_type` needs a derived classification function, in `portable_core/domain/`, returning `{external, internal, income, cost}` **as a function of (transaction, level)**. This is business logic and belongs in `services/`, exposed to `pert` through a single call. It must not be re-derived at any call site.
**Test** — `test_flow_classification_matrix`: the table above, verbatim, as a parametrised test. `test_internal_transfer_is_flow_neutral_at_portfolio_level`: a transfer between two accounts must leave the portfolio-level TWR bit-identical to a run without it, while changing both account-level returns.

---

**`PORT-GIPS-B03` — Large cash flows and sub-period returns · CORE**

**Source** — Firms **2.A.23.c (A)** (valuation), **2.A.24.c (A)** (return calculation), both fn: periods beginning on/after 1 Jan 2010. Asset Owners **22.A.20.c**, **22.A.21.c**.
**What GIPS says** — the portfolio must be valued on the date of **all** large cash flows, and a sub-period return calculated at the time of all large cash flows **if daily returns are not calculated**. GIPS defines the *term* `LARGE CASH FLOW` in its glossary (see §10) but supplies **no threshold**: the firm or asset owner **must define the amount for each composite or total fund**, in terms of a currency value or a percentage of assets.
**What `portable` must do** —
1. Make the large-cash-flow threshold an explicit, stored, **effective-dated** configuration value on the portfolio (and later, per composite). It must never default silently. A missing definition is an error, not a zero.
2. When daily returns are computed — which is `portable`'s default given daily prices — the sub-period requirement is automatically satisfied and the threshold becomes informational. **Say this in the report**, because a reader cannot otherwise tell.
3. When daily returns are not available for a period, revalue on large-flow dates and chain-link.
**Data model** — new table `return_policy(effective_from, large_flow_basis ENUM('amount','percent'), large_flow_value TEXT /* Decimal */, significant_flow_value TEXT NULL, …)`. Effective-dating matters for the same reason tax rates are effective-dated: a policy change must not retroactively restate history.
**Test** — `test_large_flow_threshold_required`: computing a TWR with no policy row in force for the period exits non-zero with a specific error code (propose `PT-E-GIPS-NO-FLOW-POLICY`). `test_daily_returns_subsume_subperiod_rule`: for a fixture with a large flow, the daily-chained return and the sub-period-at-flow-date return agree to a stated tolerance.

---

**`PORT-GIPS-B04` — Geometric linking · CORE**

**Source** — Firms **2.A.24.f (A)**; Asset Owners **22.A.21.f (A)**.
**What GIPS says** — "Geometrically link periodic and sub-period returns."
**What `portable` must do** — `R = ∏(1 + rᵢ) − 1`. Never sum sub-period returns; never average them arithmetically. Compute in `Decimal` throughout — this is a return path, so `CLAUDE.md` invariant 1 applies and there is no float anywhere in it.
**Test** — property test: for any generated sequence of sub-period returns, the linked return equals the direct start-to-end return when there are no flows, to within the documented rounding tolerance.

---

**`PORT-GIPS-B05` — Consistent methodology · CORE**

**Source** — Firms **2.A.24.g (A)**, **2.A.16 (A)**; Asset Owners **22.A.21.g (A)**, **22.A.13 (A)**.
**What GIPS says** — the calculation methodology for an individual portfolio must be applied consistently, and performance must be calculated in accordance with the documented calculation policy.
**What `portable` must do** — The method used for each period is **recorded on the result**, not inferred at render time. A single reported multi-year return may legitimately be chained from daily sub-periods in recent years and monthly Modified Dietz sub-periods in early years; the report must be able to say so.
**Data model** — `return_result.method` and `return_result.method_by_subperiod` (or a per-subperiod detail table).
**Test** — `test_method_recorded_per_subperiod`; golden-file test asserting the method footnote renders.

---

**`PORT-GIPS-B06` — Approximation when daily returns are unavailable · CORE**

**Source** — Firms **2.A.24.d (A)**, fn 15: required for periods beginning on/after **1 Jan 2005**. Asset Owners **22.A.21.d (A)**.
**What GIPS says** — for external cash flows that are **not** large cash flows, the firm must "calculate portfolio returns that adjust for **daily-weighted external cash flows**, if daily returns are not calculated."

**The 2020 edition names no method.** Neither "Modified Dietz" nor "Modified IRR" appears anywhere in the provisions of either standard — confirmed by direct search. The provisions specify the required *outcome*; the methods that satisfy it are named in the now-**archived** *Guidance Statement on Calculation Methodology* (2011), which remains the last place CFA Institute wrote the formula down:

```
             V_E  -  V_B  -  SUM_i( CF_i )                    D - D_i
   MD  =  ---------------------------------- ,      w_i  =  -----------
             V_B  +  SUM_i( CF_i * w_i )                          D
```

where `V_B` and `V_E` are the beginning and ending values, `CF_i` is the value of cash flow *i*, `D` is the number of **calendar** days in the period, and `D_i` is the number of calendar days from the start of the period to flow *i*. Note calendar days, not business days.

**What `portable` must do** — Implement Modified Dietz exactly as above, in `Decimal`, and use it **only** as the gap-day fallback the roadmap already describes (`pert` backlog item 1). Cite the archived Guidance Statement, not a provision number, when documenting the method name — the provision requires the property, not the formula.
**Test** — `test_modified_dietz_matches_reference`: known worked examples. `test_modified_dietz_equals_twr_without_flows`: with no flows, Modified Dietz reduces to the simple period return exactly.

---

**`PORT-GIPS-B07` — Do not annualise sub-one-year returns · CORE**

**Source** — Firms **2.A.12 (A)**; Asset Owners **22.A.9 (A)**; and in advertisements, Firms **8.A.4 (A)** / Asset Owners **26.A.4 (A)**.
**What GIPS says** — "Returns for periods of less than one year must not be annualized." Unconditional, and it applies to portfolio-level as well as composite-level returns.
**What `portable` must do** — `CLAUDE.md` already lists this as a domain trap. It is now a cited requirement with a provision number. Enforce it in the **formatter layer** so that no call site can bypass it: a return object carries its period length, and any attempt to render an annualised figure for a period under one year raises.
**Interaction** — this governs the MWR too: the since-inception MWR is annualised only once the since-inception period exceeds one year (see `PORT-GIPS-C03`).
**Test** — `test_subyear_return_never_annualized`: parametrised over 1 day, 1 month, 364 days, 365 days, 366 days; assert the 364-day case is not annualised and the 366-day case is.

---

### 6.C — Money-weighted return

---

**`PORT-GIPS-C01` — The MWR eligibility gate · CORE**

**Source** — Firms **1.A.35 (A)**.
**What GIPS says** — 1.A.35 opens by mandating time-weighted returns, then states the money-weighted carve-out. The operative second half reads: "The firm may present money-weighted returns **only if** the firm has control over the external cash flows into the portfolios in the composite or pooled fund **and** the portfolios in the composite have or the pooled fund has **at least one** of the following characteristics: a. Closed-end b. Fixed life c. Fixed commitment d. Illiquid investments as a significant part of the investment strategy."

Both limbs must hold. Note that GIPS **never requires** MWR — it is always an option, never an obligation.

**What `portable` must do** — Apply the gate honestly to the owner's own portfolio. Limb (i) is satisfied: the owner controls his own contributions and withdrawals. Limb (ii) is **not** satisfied by an open-ended, liquid, personal taxable portfolio. Under a GIPS-modelled regime, therefore:

> **MWR may be presented for this portfolio, but only alongside TWR — never as the headline or the sole return.**

This is the right answer economically as well as formally: the MWR is the owner's actual experience and is the number he cares about; the TWR is the number that lets him compare himself to a manager or an index. `pert` presents both, always labelled (`PORT-GIPS-I04`).
**Data model** — none.
**Test** — `test_mwr_never_presented_alone`: a report configuration requesting MWR only either adds TWR or refuses.

---

**`PORT-GIPS-C02` — Since-inception, annualised, daily flows · CORE**

**Source** — Firms **2.A.29 (A)**; Asset Owners **22.A.23 (A)**. Footnote 17: daily external cash flows required for periods beginning on/after **1 January 2020**; before that, quarterly or more frequent.
**What GIPS says** — when calculating money-weighted returns the firm must (a) calculate **annualised since-inception** money-weighted returns; (b) calculate them using **daily external cash flows**; (c) include **stock distributions as external cash flows, valued at the time of distribution**; and value portfolios at least annually and as of the period end for any period for which performance is calculated (**2.A.28 / 22.A.22**).
**What `portable` must do** — The canonical MWR is the **annualised since-inception IRR**, driven by the daily external-cash-flow series from `PORT-GIPS-B02` plus the ending market value. Period MWRs (YTD, 3-year) may be computed and are useful, but must be labelled as such and must not be presented as *the* MWR. In-kind distributions and transfers are flows valued on their distribution date.
**Data model** — the flow series must be day-resolution. `valuation_snapshot` already records external cash flows and their timing — confirm it stores the flow date, not just the period.
**Test** — `test_si_mwr_uses_daily_flows`: two flow series differing only in the day within a month must produce different SI-MWRs. `test_stock_distribution_is_a_flow`.

---

**`PORT-GIPS-C03` — Sub-one-year MWR is not annualised · CORE**

**Source** — Firms **5.A.1.b (A)**, **7.A.1.b (A)**; Asset Owners **25.A.1.b (A)**; general rule Firms 2.A.12 / AO 22.A.9.
**What GIPS says** — "When the composite has a track record that is less than a full year, the **non-annualized** since-inception money-weighted return through the initial annual period end."
**What `portable` must do** — Falls out of `PORT-GIPS-B07` if the period length travels with the result. Assert it explicitly anyway, because the SI-MWR is the one place where the natural implementation (`XIRR`) returns an annualised rate by construction and must be *de*-annualised or refused.
**Test** — `test_si_mwr_under_one_year_not_annualized`.

---

**`PORT-GIPS-C04` — Convergence and failure · CORE (`portable`-specific)**

**Source** — none. GIPS is silent on solver behaviour.
**What `portable` must do** — This is a gap in the standard that `CLAUDE.md`'s "fail loudly on ambiguity" invariant fills. Newton with a bisection fallback, a documented bracket, an iteration cap, and — on non-convergence or multiple sign changes in the flow series — **refuse and explain**, exit non-zero. Never return a plausible root from a pathological flow pattern. Record the solver, iteration count, and residual on the result.
**Test** — `test_xirr_refuses_pathological_flows`: a flow series with multiple IRRs exits with a specific error code and names the flows responsible.

---

**`PORT-GIPS-C05` — MWR aggregation is by cash flow, not by averaging returns · EXT**

**Source** — Firms **2.A.39 (A)**; Asset Owners **22.A.29 (A)**.
**What GIPS says** — composite money-weighted returns must be calculated "by aggregating the portfolio-level information for those portfolios included in the composite."
**What `portable` must do** — When a portfolio-level MWR is built from multiple accounts, aggregate the **flow series and the market values**, then solve once. Do **not** asset-weight the per-account MWRs — that is a different and wrong number. This applies today at the account→portfolio level, not only to future composites, so treat it as CORE in practice even though the provision is written for composites.
**Test** — `test_portfolio_mwr_is_aggregated_not_averaged`: construct two accounts whose asset-weighted MWR average differs materially from the aggregated solve; assert `portable` reports the aggregated figure.

---

### 6.D — Fees, costs, and return bases

This is where the Asset Owner standards diverge most sharply from the Firms standards, and where `portable` needs a schema change before `pert` starts.

---

**`PORT-GIPS-D01` — The three return bases · CORE**

**Source** — Asset Owners **22.A.24, 22.A.25, 22.A.26 (A)**, all effective for periods beginning on/after 1 Jan 2015. Firms analogues: **2.A.37, 2.A.38 (A)** and the glossary.

**What GIPS says** — the Asset Owner standards define **three** return bases, where the Firms standards define two. The ladder:

| Basis | Deducts | Provision |
|---|---|---|
| **Gross-of-fees** | transaction costs; **all fees and expenses of externally managed pooled funds** | 22.A.26 |
| **Net-of-external-costs-only** | the above, **plus** investment management fees for externally managed segregated accounts | 22.A.25 |
| **Net-of-fees** | the above, **plus** the asset owner's own internal **investment management costs** | 22.A.24 |

The Firms ladder is shorter: gross-of-fees = "the return on investments reduced by any transaction costs"; net-of-fees = "the gross-of-fees return reduced by investment management fees."

Three consequences that are easy to get wrong:

1. **Fees and expenses of externally managed pooled funds are deducted at *every* tier, including gross.** They are embedded in NAV and cannot be added back. A return that adds them back is a "full gross-of-fees" return and, under Asset Owners **24.A.4 (A)**, **must be labelled supplemental information**.
2. **Custody fees are treated differently by the two regimes, and this is a real fork.** Under the **Firms** ladder, custody is an *administrative fee* — glossary: "All fees other than transaction costs and the investment management fee. Administrative fees may include custody fees, accounting fees, auditing fees, consulting fees, legal fees, performance measurement fees, and other related fees" — and administrative fees are subtracted by neither the gross nor the net definition. Under the **Asset Owner** ladder, custody falls inside `INVESTMENT MANAGEMENT COSTS`, which the November 2020 errata (**E2**) expressly amended to include custody fees, and which 22.A.24 requires to be deducted at **net-of-fees**. `portable` follows the Asset Owner treatment, per §4.4. In **both** regimes the transaction-based component of a custody fee is **not** a transaction cost.
3. For asset owners, **net-of-fees is the required presentation for total funds** (24.A.1.b, effective for periods beginning on/after 1 Jan 2015) — the inverse of the Firms convention, where gross-of-fees is the workhorse. Gross and net-of-external-costs-only are recommended additions (24.B.1).

**What `portable` must do** — Classify every fee-bearing transaction into exactly one of four buckets, and derive the three bases from that classification:

| Bucket | Examples in `portable` | Gross | Net-ext-only | Net |
|---|---|---|---|---|
| **Transaction cost** | commissions, exchange fees, SEC/TAF fees, the bid-ask component of a bundled fee | ✓ | ✓ | ✓ |
| **Embedded pooled-fund fee** | ETF and mutual-fund expense ratios (implicit in NAV) | ✓ | ✓ | ✓ |
| **External investment management fee** | an advisor's fee on a separately managed account | — | ✓ | ✓ |
| **Internal investment management cost** | custody fees, data and market-data subscriptions, investment research, performance-measurement tooling — the owner's own cost of running the portfolio (AO 22.A.24.d, as amended by errata **E2**) | — | — | ✓ |
| **Other administrative fee** | account maintenance, wire fees, tax preparation, legal — costs not attributable to managing the investments | — | — | — |

Margin interest is a **financing cost**, not a fee — GIPS is silent, and `portable` should treat it as a cost that reduces return in all three bases (it reduces net asset value directly), with a disclosure saying so.

For a self-managed portfolio with no external manager, gross-of-fees and net-of-external-costs-only will be **numerically identical**. Report them once, labelled, rather than printing two identical columns.

**Data model** — **schema change, needed before `pert`:** add `fee_class` to the transaction/fee model with the enum above, defaulting to nothing (`NULL` must be an error, not a silent "administrative"). Add a `return_basis` enum to `return_result`. Migration required, per `CLAUDE.md`'s schema-change rules.
**Test** — `test_fee_classification_exhaustive`: every fee-bearing transaction type in the fixture corpus has a classification; a `NULL` classification fails the build. `test_custody_fee_reduces_net_only`: adding a custody fee must leave the gross-of-fees and net-of-external-costs-only returns unchanged and must reduce the net-of-fees return. `test_other_admin_fee_reduces_no_gips_basis`: a wire fee changes market value but must not change any of the three reported bases — it appears only in the supplemental "net of all costs" figure.

---

**`PORT-GIPS-D02` — Transaction costs must be deducted; estimates only where actuals are unknown · CORE**

**Source** — Firms **2.A.13 (A)**; Asset Owners **22.A.10 (A)**.
**What GIPS says** — "All returns must be calculated after the deduction of transaction costs incurred during the period. The firm may use estimated transaction costs **only for those portfolios for which actual transaction costs are not known**."
**What `portable` must do** — `portable` records actual fees per transaction, so estimation should never be needed. If an estimate is ever used, it must be flagged and the estimation method disclosed (`PORT-GIPS-I09`). A "pure gross-of-fees" return that ignores transaction costs is **not a GIPS return type** for asset owners at all; under the Firms wrap-fee regime it exists only as **supplemental information** and must be labelled as such (Firms 4.A.17).
**Test** — `test_transaction_costs_always_deducted`; `test_estimated_costs_are_flagged`.

---

**`PORT-GIPS-D03` — Bundled fees · EXT**

**Source** — Firms **2.A.14 (A)**; Asset Owners **22.A.11 (A)**; *Guidance Statement on Wrap Fee Portfolios* (2021).
**What GIPS says** — where transaction costs cannot be estimated or segregated from a bundled fee, gross-of-fees returns must be reduced by the entire bundled fee or by the portion containing the transaction costs; net-of-fees returns by the entire bundled fee or the portion containing transaction costs plus the investment management fee. The glossary defines a bundled fee as one that "combines multiple fees into one total or 'bundled' fee… any combination of investment management fees, transaction costs, custody fees, and/or administrative fees."
**What `portable` must do** — Not applicable to a self-directed brokerage portfolio. **Refuse rather than approximate**: if a fee is entered without a classification and cannot be decomposed, error out. Revisit if the owner ever holds a wrap or bundled-fee account.
**Test** — n/a until the feature exists; the `NULL` classification failure in `PORT-GIPS-D01` covers the refusal.

---

**`PORT-GIPS-D04` — Returns are net of leverage · CORE**

**Source** — Firms **2.A.2.b, 2.A.15 (A)**; Asset Owners **22.A.2.b, 22.A.12 (A)**.
**What GIPS says** — assets and returns must be calculated **net of discretionary leverage** and "not grossed up as if the leverage did not exist."
**What `portable` must do** — A margin account's market value is assets minus the margin loan. Do not report a gross exposure figure as market value. Margin interest reduces return.
**Data model** — the margin loan must be a modelled liability with a balance, not a memo field.
**Test** — `test_market_value_net_of_margin_loan`.

---

**`PORT-GIPS-D05` — Underlying pooled fund fees · CORE**

**Source** — Firms **2.A.17 (A)**; Asset Owners **22.A.14 (A)**.
**What GIPS says** — for portfolios invested in underlying pooled funds, all returns must reflect the deduction of all fees and expenses charged at the underlying pooled fund level.
**What `portable` must do** — Nothing active: ETF and fund NAVs and prices are already net of expenses, so this is satisfied automatically. **Document that it is satisfied automatically**, so nobody later "corrects" for expense ratios and double-counts them. That would be a silently wrong number of exactly the class this repo fears most.
**Test** — a documentation assertion in `docs/output-formats.md`; no code test.

---

### 6.E — Composites and aggregation · EXT (deferred to v1.0)

`portable` has no composites today, and asset owners are not required to have them. This subsection records what becomes binding **if** the v1.0 "Performance composites" backlog item is built, so the schema does not have to be redesigned then.

| ID | Source | Requirement | Consequence for `portable` |
|---|---|---|---|
| `PORT-GIPS-E01` | AO **23.A.4 (A)** | Composites must be defined by investment mandate, objective, or strategy, and must include **all** portfolios meeting the definition | No cherry-picking. A composite definition is a stored predicate, and membership is derived from it, not hand-assigned. |
| `PORT-GIPS-E02` | AO **23.A.5 (A)**; Firms **3.A.6 (A)** | Changes to a composite definition **must not be applied retroactively** | Composite definitions are effective-dated, like tax rates. |
| `PORT-GIPS-E03` | AO **23.A.6 (A)**; Firms **3.A.7 (A)** | New portfolios added on a **timely and consistent** basis once funded | Inclusion lag is a stored policy value, not a per-case judgement. |
| `PORT-GIPS-E04` | AO **23.A.7 (A)**; Firms **3.A.9 (A)** | Terminated portfolios remain in the historical record **through the last full measurement period they were managed** — permanently | Survivorship bias is prohibited by construction. `portable`'s append-only ledger already makes this natural. |
| `PORT-GIPS-E05` | AO **23.A.8 (A)**; Firms **3.A.10 (A)** | Portfolios must not be switched between composites except on a documented mandate change or a composite redefinition; **historical performance stays with the original composite** | Membership is an effective-dated interval, and history is never rewritten. |
| `PORT-GIPS-E06` | AO **23.A.2, 23.A.3 (A)** | Multiple total funds on the **same** strategy may be presented separately or combined; different strategies **must** be reported separately | Relevant if the owner ever adds a second `.port` file for a distinct mandate. |
| `PORT-GIPS-E07` | Firms **2.A.36 (A)**; AO **22.A.28 (A)** | Composite TWR must use one of: (a) asset-weight portfolio returns by **beginning-of-period values**; (b) asset-weight by a method reflecting **beginning-of-period values and external cash flows**; (c) the **aggregate method** | Three permitted approaches, no preference expressed. Pick one, record it in an ADR, apply it consistently, disclose it. |
| `PORT-GIPS-E08` | Firms **3.A.15–3.A.18 (A)** | Carve-outs must carry cash and related income (separately accounted or **synthetically allocated**); must be representative of a standalone portfolio; must be created for **all** portfolios managed to that strategy; standalone portfolios need their own composite | This is what an "equity sleeve" report is under GIPS. Do not build a carve-out report without the cash allocation — a segment return without cash is not a GIPS return. **No effective-date footnote attaches to 3.A.15–3.A.18** — the widely-repeated "1 January 2020" is the *edition's* effective date, not a carve-out rule. |
| `PORT-GIPS-E09` | Firms **3.A.12, 3.A.13 (A)** | Portfolios removed for **significant cash flows** require an **ex ante, composite-specific** definition, consistently followed; **temporary new accounts must not be included in composite performance** | **"Significant" and "large" are different, independently defined thresholds.** Large → revalue and compute a sub-period return. Significant → temporarily remove the portfolio from the composite. A flow can be one without being the other. Do not model them with a single field. |
| `PORT-GIPS-E10` | Firms **3.A.19 (A)** | Different composites, funds, or carve-outs must **not** be combined to create a simulated strategy presented as a composite | Bars the "here's what my model portfolio would have returned" report unless it is labelled theoretical supplemental information (`PORT-GIPS-I11`). |

---

### 6.F — Risk measures

---

**`PORT-GIPS-F01` — Three-year annualised ex-post standard deviation · CORE**

**Source** — Asset Owners **24.A.1.j (A)** (wording fixed by errata **E2**), footnote: required for periods ending on or after **1 January 2011**. Firms **4.A.1.j (A)**, footnote 9, same date.
**What GIPS says** — for total funds/composites for which **monthly returns are available**, present the **three-year annualised ex-post standard deviation, using monthly returns**, of **both** the total fund/composite **and the benchmark**, as of each annual period end.
**What `portable` must do** —
1. Compute σ from **36 monthly returns**. Not daily returns scaled by √252. Not weekly. **Monthly.** This is the single most commonly botched detail in performance reporting, and GIPS is explicit about it.
2. Annualise by ×√12.
3. Compute the **benchmark's** σ the same way, from the same 36 months, and present both.
4. The trigger is *monthly returns being available*, not the fund having a three-year history per se.
**Data model** — a monthly return series must be materialised and rebuildable, not recomputed ad hoc.
**Test** — `test_stddev_from_monthly_returns`: assert against a hand-computed 36-month series; assert a daily-based σ is *not* what is reported. `test_stddev_benchmark_same_basis`.

---

**`PORT-GIPS-F02` — Disclose when 36 monthly returns are unavailable · CORE**

**Source** — Asset Owners **24.C.30 (A)** (added by errata **E2**); Firms **4.C.36 (A)**.
**What GIPS says** — for funds/composites with at least **three annual periods** of performance, disclose if the three-year annualised ex-post standard deviation is not presented **because 36 monthly returns are not available**.
**What `portable` must do** — Emit the disclosure automatically. Never render a blank cell, an em-dash, or a zero where σ is unavailable — `CLAUDE.md`'s "explicit null, never let blank and zero mean the same thing" rule and this provision are the same rule.
**Note** — the 2010 edition's requirement to substitute an alternative risk measure when σ is inappropriate is **gone**. In 2020 an additional risk measure is only a *recommendation* (Firms 4.B.5 / AO 24.B.7). The only hard obligation when σ is unavailable is the disclosure.
**Test** — `test_missing_stddev_emits_disclosure`.

---

**`PORT-GIPS-F03` — Additional risk measures: matched periodicity and methodology · CORE**

**Source** — Firms **2.A.18 (A)**; Asset Owners **22.A.15 (A)**.
**What GIPS says** — when calculating additional risk measures: (a) the **periodicity** of the portfolio/composite returns and the benchmark returns must be the same; (b) the risk-measure **calculation methodology** must be the same for both.
**What `portable` must do** — `pert`'s risk backlog (Sharpe, Sortino, information ratio, Treynor, Jensen's alpha, beta, M², max drawdown) must compute portfolio and benchmark figures from a single shared code path over a single shared periodicity. A beta computed from daily portfolio returns against monthly benchmark returns is not merely imprecise — it is prohibited.
**Data model** — `risk_measure_result` carries `periodicity` and `method_id`; a mismatch between the portfolio and benchmark legs is an assertion failure, not a warning.
**Test** — `test_risk_measure_periodicity_parity`: attempting to compute beta with mismatched periodicities raises.

---

**`PORT-GIPS-F04` — Which return basis feeds the risk measures · CORE**

**Source** — Firms **2.B.7 (B)**, **4.C.44 (A)**; Asset Owners **22.B.7 (B)**, **24.C.35 (A)**.
**What GIPS says** — the firm **should** use gross-of-fees returns when calculating risk measures `(B)`; and **must disclose** which return basis was used `(A)`. The disclosure is three-way under the Asset Owner standards (24.C.35: gross-of-fees, net-of-external-costs-only, or net-of-fees) and two-way under the Firms standards (4.C.44: gross-of-fees or net-of-fees), because `NET-OF-EXTERNAL-COSTS-ONLY` is an asset-owner-only basis.
**What `portable` must do** — Default to gross-of-fees for risk measures, make it configurable, and always disclose the choice on the tearsheet.
**Test** — golden-file test on the disclosure line.

---

**`PORT-GIPS-F05` — Name the risk-free rate · CORE**

**Source** — Firms **4.C.43.b (A)**; Asset Owners **24.C.34 (A)**.
**What GIPS says** — describe each additional risk measure, and **disclose the name of the risk-free rate** if one is used in its calculation.
**What `portable` must do** — Sharpe, Sortino, Treynor, Jensen's alpha and M² all consume a risk-free rate. The specific series — which fafnir tenor, from which curve, as of which date — must travel with the result and appear in the report. "The risk-free rate" is not an acceptable disclosure; "3-month US Treasury bill, secondary market, from fafnir `ref` curve, monthly average" is.
**Data model** — `risk_measure_result.risk_free_source` (provider, series id, tenor, as-of).
**Test** — `test_risk_free_rate_named_in_output`.

---

### 6.G — Benchmarks

---

**`PORT-GIPS-G01` — Total-return benchmarks only; price-only benchmarks are prohibited · CORE**

**Source** — Firms **1.A.18, 1.A.19 (A)**; Asset Owners **21.A.15 (A)**; Firms **8.A.11 (A)** / AO **26.A.7 (A)** for advertisements.
**What GIPS says** — the benchmark must reflect the investment mandate, objective, or strategy, and **price-only benchmarks are prohibited** in GIPS reports. Benchmark returns must be **total returns**.
**What `portable` must do** — This is a real and easy trap: the S&P 500 *price index* is far more readily available than the *total return index*, and using it understates the benchmark by roughly the dividend yield every year — flattering the portfolio by ~1.5–2% annually. `portable` must:
1. Store, per benchmark, whether the series is price-only or total-return.
2. **Refuse** to use a price-only series as a benchmark in a performance report, with a specific error, rather than warning.
3. Have `FafnirProvider` resolve total-return series where available and report clearly when only a price series exists.
**Data model** — `benchmark.return_type` ∈ {`total_return`, `price_only`}, `NOT NULL`, no default.
**Test** — `test_price_only_benchmark_refused`: exit non-zero with `PT-E-GIPS-PRICE-ONLY-BENCHMARK`.

---

**`PORT-GIPS-G02` — Benchmark, or a disclosed absence · CORE**

**Source** — Asset Owners **24.A.1.f (A)**, **24.C.25 (A)**; Firms **4.A.1.e (A)**, **4.C.31 (A)**.
**What GIPS says** — present the benchmark total return for each annual period and for every other period for which fund returns are presented, **unless the firm determines there is no appropriate benchmark** — in which case it must **disclose why no benchmark is presented**.
**What `portable` must do** — There is no silent omission. Every return column either has a benchmark column beside it or carries a stated reason for having none.
**Test** — `test_no_benchmark_requires_reason`.

---

**`PORT-GIPS-G03` — The blended policy benchmark · CORE**

**Source** — Asset Owners **24.C.28 (A)** — the asset-owner-specific provision; also **24.C.27 (A)** (custom benchmarks) and **24.C.29 (A)** (portfolio-weighted custom benchmarks). Firms analogues **4.C.33, 4.C.34 (A)**.
**What GIPS says** — where the total fund benchmark is a **blend of asset class benchmarks weighted by policy weights**, disclose **each asset class benchmark with its weight as of the most recent annual period end**, plus general information about how those benchmarks are structured. For custom benchmarks generally: disclose components, weights, the **rebalancing process**, and the calculation methodology, and **clearly label it as a custom benchmark**.
**What `portable` must do** — A blended benchmark is a first-class stored object, not a formula in a config string: components, effective-dated weights, and an explicit rebalancing rule (calendar frequency or drift bands). The report renders the composition table automatically. This directly specifies `pert` backlog item 4 ("blended benchmarks with rebalancing rules").
**Data model** — `benchmark_component(benchmark_id, instrument_or_index_id, weight, effective_from)` plus `benchmark.rebalance_rule`.
**Test** — `test_blend_rebalancing_is_explicit`: a monthly-rebalanced blend and an unrebalanced (drifting) blend over the same period produce different returns, and the report names which was used.

---

**`PORT-GIPS-G04` — Benchmark changes · CORE**

**Source** — Asset Owners **24.C.26 (A)**; Firms **4.C.32 (A)**.
**What GIPS says** — disclose the date and description of any benchmark change. A **prospective** change must be disclosed for as long as returns for the prior benchmark remain in the report; a **retroactive** change for a minimum of one year and as long as it is relevant.
**What `portable` must do** — Benchmark assignment is effective-dated and its history is queryable. A benchmark change generates a disclosure entry with a start date and an expiry rule.
**Data model** — `portfolio_benchmark(benchmark_id, effective_from, change_reason, change_type ENUM('prospective','retroactive'))`.
**Test** — `test_benchmark_change_disclosed_for_required_duration`.

---

**`PORT-GIPS-G05` — Benchmark periodicity and withholding taxes · CORE**

**Source** — Asset Owners **24.C.5 (A)** (periodicity), **24.C.21 (A)** (withholding); Firms **4.C.5, 4.C.26 (A)**.
**What GIPS says** — disclose the benchmark description (key features, or the name of a readily recognised index) **and the periodicity of the benchmark if benchmark returns are calculated less frequently than monthly**; and disclose whether benchmark returns are net of withholding taxes, if that information is available.
**What `portable` must do** — Store both facts on the benchmark record and render them.
**Test** — golden-file test on the benchmark disclosure block.

---

### 6.H — Report contents

What a GIPS Asset Owner Report must contain, and therefore what a `pert` tearsheet should contain in order to be *modelled on* one. Because `portable` makes no compliance claim, these are **`portable` design requirements**, not obligations — but a tearsheet that omits them is less informative than the standard it borrows from, which defeats the purpose.

**`PORT-GIPS-H01` — Periods presented · CORE**

**Source** — Asset Owners **24.A.1.a (A)**; contrast Firms **4.A.1.a (A)**.
**What GIPS says** — asset owners: **at least one year** of compliant performance (or since inception if shorter), then **add one year each year until a minimum of ten years** is presented. Firms: at least **five** years initially, building to ten.
**What `portable` must do** — Show every annual period since inception, capped at nothing. The tearsheet's calendar-year table is the natural home. `pert` backlog item 3 (MTD/QTD/YTD/1/3/5/10-year/since-inception) satisfies and exceeds this.

**`PORT-GIPS-H02` — Annual returns, and the partial first and last periods · CORE**

**Source** — Asset Owners **24.A.1.c, .d, .e (A)**. (Note: 24.A.1.**b** is the separate net-of-fees requirement for total funds — see `PORT-GIPS-D01`. The sub-letters are easy to transpose.)
**What GIPS says** — returns for each annual period; where the initial period is less than a full year, the return **from the inception date through that period end**; and on termination, the return **from the last annual period end through the termination date**.
**What `portable` must do** — Stub years are computed from the actual inception date, not from 1 January, and are labelled as partial and **not annualised** (`PORT-GIPS-B07`).
**Test** — `test_partial_first_year_labelled_and_unannualized`.

**`PORT-GIPS-H03` — Assets alongside returns · CORE**

**Source** — Asset Owners **24.A.1.g, .h, .i (A)**; footnotes: (g) and (i) required for periods ending on/after 31 Dec 2020. Firms **4.A.1.f–h (A)**, fn 5 on total firm assets.
**What GIPS says** — number of total funds or portfolios in the composite; total fund or composite assets; total asset owner assets — each **as of each annual period end**.
**What `portable` must do** — Every annual return row carries ending market value and the number of accounts contributing. A return without the asset base beside it is uninterpretable.

**`PORT-GIPS-H04` — Label everything · CORE**

**Source** — Asset Owners **24.A.3 (A)**; Firms **4.A.3 (A)**, **5.A.5 (A)**.
**What GIPS says** — clearly label or identify the **periods presented** and **the return basis**. Asset Owners 24.A.3 gives the three-way choice (gross-of-fees, net-of-external-costs-only, net-of-fees); the Firms analogue 4.A.3 is two-way (gross-of-fees or net-of-fees), since the middle basis exists only in the Asset Owner regime.
**What `portable` must do** — In `--format json`, the return basis is a field on every return object, not a report-level footnote. In `table` and `markdown`, it appears in the column header. `CLAUDE.md` already says "Label every return with its method"; extend that to **method *and* basis *and* period**.

**`PORT-GIPS-H05` — Level-5 valuation percentage · EXT**

**Source** — Asset Owners **24.A.2 (A)**; Firms **4.A.2 (A)**.
**What GIPS says** — present the percentage of total fund or composite assets valued using **subjective, unobservable inputs** (hierarchy level (e)) as of the most recent annual period end, **if such investments represent a material amount**.
**What `portable` must do** — Compute it from `price.valuation_level` (`PORT-GIPS-A02`). For a listed-securities portfolio it will be zero; render it anyway, because "0%" is information and a missing row is not.

**`PORT-GIPS-H06` — Track-record breaks must not be linked · CORE**

**Source** — Asset Owners **24.A.6 (A)**; Firms **4.A.5 (A)**.
**What GIPS says** — if a composite loses all its member portfolios the track record **ends**; if portfolios are added later it **restarts**. Both periods are presented with the break clearly shown, and performance before the break **must not be linked** to performance after it.
**What `portable` must do** — If the portfolio goes fully to zero market value with no positions and no cash, and later restarts, `pert` must break the chain rather than link through the gap. Chain-linking across an empty period silently manufactures a return.
**Test** — `test_track_record_break_not_linked`.

**`PORT-GIPS-H07` — One currency · CORE**

**Source** — Asset Owners **24.A.7 (A)**; Firms **4.A.12 (A)**.
**What GIPS says** — all required and recommended information in a report must be presented in the **same currency**.
**What `portable` must do** — Already satisfied: USD-only in v0.1, with a currency column carried for future use. When multi-currency arrives (backlog, P1), this provision becomes the constraint that a single report is single-currency, with translation done once and disclosed.

**`PORT-GIPS-H08` — Supplemental information · CORE**

**Source** — Asset Owners **24.A.8 (A)**, **24.A.4 (A)**; Firms **4.A.18 (A)**, **4.A.17 (A)**.
**What GIPS says** — supplemental information must (a) relate directly to the fund or composite, (b) **not contradict or conflict with** required or recommended information, and (c) be **clearly labelled as supplemental information**. A "full gross-of-fees" return — one that adds back embedded pooled-fund fees — must be identified as supplemental (24.A.4). So must theoretical performance (24.C.37) and, for firms, pure gross-of-fees wrap returns (4.A.17).
**What `portable` must do** — The tearsheet has a structurally distinct supplemental section. Anything `portable` computes that GIPS would not recognise as a return — after-tax returns, model or backtested results, pure gross returns, sleeve returns without allocated cash — lives there, labelled.
**Data model** — `return_result.is_supplemental` boolean; formatters must render it, not just store it.
**Test** — `test_supplemental_section_is_labelled`; `test_after_tax_returns_are_supplemental`.

---

### 6.I — Disclosure

The disclosure requirements are the largest block in GIPS (Firms 4.C.1–4.C.48; AO 24.C.1–24.C.37). Most concern firm identity, fee schedules, and prospective-client mechanics that have no analogue here. Those that translate:

| ID | Source (AO / Firms) | Disclosure | `portable` obligation |
|---|---|---|---|
| `PORT-GIPS-I01` | 24.C.4 / 4.C.4 | **Description** of the total fund or composite | Portfolio description is a stored, required field, rendered on every report. |
| `PORT-GIPS-I02` | 24.C.5 / 4.C.5 | **Benchmark description** and periodicity | See `PORT-GIPS-G05`. |
| `PORT-GIPS-I03` | 24.C.9 / 4.C.9 | **Reporting currency** | Rendered even when trivially USD. |
| `PORT-GIPS-I04` | 24.C.6–.8 / 4.C.6–.7 | Which fees are and are not deducted at the stated return basis | Auto-generated from the fee classification in `PORT-GIPS-D01`, not hand-written prose. |
| `PORT-GIPS-I05` | 24.C.10 / 4.C.13 | **Inception date** | Already in `meta`. |
| `PORT-GIPS-I06` | 24.C.13 / 4.C.16 | Policies for **valuing investments, calculating performance, and preparing reports** are available | `pert` reports link to `docs/gips-standard.md` (this document) and to the effective `return_policy` row. |
| `PORT-GIPS-I07` | 24.C.14 / 4.C.17 | Historical use of **leverage, derivatives, and short positions**, if material | Derivable: `portable` knows whether options, shorts, or margin were used in each period. Auto-generate; do not rely on the owner remembering. |
| `PORT-GIPS-I08` | 24.C.16 / 4.C.19 | **Significant events** that help interpret the report — minimum one year, and as long as relevant | A `portfolio_event(date, description, expiry)` table. Manual entry, but prompted for. |
| `PORT-GIPS-I09` | 24.C.15 / 4.C.18 | **Estimated transaction costs** — that they were used, and how determined | See `PORT-GIPS-D02`. |
| `PORT-GIPS-I10` | 24.C.32 / 4.C.41 | Use of **preliminary or estimated values** as fair value | From `price.is_estimate`. |
| `PORT-GIPS-I11` | 24.C.37 / 4.C.48 | **Theoretical performance** presented as supplemental: that it is theoretical and not based on actual assets; the methodology and assumptions; whether actual or estimated fees are reflected; clearly labelled supplemental | Binds any backtest, model portfolio, or optimiser-proposed result that `po` might produce. |
| `PORT-GIPS-I12` | 24.C.20 / 4.C.25 | Whether returns are **gross or net of withholding taxes**, if material | From `PORT-GIPS-A06`. |
| `PORT-GIPS-I13` | 24.C.33 / 4.C.42 | Any **change in the type of return presented** (e.g. MWR → TWR) — minimum one year | Rare here, but cheap to support given effective-dated policy rows. |
| `PORT-GIPS-I14` | 24.C.31 / 4.C.38 | Any change resulting from correction of a **material error** — minimum one year after correction | See `PORT-GIPS-J02`. |
| `PORT-GIPS-I15` | 24.C.34–.35 / 4.C.43–.44 | Description of each **additional risk measure**, the **risk-free rate name**, and which return basis fed the risk measures | See `PORT-GIPS-F04`, `F05`. |
| `PORT-GIPS-I16` | 24.C.30 / 4.C.36 | σ not presented because **36 monthly returns unavailable** | See `PORT-GIPS-F02`. |
| `PORT-GIPS-I17` | 24.C.17–.19 / 4.C.21–.23 | **Redefinition** of the reporting entity or composite, and **name changes** — date and description | Effective-dated `meta` history. |

**Implementation note.** These disclosures should be **generated from state**, not typed into a template. Every one of `PORT-GIPS-I02`, `I04`, `I07`, `I09`, `I10`, `I12`, `I15`, `I16` is derivable from data `portable` already holds. A disclosure block that is generated cannot drift out of sync with the numbers above it; one that is hand-maintained inevitably will. Build a `DisclosureEngine` in `services/` that takes a report and returns an ordered list of disclosure items with stable IDs.

---

### 6.J — Error correction, records, and integrity

---

**`PORT-GIPS-J01` — Materiality is a policy, not a number GIPS supplies · CORE**

**Source** — Firms **1.A.20, 1.A.21 (A)** (as amended by errata **E1**); Asset Owners **21.A.16 (A)**. Glossary, **MATERIAL ERROR**: "An error in a GIPS composite report, GIPS pooled fund report, or GIPS asset owner report that must be corrected and disclosed in a corrected report."
**What GIPS says** — the definition is deliberately circular. **There is no numeric materiality threshold anywhere in the provisions.** The firm or asset owner must define materiality itself, in its policies (1.A.5.a / 21.A.6). GIPS then prescribes only the *distribution* obligation on correction.
**What `portable` must do** — Materiality is a stored, effective-dated configuration value with a documented basis (propose: an absolute basis-point threshold on any presented return, plus an absolute currency threshold on market value, whichever binds first). `pert` compares a rebuilt report against the previously issued one and classifies the difference. A tool that silently reissues a changed number is worse than one that has no error handling at all.
**Data model** — `report_issue(report_id, issued_at, content_hash, superseded_by)`. The content hash makes this cheap and exact, and `portable`'s determinism invariant is what makes the hash meaningful.
**Test** — `test_material_error_detected_on_reissue`.

---

**`PORT-GIPS-J02` — Correction is additive, never destructive · CORE**

**Source** — Firms **1.A.20 (A)**; Asset Owners **21.A.16 (A)**; disclosure at 24.C.31 / 4.C.38.
**What GIPS says** — the corrected report must be provided to the current verifier, to current clients / the oversight body, and to former verifiers and current prospects who received the erroneous version. There is **no obligation** to former clients, former investors, or former prospects (this narrowing is precisely what errata **E1** clarified). The correction must be **disclosed** in the corrected report for a minimum of one year.
**What `portable` must do** — Corrections are new report issues that reference the superseded one, carrying a machine-readable diff of what changed and why. This is the same discipline `CLAUDE.md` invariant 2 imposes on the ledger — a mistake is corrected with a new entry, never by editing history — extended from transactions to reports.
**Test** — `test_correction_preserves_superseded_report`.

---

**`PORT-GIPS-J03` — Supporting data must be captured and maintained · CORE**

**Source** — Firms **1.A.25, 1.A.26 (A)**; Asset Owners **21.A.19, 21.A.20 (A)**.
**What GIPS says** — "All data and information necessary to support all items included in [GIPS reports] must be captured, maintained, and available within a reasonable time frame, **for all periods presented**." And: the entity is responsible for its own claim and **must ensure that records and information provided by any third party meet the requirements** — you cannot outsource data integrity to a data vendor.
**Retention period** — there is **no general books-and-records retention period in years** anywhere in Section 1. 1.A.25 says "for all periods presented." The only five-year figure is 1.A.22.a (terminated composites remaining on the composite list) and AO 21.A.17. Do not repeat the folk claim that GIPS requires five- or seven-year retention of underlying records; it does not.
**What `portable` must do** — This is the provision that justifies `portable`'s entire architecture. The append-only ledger, the price cache with per-price source and as-of, the recorded provider for every valuation, and `pt rebuild` replay together constitute exactly the record this provision demands. The second sentence — third-party records — is why `providers/fafnir.py` must record *which warehouse rows produced a price*, not merely the price.
**Test** — the existing ledger-replay determinism test is the acceptance criterion. Add `test_every_snapshot_price_has_source_and_asof`.

---

**`PORT-GIPS-J04` — Do not link theoretical to actual · CORE**

**Source** — Firms **1.A.27 (A)**; Asset Owners **21.A.21 (A)**.
**What GIPS says** — "The firm must not link actual performance to historical theoretical performance."
**What `portable` must do** — Binds `po` directly. An optimiser backtest may be shown; it may not be chained onto the live track record to produce a single continuous series. The `is_supplemental` flag from `PORT-GIPS-H08` and a hard guard in the chain-linking code, not merely a convention.
**Test** — `test_theoretical_cannot_chain_to_actual`: attempting to link a result flagged theoretical to a live series raises.

---

**`PORT-GIPS-J05` — Prohibited compliance language, enforced by lint · CORE**

**Source** — Firms **1.A.8, 1.A.9, 1.A.10 (A)**; Asset Owners **21.A.9, 21.A.10 (A)**.
**What GIPS says** — verbatim, and this is the important one:

> **1.A.9 (A)** — "Statements referring to the calculation methodology as being **'in accordance,' 'in compliance,' or 'consistent' with the Global Investment Performance Standards**, or similar statements, are prohibited."

> **1.A.8 (A)** — a firm not meeting all requirements must not state it is "in compliance with the Global Investment Performance Standards except for…" or make any other statement indicating compliance or **partial compliance**.

> **1.A.10 (A)** — must not describe a client's performance as "calculated in accordance with the Global Investment Performance Standards," except where a compliant firm reports to its own current clients.

**A finding worth flagging.** `portable_bootstrap_prompt.md` §4.7 and the `pert` backlog both use the phrase **"GIPS-consistent methodology."** That phrasing is, by name, the construction 1.A.9 prohibits. The prohibition is addressed to firms, and `portable`'s owner is not a firm, so no rule is being broken — but the phrase is prohibited *precisely because CFA Institute judges it misleading*, and a tool built to this standard should not adopt the one form of words the standard singles out. This is a case where the design document is wrong and should be changed.

**Recommended replacement wording**, for `docs/`, the README, and every report footer:

> Returns in this report are calculated using methodology **modelled on** the Global Investment Performance Standards (2020 edition), published by CFA Institute. This is **not** a claim of compliance with the GIPS standards, and this report has **not** been prepared in accordance with them. GIPS compliance is an entity-wide assertion that requires firm- or asset-owner-wide policies, records, and — as best practice — independent verification, and it cannot be made for a single portfolio or by an individual. GIPS® is a registered trademark of CFA Institute, which does not endorse this tool.

**What `portable` must do** — Add a lint rule, alongside the existing no-float-in-money-paths rule, that fails the build on any of these patterns in source, docs, templates, or test fixtures:

```
(?i)\b(in\s+)?(compliance|accordance|conformity)\s+with\s+(the\s+)?GIPS
(?i)\bGIPS[- ]compliant\b
(?i)\bGIPS[- ]consistent\b
(?i)\bconsistent\s+with\s+(the\s+)?GIPS\b
(?i)\bcomplies\s+with\s+(the\s+)?GIPS\b
```

Allow-list exactly one location: the disclaimer text above, and this document. Do not silence the rule anywhere else.
**Test** — `test_no_prohibited_gips_language`: the lint rule runs in `make lint` and in CI; a fixture file containing each prohibited phrase must fail it.

---

**`PORT-GIPS-J06` — Determinism as evidence · CORE (`portable`-specific)**

**Source** — no GIPS provision; derived from 1.A.25 / 21.A.19 and from `CLAUDE.md` invariant 6.
**Rationale** — GIPS demands that every reported figure be supportable. `portable`'s determinism guarantee — same inputs, identical bytes out — is a stronger property than GIPS asks for, and it is what makes `PORT-GIPS-J01`'s content-hash approach to error detection work at all. Record it as the reason the invariant exists, so nobody later trades it away for convenience.

---

## 7. What GIPS does not cover

Three things `portable` cares about are outside the standard entirely. Knowing this prevents the opposite errors of inventing a GIPS rule that does not exist, and of assuming silence means prohibition.

### 7.1 After-tax performance — outside GIPS since 2010

**"After-tax" and "after tax" appear nowhere in the 2020 Firms or Asset Owners standards.** The only tax content is withholding taxes (`PORT-GIPS-A06`). After-tax reporting was removed at the **2010** edition — not 2020 — and ownership transferred to the US country sponsor, because tax rules are jurisdiction-specific.

The governing reference is therefore the **USIPC After-Tax Performance Standards** ([PDF](https://www.gipsstandards.org/wp-content/uploads/2024/10/usipc-after-tax-performance-standards.pdf)), originally effective 1 January 2006 and revised effective **1 January 2011**. Its status is worth reading precisely:

- Firms, whether or not they claim GIPS compliance, are **encouraged** to comply — so presenting after-tax results at all is optional (this paraphrases; the exact encouragement wording was not recovered verbatim and is listed at §11.2 G7);
- but firms are **"expected to adhere"** to the standards **when calculating and presenting performance results after the effects of taxes** — so the methodology is not a free-for-all once you choose to present them;
- and adherence is **not** a condition of claiming GIPS compliance.

It also requires that "for each after-tax composite, all input data, calculation, composite construction, disclosure and presentation must follow the requirements as prescribed in the Global Investment Performance Standards" — i.e. after-tax sits *on top of* the GIPS machinery rather than replacing it.

**Consequences for `portable`:**

1. After-tax returns are **supplemental information** under `PORT-GIPS-H08`, always labelled.
2. `pert` backlog item 8 ("After-tax performance… following AIMR/GIPS after-tax guidance") should be **re-scoped** to cite the USIPC standards explicitly, and to note that they are US-specific, voluntary, and last revised fifteen years ago. The phrase "AIMR/GIPS after-tax guidance" points at a lineage that no longer exists as such.
3. `portable`'s after-tax capability is a genuine differentiator precisely *because* the global standard abandoned this ground. It should be built well and described honestly — and it inherits `CLAUDE.md`'s existing caveats: not tax advice, not a substitute for a 1099-B, and (until the v0.2 backlog item lands) not wash-sale aware.
4. After-tax reporting ranked third in practitioner polling at the 2025 GIPS Standards Conference for topics wanted in future guidance. Nothing is in exposure draft. Watch for it; do not wait for it.

### 7.2 Attribution

GIPS has **no attribution requirements**. Brinson-Fachler, Carino, Menchero — none of these are GIPS concepts. The only relevant CFA Institute work is an **exposure draft** *Guide for Best Practices in Return Attribution Reporting* (comment period closed 12 December 2025) which states on its face that it "is not authoritative guidance for the GIPS standards"; I could not confirm whether a final version has been published. `pert` backlog item 6 should therefore cite the practitioner literature (Brinson-Hood-Beebower, Brinson-Fachler, Carino, Menchero) and its own ADR, **not** GIPS. Attribution results are supplemental information.

### 7.3 Wash sales, holding periods, and everything else in `docs/tax-methodology.md`

Entirely outside GIPS. The domain traps listed in `CLAUDE.md` — holding-period boundaries, short sales always short-term, spinoff basis allocation, option premium on assignment, wash sales crossing accounts — are US tax law, not performance measurement. Keep the two documents separate. The one place they touch is `PORT-GIPS-A06` (ex-date vs. pay-date), where GIPS supplies the accrual rule and the tax code supplies the entitlement rule, and they happen to agree.

---

## 8. Conformance summary

What `pert` v0.2 must satisfy before it emits a return:

| Area | CORE items | Status |
|---|---|---|
| Valuation | A01–A09 | A01, A04, A05, A09 largely satisfied by v0.1; **A02 (valuation level), A06 (dividend accrual into snapshots), A07 (`cash_treatment`) need work** |
| TWR | B01–B07 | **All new in v0.2. B02 (flow classification) is the highest-risk item in the whole document.** |
| MWR | C01–C05 | All new in v0.2 |
| Fees | D01–D05 | **D01 requires a schema change before `pert` starts** |
| Risk | F01–F05 | All new in v0.2 |
| Benchmarks | G01–G05 | All new in v0.2; **G01 (price-only refusal) should land with the benchmark schema** |
| Reporting | H01–H08 | All new in v0.2 |
| Disclosure | I01–I17 | `DisclosureEngine`, new in v0.2 |
| Integrity | J01–J06 | **J05 (compliance-language lint) should land now, in v0.1** |
| Composites | E01–E10 | EXT — v1.0 |

**Three things to do before `pert` work begins**, in priority order:

1. **`PORT-GIPS-J05`** — the compliance-language lint, plus correcting "GIPS-consistent" in `portable_bootstrap_prompt.md`, `CLAUDE.md`, and the `pert` backlog issues. Cheapest item here, and the one that prevents a claim shipping.
2. **`PORT-GIPS-D01`** — the `fee_class` schema change and migration. Retrofitting a fee taxonomy after returns exist means restating every published number.
3. **`PORT-GIPS-B02`** — audit `pt cash-flows --external-only` against the classification matrix and move the classification into a single service function. Everything in §6.B and §6.C consumes it; if it is wrong, every return is wrong in a way that looks entirely plausible.

Each warrants an ADR. `PORT-GIPS-E07` (composite aggregation method) and `PORT-GIPS-B06` (Modified Dietz as the documented fallback) will each need one when their features land.

---

## 9. The compliance boundary

Consolidated here so it can be quoted directly into the README, `docs/output-formats.md`, and every report footer.

### 9.1 What is true

- `portable` implements calculation, valuation, and disclosure methodology **modelled on** the 2020 GIPS standards.
- Where `portable` departs from that methodology, the departure is recorded in this document with a reason.
- Where GIPS is silent (after-tax, attribution, solver behaviour), `portable` says so rather than implying GIPS coverage.

### 9.2 What must never be said

Per Firms 1.A.8 / 1.A.9 / 1.A.10 and Asset Owners 21.A.9 / 21.A.10, and enforced by the lint rule in `PORT-GIPS-J05`:

| Prohibited | Why |
|---|---|
| "GIPS-compliant" | Compliance is entity-wide and cannot be claimed for a portfolio (1.A.1 / 21.A.1) |
| "in compliance with the GIPS standards" | Same |
| "in compliance with the GIPS standards except for…" | Partial compliance is expressly prohibited (1.A.8) |
| "calculated in accordance with the GIPS standards" | Expressly prohibited outside a compliant firm reporting to its own clients (1.A.9, 1.A.10) |
| "**GIPS-consistent**" / "consistent with the GIPS standards" | Named verbatim in the prohibition at 1.A.9 |
| "GIPS-verified" | Verification is an engagement performed by an independent verifier on an entity |
| Any implication that CFA Institute endorses `portable` | The required trademark language explicitly disclaims endorsement |

### 9.3 The standard footer

Every `pert` report, in every output format, carries:

> Returns are calculated using methodology modelled on the Global Investment Performance Standards (2020 edition), published by CFA Institute. This is not a claim of compliance with the GIPS standards, and this report has not been prepared in accordance with them. GIPS compliance is an entity-wide assertion requiring firm- or asset-owner-wide policies, records, and independent verification as best practice; it cannot be made for a single portfolio or by an individual. GIPS® is a registered trademark of CFA Institute. CFA Institute does not endorse or promote this tool, nor does it warrant the accuracy or quality of its output.

In `--format json` this is a field in the envelope (`disclaimer`), not a rendered string, so it cannot be dropped by a consumer without noticing.

---

## 10. Working glossary

Terms as GIPS uses them, with the `portable` mapping. **Verbatim** entries are quoted from a CFA Institute glossary; **derived** entries are reconstructed from provision text because the source glossary could not be retrieved (see §11.2).

| Term | GIPS definition | Source | `portable` |
|---|---|---|---|
| **Asset owner** | "An entity that manages investments, directly and/or through the use of external managers, on behalf of participants, beneficiaries, or the organization itself." | verbatim, AO 21.A.2 | The owner — but an *individual*, so outside the definition |
| **Total fund** | A pool of assets managed according to a specific investment mandate, typically spanning multiple asset classes | derived | The `.port` file |
| **Composite** | "An aggregation of one or more portfolios or total funds that are managed according to a similar investment mandate, objective, or strategy." | verbatim | v1.0 backlog |
| **Additional composite** | "A grouping of portfolios representing a particular strategy or asset class that the asset owner chooses to present." | verbatim, AO Handbook | v1.0 backlog |
| **Oversight body** | "Those who have direct oversight responsibility for total fund assets and total asset owner assets." | verbatim | The owner |
| **External cash flow** | "Capital (cash or investments) that enters or exits a portfolio. Dividend and interest income payments are not considered external cash flows." | verbatim | `PORT-GIPS-B02` |
| **Large cash flow** | "The level at which the firm or asset owner determines that an external cash flow may distort the return if the portfolio or total fund is not valued and a sub-period return is not calculated. The firm or asset owner must define the amount in terms of the value of cash/asset flow or in terms of a percentage of the portfolio assets, composite assets, or total fund assets." | verbatim | `return_policy.large_flow_*` |
| **Significant cash flow** | The level at which a client-directed external cash flow may temporarily prevent implementation of the strategy — defined ex ante, per composite | derived (archived GS) | v1.0; **distinct from large cash flow** |
| **Time-weighted return** | "A method of calculating period-by-period returns that reflects the change in value and negates the effects of external cash flows." | verbatim | Primary return |
| **Money-weighted return** | A return reflecting the change in value and the timing and size of external cash flows | derived | Secondary return, always alongside TWR |
| **Total return** | "The rate of return that includes the realized and unrealized gains and losses plus income for the measurement period." | verbatim | The only return type permitted |
| **Fair value** | Broadly, the amount at which an investment could be exchanged in an arm's-length transaction between willing parties in an orderly transaction; the Firms edition layers in the 2.B.6 hierarchy | **derived — firms-edition wording not recovered** | `PORT-GIPS-A01` |
| **Gross-of-fees** (firms) | "For firms, the return on investments reduced by any transaction costs." | verbatim | `PORT-GIPS-D01` |
| **Net-of-fees** (firms) | "For firms, the gross-of-fees return reduced by investment management fees." | verbatim | `PORT-GIPS-D01` |
| **Net-of-external-costs-only** (asset owners) | Gross, plus deduction of investment management fees for externally managed segregated accounts | derived, AO 22.A.25 | Numerically equal to gross for a self-managed portfolio |
| **Investment management costs** | "All **internal** costs for both internally and externally managed assets. In addition to costs for portfolio management, they may also involve overhead and other related costs and fees, including data valuation fees, investment research services, custody fees, pro rata share of overhead… and performance measurement and compliance services." | verbatim (as amended by errata E2) | Deducted only at net-of-fees; note this AO-specific term **includes custody fees**, unlike the Firms treatment |
| **Administrative fee** | "All fees other than transaction costs and the investment management fee. Administrative fees may include custody fees, accounting fees, auditing fees, consulting fees, legal fees, performance measurement fees, and other related fees." | verbatim | Not deducted from gross or net **in the Firms ladder** |
| **Custody fee** | "The fee payable to the custodian for the safekeeping of portfolio assets. Custody fees are considered to be administrative fees and typically contain an asset-based portion and a transaction-based portion." | verbatim | The transaction-based portion is **not** a transaction cost |
| **Bundled fee** | "A fee that combines multiple fees into one total or 'bundled' fee. Bundled fees can include any combination of investment management fees, transaction costs, custody fees, and/or administrative fees." | verbatim, Wrap Fee GS | `PORT-GIPS-D03` |
| **Private market investments** | "Includes real assets (e.g., real estate and infrastructure), private equity, and similar investments that are illiquid, not publicly traded, and not traded on an exchange." | verbatim | Not held; triggers the quarterly-valuation branch if ever held |
| **Material error** | "An error in a GIPS composite report, GIPS pooled fund report, or GIPS asset owner report that must be corrected and disclosed in a corrected report." | verbatim | Circular by design — the entity defines materiality (`PORT-GIPS-J01`) |
| **Must / Required** | "A provision, task, or action that is mandatory or required to be followed or performed." | verbatim | `(A)` in this document |
| **Should / Recommended** | "…recommended to be followed or performed and is considered to be best practice but is not required." | verbatim | `(B)` in this document |

**Note the fee-treatment divergence.** In the Firms ladder, custody is an administrative fee and is deducted from neither gross nor net. In the Asset Owner ladder, custody falls inside **investment management costs** (per the errata-amended glossary) and *is* deducted at net-of-fees. `portable` follows the Asset Owner treatment, consistent with §4.4, and must say so in `PORT-GIPS-I04`. This is exactly the kind of divergence that produces two defensible numbers from one portfolio — disclosure is the only defence.

---

## 11. Verification register

Every citation in this document was obtained from CFA Institute sources by direct retrieval, cross-checked across at least two independent passes where the provision is load-bearing. This section records what was contested and how it was settled, and what remains open. **It is part of the document, not an appendix to be skipped** — the point of a golden source is that its provenance is auditable.

### 11.1 Contested points, now settled

| Question | Verdict | Evidence |
|---|---|---|
| Does AO 24.A.1 require a measure of **internal dispersion**? | **No.** 24.A.1 runs a–j; sub-item (i) is *total asset owner assets*. | Confirmed against the AO Reports Comparison (C2) on two passes, one asking the yes/no question directly. The confusion arises because Firms **4.A.1.i** *is* internal dispersion — the sub-letters do not align between regimes. Dispersion is also meaningless at total-fund level. |
| How many variants does the AO compliance statement **24.C.1** have? | **Three** — (a) verified, (b) verified with a performance examination, (c) not verified. | C2, with the (b) variant recovered verbatim. Corroborated by the confirmed three-variant structure of 25.C.1 and by the verifier standards confirming AO performance examinations exist. |
| Is there an effective-date footnote restricting **carve-outs with allocated cash** to periods on or after 1 Jan 2020? | **No.** No footnote attaches to 3.A.15–3.A.18. | Two passes over S1 enumerating footnote status per provision. The 2020 date is the *edition's* effective date; cash allocation was prohibited under the 2010 edition and re-permitted by the 2020 edition, which is where the folk rule comes from. |
| Firms **4.A.1.h** and **4.A.1.j** footnotes | **Confirmed verbatim.** (h): required for periods ending on/after 31 Dec 2020, with an earlier-period alternative of composite assets as a percentage of total firm assets. (j): required for periods ending on/after 1 Jan 2011. | C1 |
| Is the **T+3** trade-date accommodation current? | **No.** Q&A 4874 permitted recognition at T through T+3, but its effective range ended **31 December 2019** and it is marked Archived. It has not been reissued under the 2020 edition. | Q&A database entries 4874, 5319, 5311, 5328 |
| Does the 2020 edition name **Modified Dietz**? | **No.** Neither Modified Dietz nor Modified IRR appears in the provisions of either standard. | Direct search of S1 and S2. The formula survives only in the archived 2011 Calculation Methodology Guidance Statement. |
| Is there a **2025 or 2026 edition** of the GIPS standards? | **No.** The 2020 edition is current. | Firms, Asset Owners, Work in Process, and Archived Standards pages; the April and July 2026 GIPS newsletters. The 2025–26 activity is at the *supporting document* level: verifier standards for asset owners and for fiduciary managers (both effective 1 Jan 2026) and the OCIO Guidance Statement (effective 31 Dec 2025) — all built on the 2020 framework. |
| Does any GIPS provision address **after-tax** returns? | **No.** The only tax content is withholding taxes. | Direct search of S1 and S2 for "after-tax" / "after tax" |
| Is the sub-one-year **MWR** presented non-annualised? | **Yes**, confirmed verbatim at Firms 5.A.1.b, Firms 7.A.1.b, AO 25.A.1.b, plus the general rules Firms 2.A.12 / AO 22.A.9 | C1, C2, S1, S2 |
| AO reporting frequency | **Confirmed.** 21.A.11.b requires an updated report to the oversight body at least once every 12 months; 21.B.2 recommends quarterly. | S2 |

### 11.2 Open gaps

These do not block any requirement in §6, but they should be closed before this document is ratified. **Every one requires reading a page of a printed or locally-opened PDF** — the glossaries and appendices sit at the back of the documents and are not reachable by automated text conversion, which truncates first.

| # | Gap | Impact | How to close |
|---|---|---|---|
| G1 | **Fourteen glossary terms not recovered verbatim**: `DISCRETIONARY`, `TOTAL FIRM ASSETS`, `MONEY-WEIGHTED RETURN`, `CARVE-OUT`, `TRANSACTION COSTS`, `TRADING EXPENSES`, `SIGNIFICANT CASH FLOW`, `TEMPORARY NEW ACCOUNT`, `FAIR VALUE` (firms wording), `TOTAL FUND`, `TRADE DATE ACCOUNTING`, `NET-OF-EXTERNAL-COSTS-ONLY`, `TOTAL POOLED FUND FEES`, `PERFORMANCE-BASED FEE` (full entry) | Low. All are used derivatively in §6 and each is flagged as *derived* in §10. `TRANSACTION COSTS` and `FAIR VALUE` are the two worth getting exactly right, since `PORT-GIPS-D01` and `A01` turn on them. | Read the glossary of S1 (from ~p. 72) and S2 |
| G2 | **Appendices A–D of S1 and S2 not read** — sample GIPS Composite Reports, sample Asset Owner Reports, sample advertisements, sample lists | Medium for `pert`'s tearsheet design. The samples show exact column layouts, note ordering, and footnote wording — the best available model for `PORT-GIPS-H01`–`H08`. | Read S1 Appendix A and S2 Appendices A–B before designing the tearsheet |
| G3 | **Archival status of the 2011 Calculation Methodology Guidance Statement** | Low. The formula in `PORT-GIPS-B06` is solid; only whether CFA Institute formally designates the document superseded is unresolved. It sits in the pre-2020 guidance directory and is listed among archived standards, but carries no archival stamp on its face. | GIPS helpdesk (`gips@cfainstitute.org`) |
| G4 | **Whether a final *Guide for Best Practices in Return Attribution Reporting* has been published** — the exposure draft's comment period closed 12 Dec 2025 and the Work in Process page still shows only the draft | Low; affects §7.2 only | Re-check the Work in Process page before `pert` attribution work starts (v0.2) |
| G5 | **Trade Error Guidance Statement** — reported as in development at the 2025 GIPS Standards Conference (third-party source), but absent from Work in Process with no exposure draft found | Low; would touch `PORT-GIPS-J01`–`J02` if published | Watch the quarterly GIPS newsletter |
| G6 | **AO footnote dates on 22.A.20 / 22.A.21 not independently confirmed.** `PORT-GIPS-A03` and `PORT-GIPS-B06` present one effective-date table covering both regimes, citing the Firms footnote numbers (9, 10, 11, 15). The sub-item structure of the AO provisions is identical and the dates are almost certainly the same, but the AO-side footnotes were not read. | Low — the dates only matter for historical periods before 2010 | Read S2 §22 footnotes |
| G7 | **USIPC encouragement wording.** §7.1 paraphrases "firms are encouraged to comply". The verbatim phrase recovered from the USIPC document is "expected to adhere"; the encouragement sentence was not recovered word-for-word. | Low — the substantive status claims are confirmed | Read the USIPC PDF's status section |
| G8 | **Firms Section 5–8 provision numbers** rest on CFA Institute's own comparison documents (C1) rather than on S1 directly, because S1's automated conversion truncates at 5.A.1.d | Low for `portable` — Sections 5–8 concern composites, pooled funds, and advertising, none of which `portable` implements | Read S1 pp. ~40–70 if composites are built |

### 11.3 A note on method

Two retrieval failures are worth recording because they will recur:

1. **Automated PDF conversion truncates all five GIPS standards documents before their glossaries.** Anything sourced from a glossary in this document came from a *different* GIPS edition's copy of the shared glossary (the Verifiers and Fiduciary Management Provider editions convert further) and is marked accordingly.
2. **One retrieval pass fabricated an entire section**, returning Firms Section 4's disclosure list renumbered as `5.C.x` — detectable only because it included a significant-cash-flow disclosure, which cannot exist in a money-weighted-return report. It was discarded. This is the reason every load-bearing provision number here was confirmed on at least two independent passes, and the reason §11.1 exists at all. **If you extend this document, hold to that standard.**

---

## 12. Bibliography

**Standards**
- CFA Institute, [*Global Investment Performance Standards (GIPS®) for Firms*, 2020 edition](https://www.gipsstandards.org/wp-content/uploads/2021/03/2020_gips_standards_firms.pdf) · [Errata, July 2020](https://www.gipsstandards.org/wp-content/uploads/2021/03/errata_gips_standards_for_firms.pdf)
- CFA Institute, [*GIPS® for Asset Owners*, 2020 edition](https://www.gipsstandards.org/wp-content/uploads/2021/02/2020_gips_standards_asset_owners.pdf) · [Errata, November 2020](https://www.gipsstandards.org/wp-content/uploads/2021/03/errata_november_2020_gips_standards_for_asset_owners.pdf)
- CFA Institute, [*GIPS® for Verifiers*, 2020 edition](https://www.gipsstandards.org/wp-content/uploads/2021/03/2020_gips_standards_verifiers.pdf)

**Handbooks and comparisons**
- CFA Institute, [*GIPS Standards Handbook for Firms*](https://www.gipsstandards.org/standards/gips-standards-for-firms/gips-standards-handbook-for-firms/)
- CFA Institute, [*GIPS Standards Handbook for Asset Owners*](https://www.gipsstandards.org/wp-content/uploads/2021/03/gips_standards_handbook_for_asset_owners.pdf)
- CFA Institute, [*GIPS Reports for Firms: Comparison of Sections 4–7*](https://www.gipsstandards.org/wp-content/uploads/2025/04/gips_standards_firms_report_comparison-1.pdf)
- CFA Institute, [*GIPS Asset Owner Reports: Comparison*](https://www.gipsstandards.org/wp-content/uploads/2025/04/gips_asset_owners_report_comparison.pdf)

**Guidance Statements in force**
- [Benchmarks for Firms](https://www.gipsstandards.org/wp-content/uploads/2023/08/gs_benchmarks_firms.pdf) · [Benchmarks for Asset Owners](https://www.gipsstandards.org/wp-content/uploads/2023/04/guidance-statement-benchmarks-asset-owners.pdf) · [Wrap Fee Portfolios](https://www.gipsstandards.org/wp-content/uploads/2021/09/gs_wrap_fee_portfolios.pdf) · [Overlay Strategies](https://www.gipsstandards.org/wp-content/uploads/2022/01/gs_overlay_2022.pdf) · [OCIO Portfolios](https://www.gipsstandards.org/wp-content/uploads/2024/12/gs-for-ocio-porfolios.pdf) · [Firms Managing Only BDPFs](https://www.gipsstandards.org/wp-content/uploads/2023/12/gs-firms-managing-only-bdpf.pdf) · [Verifier Independence](https://www.gipsstandards.org/wp-content/uploads/2021/03/verifier_independence_gs_2020.pdf)

**Archived but cited**
- CFA Institute, [*Guidance Statement on Calculation Methodology*, 2011](https://www.gipsstandards.org/wp-content/uploads/2021/03/calculation_methodology_gs_2011.pdf) — source of the Modified Dietz formula
- CFA Institute, [*Guidance Statement on the Application of the GIPS Standards to Asset Owners*, 2018](https://www.gipsstandards.org/wp-content/uploads/2021/03/asset_owner_gs_2018.pdf) — source of the "not to individuals" statement

**Outside GIPS**
- USIPC, [*After-Tax Performance Standards*](https://www.gipsstandards.org/wp-content/uploads/2024/10/usipc-after-tax-performance-standards.pdf), revised effective 1 January 2011

**Reference**
- CFA Institute, [GIPS Q&A Database](https://www.gipsstandards.org/standards/q-a-database/) — authoritative; always check Status and effective-date range
- CFA Institute, [GIPS Standards home](https://www.gipsstandards.org/) · [Work in Process](https://www.gipsstandards.org/standards/work-in-process/) · [Archived Standards](https://www.gipsstandards.org/standards/archived-standards/)

---

## 13. Change control

This document is subject to the same rule as `CLAUDE.md`: **when it is wrong, fix it in the same commit that makes it wrong.**

- A change to any `PORT-GIPS-xxx` requirement requires an ADR if it changes an implementation obligation, and a `CHANGELOG.md` entry either way.
- `PORT-GIPS-xxx` identifiers are never reused or renumbered. Superseded requirements are marked superseded in place, with a pointer to their replacement.
- Re-verify the golden source register (§2) **annually, before 30 June** — the date by which a compliant entity would file its CFA Institute notification, and a convenient forcing function. Check for a new edition, new or withdrawn guidance statements, and errata.
- Close the §11.2 gaps before promoting this document from Draft to ratified.
- When any requirement here is implemented, link the test name into the **Test** line so §6 doubles as a coverage map.
