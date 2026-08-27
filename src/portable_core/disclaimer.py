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
#: cannot drop it without noticing (bootstrap §6.2).
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

#: Carried by every tax report until wash-sale detection lands (ADR 0011).
#: A tax report that quietly omits wash sales is the "silently wrong number"
#: failure mode by definition, so this is not suppressible.
TAX_DISCLAIMER: Final[str] = (
    "This is an estimate, not tax advice, and portable is not a substitute for a "
    "broker's 1099-B. Estimated liability is computed as realized gain multiplied by "
    "the account's effective-dated rate for the holding period at disposition; it does "
    "not model bracket progressivity, the capital-loss limitation or carryforward, "
    "qualified-dividend rate stacking, AMT, or any other income. This report does NOT "
    "account for wash sales: the 30-day window spans all of the taxpayer's accounts, "
    "including IRAs, and covers substantially identical securities and options. "
    "Wash-sale detection is deferred to v0.2."
)
