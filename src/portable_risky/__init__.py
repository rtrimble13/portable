"""`risky` -- Risk and scenario analysis.

**Not implemented.** This package is a deliberate stub for milestone `v0.4`.
It exists so the command exists, reports honestly that it does not work yet, and
points at the issues that specify it.

`CLAUDE.md` invariant 10: a `NotImplementedError` with a link to its issue is
honest; a function that returns zero, an empty list, or a plausible-looking
default is a landmine that will surface as a wrong number months later. There
are no such functions here.

See `docs/roadmap.md` and the `area:risky` issues on the milestone.
"""

from __future__ import annotations

from typing import Final

__version__: Final[str] = "0.1.0"
MILESTONE: Final[str] = "v0.4"

__all__ = ["MILESTONE", "__version__"]
