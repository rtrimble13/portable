"""`portable_core` -- the shared framework behind every `portable` CLI.

The domain model is the product; the CLIs are thin. See `docs/architecture.md`
for the layering rules (which `tests/unit/test_layering.py` enforces) and
`CLAUDE.md` for the invariants that a passing test suite does not excuse you
from.
"""

from __future__ import annotations

from typing import Final

__version__: Final[str] = "0.1.0"

#: Bumped whenever the `--format json` envelope changes shape. Consumers pin
#: against this, not against `__version__`.
OUTPUT_SCHEMA_VERSION: Final[str] = "1.0"

__all__ = ["OUTPUT_SCHEMA_VERSION", "__version__"]
