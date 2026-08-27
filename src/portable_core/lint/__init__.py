"""The two project-specific lint rules.

Both are run by ``make lint`` and by CI, and **neither may be silenced**:

* :mod:`portable_core.lint.no_float` — no binary floating point in any money,
  quantity, price, or rate path (``CLAUDE.md`` invariant 1, bootstrap §3.2).
* :mod:`portable_core.lint.gips_language` — no compliance claim about the GIPS
  standards, anywhere (``CLAUDE.md`` invariant 11, ``PORT-GIPS-J05``).

They live in ``portable_core`` rather than in a loose ``tools/`` script so that
they are importable, type-checked under mypy strict, and unit-testable against
fixtures — a lint rule with no test is a lint rule that silently stops firing.

Run them with ``python -m portable_core.lint {no-float,gips-language,all}``.
"""

from __future__ import annotations

from portable_core.lint._common import Finding, iter_repo_files, repo_root

__all__ = ["Finding", "iter_repo_files", "repo_root"]
