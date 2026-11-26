"""Asset classes, classifications and the allocation policy. ADR 0048.

Reference rows the owner declares, effective-dated and never edited: nothing
here is inferred from a sector, a provider's label or an instrument type.
Weights are fractions of 1, in ``Decimal``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from portable_core.domain.base import check_decimal_fields

__all__ = [
    "CASH_CLASS",
    "AllocationPolicy",
    "AssetClass",
    "ClassShare",
    "Classification",
    "Target",
]

#: The built-in class: each account's cash less its margin loan, sweep
#: vehicles, and dividends declared and not yet paid (ADR 0048 §1, §4).
CASH_CLASS: Final = "cash"


@dataclass(frozen=True, slots=True)
class AssetClass:
    """One of the owner's classes. Its name never changes."""

    class_id: int
    name: str
    description: str
    is_builtin: bool = False


@dataclass(frozen=True, slots=True)
class ClassShare:
    """The share of an instrument's value in one class: more than 0, at most 1."""

    asset_class: str
    weight: Decimal

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class Classification:
    """Which class or classes an instrument belongs to, from a date.

    The shares sum to exactly 1. A corrected classification is kept and never
    read (ADR 0025 decision 5).
    """

    classification_id: int
    instrument_id: int
    effective_from: date
    shares: tuple[ClassShare, ...]
    note: str | None = None
    corrects_classification_id: int | None = None


@dataclass(frozen=True, slots=True)
class Target:
    """One class's target weight, and its band either side, in a policy."""

    asset_class: str
    target: Decimal
    #: Absolute, in the same units as the weight: 0.05 is five percentage
    #: points either side (ADR 0048, answer 2).
    band: Decimal

    def __post_init__(self) -> None:
        check_decimal_fields(self)


@dataclass(frozen=True, slots=True)
class AllocationPolicy:
    """The portfolio's target allocation from a date. Targets sum to exactly 1."""

    policy_id: int
    effective_from: date
    targets: tuple[Target, ...]
    note: str | None = None
    corrects_policy_id: int | None = None
