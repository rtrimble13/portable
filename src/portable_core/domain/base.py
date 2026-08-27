"""The shape every domain object takes, and the guard that keeps floats out.

ADR 0003: domain objects are frozen, slotted dataclasses with no I/O and no
business rules. Validation lives at the boundaries -- the CLI, the schema's
CHECK constraints, and the service layer's invariant assertions.

The one thing enforced *here* is the money guard, because it is the mistake
this repository is built to prevent and because catching it at construction
means catching it once rather than at every place a value is used.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any


def check_decimal_fields(obj: Any) -> None:
    """Reject a float where a Decimal is declared.

    Called from ``__post_init__``. The lint rule catches ``float`` written in
    the source; this catches one that arrived at runtime -- from JSON, from a
    provider's client library, from a caller who did the arithmetic in the
    wrong type. Together they close the gap.

    Annotations are strings here (``from __future__ import annotations``), so
    the check is textual. That is deliberate rather than a compromise:
    resolving annotations at runtime would need every referenced type
    importable at every call site, and the textual form is exact enough --
    ``Decimal`` appears in the annotation of every money field and in no
    other.
    """
    for field in dataclasses.fields(obj):
        annotation = str(field.type)
        if "Decimal" not in annotation:
            continue
        value = getattr(obj, field.name)
        if value is None:
            continue
        if isinstance(value, Decimal):
            continue
        raise TypeError(
            f"{type(obj).__name__}.{field.name} must be Decimal, got "
            f"{type(value).__name__}: {value!r}. "
            "Money, quantities, prices, and rates never touch binary floating "
            "point (CLAUDE.md invariant 1, ADR 0005)."
        )
