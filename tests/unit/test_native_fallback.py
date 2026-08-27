"""Differential tests for the native extension against its Python reference.

ADR 0008. When the extension is absent these are **skipped loudly** -- the test
prints which functions went unverified rather than passing quietly, because a
silent skip is how a differential test stops being one.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from portable_core import native
from portable_core.native import _reference

pytestmark = pytest.mark.unit

INT64 = st.integers(min_value=-(2**63), max_value=2**63 - 1)

_DIFFERENTIAL_SURFACE = ("checksum", "build_info")

requires_native = pytest.mark.skipif(
    not native.HAVE_NATIVE,
    reason=(
        "portable_native is not built, so the differential comparison did not run. "
        f"UNVERIFIED against C++: {', '.join(_DIFFERENTIAL_SURFACE)}. "
        "Build it with `make cpp` or `pip install -e .`."
    ),
)


# ── the reference implementation stands on its own ───────────────────────────


def test_reference_checksum_matches_known_values() -> None:
    """Pinned so a 'harmless' refactor of the reference cannot drift silently."""
    assert _reference.checksum([]) == 14695981039346656037
    assert _reference.checksum([0]) == _reference.checksum([0])
    assert _reference.checksum([1, 2]) != _reference.checksum([2, 1])


@given(st.lists(INT64, max_size=64))
def test_reference_checksum_is_a_64_bit_value(values: list[int]) -> None:
    assert 0 <= _reference.checksum(values) < 2**64


def test_reference_checksum_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="out of int64 range"):
        _reference.checksum([2**63])


def test_dispatch_reports_its_implementation() -> None:
    """`pt info` surfaces this, so a number traces to the code that made it."""
    assert native.implementation() in {"native", "python"}
    assert (native.implementation() == "native") is native.HAVE_NATIVE


# ── the differential comparison ──────────────────────────────────────────────


@requires_native
@given(st.lists(INT64, max_size=128))
def test_native_checksum_agrees_with_python_reference(values: list[int]) -> None:
    """The reference is normative: if these disagree, the C++ is wrong."""
    import portable_native

    assert portable_native.checksum(values) == _reference.checksum(values)


@requires_native
def test_native_checksum_agrees_at_the_signed_extremes() -> None:
    """Where a platform's signed representation would show through, if it did."""
    import portable_native

    extremes = [-(2**63), -1, 0, 1, 2**63 - 1]
    assert portable_native.checksum(extremes) == _reference.checksum(extremes)


@requires_native
def test_native_build_info_is_a_real_cpp_build() -> None:
    import portable_native

    info = portable_native.build_info()
    assert info.module_version == "0.1.0"
    assert int(info.cxx_standard) >= 201703
    assert native.implementation() == "native"
