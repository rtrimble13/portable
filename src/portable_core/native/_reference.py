"""Pure-Python reference implementations of everything in `portable_native`.

**This module is normative.** ADR 0008: the Python reference is written first,
and if it and the C++ disagree, the C++ is wrong until proven otherwise. The
differential tests in `tests/unit/test_native_fallback.py` compare them.

Nothing here may be deleted when the extension exists. The fallback is not a
degraded mode -- it is the control arm that tells us the fast path is right, and
it is what lets somebody without a compiler still read their own tax report.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, NamedTuple

_FNV_OFFSET_BASIS: Final[int] = 14695981039346656037
_FNV_PRIME: Final[int] = 1099511628211
_MASK64: Final[int] = 0xFFFFFFFFFFFFFFFF


class BuildInfo(NamedTuple):
    """Mirrors `portable_native.BuildInfo`. See ADR 0008 and PORT-GIPS-J03."""

    compiler: str
    cxx_standard: str
    module_version: str


def build_info() -> BuildInfo:
    """Describe the pure-Python implementation in the extension's own shape."""
    return BuildInfo(compiler="pure-python", cxx_standard="n/a", module_version="0.1.0")


def checksum(values: Sequence[int]) -> int:
    """FNV-1a over 64-bit integers, byte by byte, little-end first.

    Must agree with `portable::checksum` bit for bit, for every input including
    the extremes of the signed range. The two-step -- reinterpret through the
    unsigned domain, then hash the low byte of each of the eight shifts -- is
    what makes the result independent of how a platform represents a negative
    integer.
    """
    digest = _FNV_OFFSET_BASIS
    for value in values:
        if not -(2**63) <= value < 2**63:
            raise ValueError(f"value out of int64 range: {value}")
        bits = value & _MASK64
        for shift in range(0, 64, 8):
            digest ^= (bits >> shift) & 0xFF
            digest = (digest * _FNV_PRIME) & _MASK64
    return digest
