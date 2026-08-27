"""The one dispatch point between the native extension and its Python twin.

ADR 0008 and `docs/architecture.md` §10. The rules, restated because they are
the whole reason this module exists:

* The Python reference is normative. Disagreement means the C++ is wrong.
* **No native-only functionality, ever.** The extension may only make existing
  Python faster; a capability that existed only in C++ would make
  ``PORTABLE_BUILD_NATIVE=OFF`` a silently different product.
* **`Decimal` does not cross the pybind11 boundary.** When a money hot path is
  eventually moved, it moves as integer or string arithmetic with an explicit,
  tested conversion -- never as ``double``.
* `pt info --format json` reports which implementation is live, so a number can
  always be traced to the code that produced it (PORT-GIPS-J03).
"""

from __future__ import annotations

from collections.abc import Sequence
from types import ModuleType
from typing import Literal

from portable_core.native import _reference

# `portable_native` is an optional compiled extension, so it is imported as an
# opaque module object rather than as a typed symbol: mypy cannot see a
# pybind11 module's signatures, and pretending otherwise would put an
# unverified type contract in front of the one place we do not have one. The
# conversions below are explicit for the same reason.
_native: ModuleType | None
try:  # pragma: no cover -- exercised by whichever half of CI is running
    import portable_native as _native_module

    _native = _native_module
    HAVE_NATIVE: bool = True
except ImportError:  # pragma: no cover
    _native = None
    HAVE_NATIVE = False


def implementation() -> Literal["native", "python"]:
    """Which implementation this process will actually use."""
    return "native" if HAVE_NATIVE else "python"


def build_info() -> _reference.BuildInfo:
    """Build provenance for whichever implementation is live."""
    if _native is not None:
        info = _native.build_info()
        return _reference.BuildInfo(
            compiler=info.compiler,
            cxx_standard=info.cxx_standard,
            module_version=info.module_version,
        )
    return _reference.build_info()


def checksum(values: Sequence[int]) -> int:
    """Deterministic FNV-1a checksum over 64-bit integers."""
    if _native is not None:
        return int(_native.checksum(list(values)))
    return _reference.checksum(values)


__all__ = ["HAVE_NATIVE", "build_info", "checksum", "implementation"]
