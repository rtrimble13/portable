"""What an adapter's sources can support, and what their absence costs.

ADR 0018 §2, mirroring `providers.Capability`. The pattern is deliberately the
same one `providers/base.py` uses, for the same reason: *a partial provider is
legal and its gaps are visible*. `FileProvider` declares a capability only where
the caller actually supplied a file, because declaring one with nothing behind
it "would produce an empty result that reads as 'no prices exist' rather than
'you did not tell me where they are'". Import needs that construction more, not
less: a portfolio built without cost basis is permanently different from one
built with it, and a reader months later has to be able to tell which they have.

The rule that makes it worth having is ADR 0018 §3: **a capability is declared
on validated data, not on a present column.** The reference custodian's export
has a settlement-date column in which most populated cells hold a date earlier
than their own trade date. A column is not a capability. Where a column exists
but fails its check the capability is withheld *and the reason is reported* --
which is strictly better than the column being absent, because the user learns
their custodian's export is broken rather than assuming the field is
unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "ABSENCE_MEANS",
    "CapabilityFinding",
    "CapabilitySet",
    "ImportCapability",
]


class ImportCapability(StrEnum):
    """An optional input, declared by the adapter that validated it."""

    COST_BASIS = "cost_basis"
    ACQUISITION_DATE = "acquisition_date"
    LOT_DETAIL = "lot_detail"
    TRANSACTION_ID = "transaction_id"
    INSTRUMENT_SYMBOL = "instrument_symbol"
    SETTLEMENT_DATE = "settlement_date"
    CORPORATE_ACTIONS = "corporate_actions"
    EXTERNAL_FLOWS = "external_flows"
    HISTORY_TO_INCEPTION = "history_to_inception"


#: What the resulting portfolio may no longer claim, per capability. ADR 0018's
#: table, in code, because this is what `pt import` reports and what `pt info`
#: carries forward -- and a table only in a document drifts from the behaviour.
ABSENCE_MEANS: Final[dict[ImportCapability, str]] = {
    ImportCapability.COST_BASIS: (
        "every seeded lot is basis_source='unavailable'; the portfolio still "
        "builds and performance is unaffected, but `pt tax` cannot report a "
        "realized gain on any pre-cutover lot"
    ),
    ImportCapability.ACQUISITION_DATE: (
        "seeded lots are dated at the cutover, so holding-period character is "
        "conservative by construction -- everything seeded reads short-term "
        "until a year past the cutover"
    ),
    ImportCapability.LOT_DETAIL: (
        "no specific identification within a seeded block; `pt sell --lots` can "
        "name the block, not shares inside it"
    ),
    ImportCapability.TRANSACTION_ID: (
        "external_ref is synthesized from row content (ADR 0012), so re-import "
        "is safe only while the custodian's export is stable row-for-row"
    ),
    ImportCapability.INSTRUMENT_SYMBOL: (
        "a name-to-symbol crosswalk is required and any unmapped name is a refusal"
    ),
    ImportCapability.SETTLEMENT_DATE: (
        "settlement is not recorded; no effect on recognition, since `portable` "
        "is trade-date accounting"
    ),
    ImportCapability.CORPORATE_ACTIONS: (
        "splits and reorganisations are absent from the history, so rolled-back "
        "quantities will not reconcile; the reconstruction's own check catches "
        "this and names the instrument"
    ),
    ImportCapability.EXTERNAL_FLOWS: (
        "contributions and withdrawals rest on the activity map alone, with no "
        "second document to cross-check against; a misclassified deposit "
        "rewrites the track record silently (PORT-GIPS-B02), so review the map "
        "with that specifically in mind"
    ),
    ImportCapability.HISTORY_TO_INCEPTION: (
        "a cutover is required and ADR 0017 applies in full"
    ),
}


@dataclass(frozen=True, slots=True)
class CapabilityFinding:
    """One capability, whether it was declared, and why.

    ``reason`` is populated on a *withheld* capability and is the part worth
    reading: "the column is not in the file" and "the column is there and 71%
    of its dates precede their own trade date" are different facts about the
    custodian, and only one of them is worth complaining to them about.
    """

    capability: ImportCapability
    declared: bool
    #: The named check that decided it, e.g. ``"populated"``. ``None`` where the
    #: source declared no check at all for this capability.
    check: str | None = None
    reason: str | None = None
    #: Rows examined and rows that satisfied the check. Reported so a marginal
    #: pass is as visible as a failure -- 96% of rows carrying a basis is a
    #: declared capability and also something to look at.
    examined: int = 0
    satisfied: int = 0

    def summary(self) -> str:
        if self.declared:
            return f"declared by {self.check} ({self.satisfied}/{self.examined} rows)"
        return self.reason or "not declared"


@dataclass(frozen=True, slots=True)
class CapabilitySet:
    """Every capability's finding, declared or not. Never a bare set.

    A bare set of what is present cannot answer "why not?", and that question
    is the one the user actually has when their tax report refuses.
    """

    findings: tuple[CapabilityFinding, ...]

    def __contains__(self, capability: object) -> bool:
        return any(f.capability == capability and f.declared for f in self.findings)

    @property
    def declared(self) -> tuple[ImportCapability, ...]:
        return tuple(f.capability for f in self.findings if f.declared)

    @property
    def withheld(self) -> tuple[CapabilityFinding, ...]:
        return tuple(f for f in self.findings if not f.declared)

    def finding(self, capability: ImportCapability) -> CapabilityFinding:
        for found in self.findings:
            if found.capability == capability:
                return found
        return CapabilityFinding(
            capability, declared=False, reason="the source declares no check for it"
        )
