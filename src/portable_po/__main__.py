"""Entry point for the `po` CLI.

Prints its help and exits non-zero. It does not pretend to work.
"""

from __future__ import annotations

import sys
from typing import Final

from portable_po import MILESTONE

_PLANNED: Final[tuple[str, ...]] = (
    "Integrate rtrimble13/po (portopt) as the optimization engine -- wrap, do not reimplement",
    ".port -> optimizer input adapter",
    "Optimizer output -> proposed trades, as a pt-consumable file",
    "Tax-aware optimization -- the differentiator",
    "Constraint surface in the CLI",
    "Efficient frontier and reporting (PORT-GIPS-J04: never link theoretical to actual)",
)

_HELP: Final[str] = f"""\
po -- Portfolio optimization

  NOT YET IMPLEMENTED. Planned for milestone {MILESTONE}.

  `portable` ships stubs that say so rather than functions that return a
  plausible default, because a plausible default in a money path is a landmine
  that surfaces as a wrong number months later (CLAUDE.md invariant 10).

  What po will do, one issue each:

      * Integrate rtrimble13/po (portopt) as the optimization engine -- wrap, do not reimplement
      * .port -> optimizer input adapter
      * Optimizer output -> proposed trades, as a pt-consumable file
      * Tax-aware optimization -- the differentiator
      * Constraint surface in the CLI
      * Efficient frontier and reporting (PORT-GIPS-J04: never link theoretical to actual)

  Every one of these inherits docs/gips-standard.md where it governs; the
  requirement IDs above are the specification.

  Tracking:  https://github.com/rtrimble13/portable/labels/area:po
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
