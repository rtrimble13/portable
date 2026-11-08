"""Entry point for the `portable` command: what belongs to the family, not one CLI.

**Not implemented.** Its first and only planned verb is `portable config show`,
which docs/architecture.md §6 specifies: the effective configuration and where
each value came from. `Config.to_rows()` already computes that, secrets
redacted; the entry point is what is missing, tracked in issue #38.

It lives in `portable_core` rather than in a CLI package because configuration
is shared by every CLI, and no CLI may import another.

Prints its help and exits non-zero. It does not pretend to work.
"""

from __future__ import annotations

import sys
from typing import Final

MILESTONE: Final[str] = "v0.2"
ISSUE_URL: Final[str] = "https://github.com/rtrimble13/portable/issues/38"

_HELP: Final[str] = f"""\
portable -- Commands shared by the whole portable CLI family

  NOT YET IMPLEMENTED. Planned for milestone {MILESTONE}.

  `portable` ships stubs that say so rather than commands that appear to
  work, because output that looks right and is not is the failure mode this
  project exists to avoid (CLAUDE.md invariant 10).

  What portable will do:

      * `portable config show` -- the effective configuration, and where
        each value came from: a flag, a PORTABLE_* variable, ./portable.toml,
        ~/.portablerc, or the built-in default. Secrets redacted.
        docs/architecture.md section 6.

  Tracking:  {ISSUE_URL}
  Roadmap:   docs/roadmap.md

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
