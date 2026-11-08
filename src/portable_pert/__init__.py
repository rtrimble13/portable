"""`pert` -- performance measurement, from the snapshots `pt value` stores.

**Partly built.** `pert twr` computes the time-weighted return (ADR 0040):
daily sub-periods from stored, checked valuation snapshots, Modified Dietz
across a gap of unpriced days, every return basis from the stored fee
classes, the method recorded per sub-period. It reads and never writes.

The rest of milestone `v0.2` -- money-weighted returns, multi-period
reporting, benchmarks, risk, attribution, the tearsheet -- is not built, and
`pert` offers no command for it rather than one that returns a plausible
default (`CLAUDE.md` invariant 10). See `docs/roadmap.md` and the `area:pert`
issues.
"""

from __future__ import annotations

from typing import Final

__version__: Final[str] = "0.1.0"
MILESTONE: Final[str] = "v0.2"

__all__ = ["MILESTONE", "__version__"]
