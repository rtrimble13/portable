"""`pt` -- the Portfolio Tool.

Portfolio and account definition and maintenance: the primary means of
constructing portfolios and keeping them current for every other tool in
`portable`.

`pt` is thin. Everything it does is `portable_core` doing it -- see
`docs/architecture.md` §2 for how a command's control flow goes, and why
`--dry-run` cuts exactly where it does.
"""

from __future__ import annotations

from portable_core import __version__

__all__ = ["__version__"]
