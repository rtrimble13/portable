"""The one approved form of words about the GIPS standards.

`docs/gips-standard.md` §9.3 fixes this text. It is defined here, once, so that
every report in every output format carries the identical string and so the
compliance-language lint rule (``PORT-GIPS-J05``) has exactly one thing to
allow-list.

Do not edit this text without an ADR and a `CHANGELOG.md` entry. Do not
paraphrase it at a call site: import it.
"""

from __future__ import annotations

from typing import Final

#: The standard footer. Carried by every command that emits a return, and by
#: every ``pert`` report in every output format. In ``--format json`` this is an
#: envelope *field* (``disclaimer``), never a rendered string, so a consumer
#: cannot drop it without noticing (``CLAUDE.md``, "Output").
GIPS_DISCLAIMER: Final[str] = (
    "Returns are calculated using methodology modelled on the Global Investment "
    "Performance Standards (2020 edition), published by CFA Institute. This is not a "
    "claim of compliance with the GIPS standards, and this report has not been prepared "
    "in accordance with them. GIPS compliance is an entity-wide assertion requiring "
    "firm- or asset-owner-wide policies, records, and independent verification as best "
    "practice; it cannot be made for a single portfolio or by an individual. GIPS® is "
    "a registered trademark of CFA Institute. CFA Institute does not endorse or promote "
    "this tool, nor does it warrant the accuracy or quality of its output."
)

#: How the disclaimer must be wrapped when a formatter lays it out for a human.
#:
#: ``break_on_hyphens=False`` is not cosmetic. The compliance-language lint rule
#: allow-lists this text by matching its tokens in order, and Python's default
#: wrapping splits ``asset-owner-wide`` across a line break -- which destroys the
#: token, defeats the match, and makes the rule fire on the one wording that
#: exists in order to be correct. Break on spaces only.
WRAP_KWARGS: Final[dict[str, object]] = {
    "break_on_hyphens": False,
    "break_long_words": False,
}

#: What wash-sale detection covers (ADR 0037, decision 2). Carried by every
#: tax report beside :data:`WASH_SALES_NOT_DETECTED`.
WASH_SALES_DETECTED: Final[tuple[str, ...]] = (
    "losses on long lots in taxable accounts, by sale, expiration, or the stock a "
    "written call delivers",
    "replacements bought, reinvested, taken in units or acquired by an option, in "
    "every account of this portfolio, IRAs included, within 30 days before or after",
    "the same instrument, and instruments declared substantially identical with "
    "`pt instrument equivalent`",
)

#: What it does not, stated in every tax output (ADR 0037, decision 11). A tax
#: report that quietly omitted these would be the "silently wrong number"
#: failure mode by definition, so the list is not suppressible.
WASH_SALES_NOT_DETECTED: Final[tuple[str, ...]] = (
    "securities not declared substantially identical (`pt tax wash-candidates` lists them)",
    "options and contracts to acquire (listed as candidates, not applied)",
    "lots transferred in, or seeded with a custodian-stated basis that may already "
    "carry the custodian's own adjustment (listed as candidates)",
    "short sales, and the purchases that close them (IRC 1091(e))",
    "acquisitions outside this portfolio, such as an account not in this file",
    "replacements disposed of before the loss sale, which are reported for manual "
    "handling and mark the year incomplete",
)

#: Carried by every tax report (ADR 0011). Not suppressible.
#: ADR 0041 §4. What the tax report does not compute for a bond, stated on
#: every report in which a bond was held. Each changes basis or income, so a
#: gain on a bond is reported without them.
BOND_TAX_NOT_COMPUTED: Final[tuple[str, ...]] = (
    "bond premium amortization (IRC §171)",
    "market discount (IRC §1276)",
    "original issue discount",
    "coupon interest, and the purchased accrued interest that reduces it",
)

TAX_DISCLAIMER: Final[str] = (
    "This is an estimate, not tax advice, and portable is not a substitute for a "
    "broker's 1099-B. Estimated liability is computed as the allowed gain multiplied "
    "by the account's effective-dated rate for the holding period at disposition; it "
    "does not model bracket progressivity, the capital-loss limitation or "
    "carryforward, qualified-dividend rate stacking, AMT, or any other income. Wash "
    "sales are applied for the same instrument and for instruments declared "
    "substantially identical, across every account in this portfolio including IRAs. "
    "This report does NOT detect a wash sale through an undeclared security, an "
    "option, a lot transferred in, a short sale, or an account outside this "
    "portfolio; each report lists what it does not detect."
)
