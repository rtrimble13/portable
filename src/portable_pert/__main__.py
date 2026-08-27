"""Entry point for the `pert` CLI.

Prints its help and exits non-zero. It does not pretend to work.
"""

from __future__ import annotations

import sys
from typing import Final

from portable_pert import MILESTONE

_PLANNED: Final[tuple[str, ...]] = (
    "Daily time-weighted return engine (PORT-GIPS-B01..B07)",
    "Money-weighted return / XIRR (PORT-GIPS-C01..C05)",
    "Multi-period reporting (PORT-GIPS-B07, H02)",
    "Benchmarks and relative performance (PORT-GIPS-G01..G05)",
    "Risk-adjusted metrics (PORT-GIPS-F01..F05)",
    "Brinson-Fachler attribution (outside GIPS; see gips-standard.md 7.2)",
    "Position- and security-level analysis (PORT-GIPS-E08, H08)",
    "After-tax performance (USIPC, not GIPS; see gips-standard.md 7.1)",
    "Tearsheet output (PORT-GIPS-H01..H08, I01..I17)",
)

_HELP: Final[str] = f"""\
pert -- Performance, attribution, and risk-adjusted returns

  NOT YET IMPLEMENTED. Planned for milestone {MILESTONE}.

  `portable` ships stubs that say so rather than functions that return a
  plausible default, because a plausible default in a money path is a landmine
  that surfaces as a wrong number months later (CLAUDE.md invariant 10).

  What pert will do, one issue each:

      * Daily time-weighted return engine (PORT-GIPS-B01..B07)
      * Money-weighted return / XIRR (PORT-GIPS-C01..C05)
      * Multi-period reporting (PORT-GIPS-B07, H02)
      * Benchmarks and relative performance (PORT-GIPS-G01..G05)
      * Risk-adjusted metrics (PORT-GIPS-F01..F05)
      * Brinson-Fachler attribution (outside GIPS; see gips-standard.md 7.2)
      * Position- and security-level analysis (PORT-GIPS-E08, H08)
      * After-tax performance (USIPC, not GIPS; see gips-standard.md 7.1)
      * Tearsheet output (PORT-GIPS-H01..H08, I01..I17)

  Every one of these inherits docs/gips-standard.md where it governs; the
  requirement IDs above are the specification.

  Tracking:  https://github.com/rtrimble13/portable/labels/area:pert
  Roadmap:   docs/roadmap.md
  Standard:  docs/gips-standard.md

  What works today:  `pt --help`
"""


def main() -> int:
    """Print help to stderr and exit 1. Structured output would be a lie."""
    # T201 is suppressed here deliberately: this is the one place a CLI
    # writes without a formatter, because there is no result to format.
    print(_HELP, file=sys.stderr)  # noqa: T201
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
