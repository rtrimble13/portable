"""Custodian adapters: exports in, canonical records out.

ADR 0018. Two documents are required of any custodian -- a holdings snapshot
carrying cash, and a transaction history -- and everything beyond them is a
declared capability whose absence is a named refusal rather than a quiet
degradation.

The common case needs no Python: `TabularAdapter` reads `source.toml` and
`activity_map.toml` and that is the whole adapter. Per-custodian modules exist
only where a custodian's data needs logic a mapping cannot express, and each is
a documented deviation rather than the norm.
"""

from __future__ import annotations

from portable_core.importers.activity import (
    ActivityMap,
    ActivityRule,
    Sign,
    load_activity_map,
)
from portable_core.importers.capabilities import (
    ABSENCE_MEANS,
    CapabilityFinding,
    CapabilitySet,
    ImportCapability,
)
from portable_core.importers.checks import CHECKS
from portable_core.importers.records import HoldingRecord, TransactionRecord
from portable_core.importers.source import CapabilityCheck, SourceSpec, load_source
from portable_core.importers.tabular import AdapterReport, SkippedRow, TabularAdapter

__all__ = [
    "ABSENCE_MEANS",
    "CHECKS",
    "ActivityMap",
    "ActivityRule",
    "AdapterReport",
    "CapabilityCheck",
    "CapabilityFinding",
    "CapabilitySet",
    "HoldingRecord",
    "ImportCapability",
    "Sign",
    "SkippedRow",
    "SourceSpec",
    "TabularAdapter",
    "TransactionRecord",
    "load_activity_map",
    "load_source",
]
