"""The `PortableError` hierarchy.

Every error `portable` raises at a user-facing boundary carries:

* a **stable code** (``PT-E-LOT-UNMATCHED``) that may be matched on by scripts
  and never changes meaning;
* a **human message** that says what went wrong and what to do about it;
* **structured context** that renders as JSON under ``--format json``.

No bare exception reaches the user. The CLI's top-level handler catches
`PortableError`, renders it in the active format, and exits with the code the
error carries.

Exit codes (README.md, bootstrap §7.2):

===== ==========================================================
``0`` success
``1`` generic error
``2`` usage error
``3`` portfolio/file error -- missing, locked, wrong schema version
``4`` validation failure -- invariant broken, unmatched lots, missing policy
``5`` data unavailable -- provider down, price stale beyond tolerance
``6`` reconciliation break
===== ==========================================================

The ``PT-E-GIPS-*`` prefix is reserved for refusals that
``docs/gips-standard.md`` requires.
"""

from __future__ import annotations

from portable_core.errors.base import (
    ExitCode,
    PortableError,
)
from portable_core.errors.kinds import (
    DataUnavailableError,
    GipsRefusalError,
    PortfolioFileError,
    ReconciliationBreakError,
    UsageError,
    ValidationError,
)

__all__ = [
    "DataUnavailableError",
    "ExitCode",
    "GipsRefusalError",
    "PortableError",
    "PortfolioFileError",
    "ReconciliationBreakError",
    "UsageError",
    "ValidationError",
]
