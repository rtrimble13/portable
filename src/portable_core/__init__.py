"""`portable_core` -- the shared framework behind every `portable` CLI.

The domain model is the product; the CLIs are thin. See `docs/architecture.md`
for the layering rules (which `tests/unit/test_layering.py` enforces) and
`CLAUDE.md` for the invariants that a passing test suite does not excuse you
from.
"""

from __future__ import annotations

from typing import Final

#: The version of every `portable` CLI, written here and nowhere else (ADR 0049).
#: `pt`, `pert`, `po`, `risky` and the native extension re-export or read it,
#: and `pyproject.toml` reads it for the distribution metadata. A plain string
#: literal, so a build can read it without importing the package. Set by
#: `scripts/release.py` on a branch; merging that branch publishes it.
__version__ = "0.3.0"

#: Bumped whenever the `--format json` envelope changes shape. Consumers pin
#: against this, not against `__version__`.
OUTPUT_SCHEMA_VERSION: Final[str] = "1.0"

__all__ = ["OUTPUT_SCHEMA_VERSION", "__version__"]
