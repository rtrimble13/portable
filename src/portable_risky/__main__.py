"""Entry point for the `risky` CLI.

Prints its help and exits non-zero. It does not pretend to work.
"""

from __future__ import annotations

import sys
from typing import Final

from portable_risky import MILESTONE

_PLANNED: Final[tuple[str, ...]] = (
    "Exposure analytics",
    "Volatility and covariance",
    "VaR and CVaR, with backtesting of exceptions",
    "Stress testing and scenario analysis",
    "Option risk -- candidate for the first real C++ hot path",
    "Fixed income risk",
    "Drawdown and tail analytics",
)

_HELP: Final[str] = f"""\
risky -- Risk and scenario analysis

  NOT YET IMPLEMENTED. Planned for milestone {MILESTONE}.

  `portable` ships stubs that say so rather than functions that return a
  plausible default, because a plausible default in a money path is a landmine
  that surfaces as a wrong number months later (CLAUDE.md invariant 10).

  What risky will do, one issue each:

      * Exposure analytics
      * Volatility and covariance
      * VaR and CVaR, with backtesting of exceptions
      * Stress testing and scenario analysis
      * Option risk -- candidate for the first real C++ hot path
      * Fixed income risk
      * Drawdown and tail analytics

  Every one of these inherits docs/gips-standard.md where it governs; the
  requirement IDs above are the specification.

  Tracking:  https://github.com/rtrimble13/portable/labels/area:risky
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
