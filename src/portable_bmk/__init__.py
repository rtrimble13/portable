"""`bmk`: building benchmark and synthetic indexes in a `.bmk` file (ADR 0050).

Thin, as `pt` is: each command parses its arguments, calls one service in
`portable_core`, and hands the result to a formatter. The index's arithmetic is
`portable_core.services.index_construction`, read by `pt` and `pert` too
without importing this package.
"""

from portable_core import __version__

__all__ = ["__version__"]
