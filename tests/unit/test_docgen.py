"""`docgen` writes UTF-8 with LF line endings, whatever stdout's encoding.

`tests/integration/test_schema_reference.py` holds the real reference to the
committed file under cp1252. This holds the mechanism on its own, with text
that has both kinds of character the code page gets wrong: one it maps to
another byte (``—`` to ``0x97``) and one it cannot encode at all (``─``). So it
stays meaningful whatever the DDL comments happen to contain.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from portable_core.schema import docgen

pytestmark = pytest.mark.unit

TEXT = "# Title\n\n- `lot` — one row per lot\n\n-- ─── section ───\n"


def _main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> bytes:
    """Run `docgen.main` with stdout as Windows gives it to a redirect.

    That is a cp1252 text stream. It is installed here rather than in a
    fixture because pytest restores its own capture between a fixture's setup
    and the test. Returns what reached stdout.
    """
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(docgen, "document", lambda: TEXT)
    monkeypatch.setattr(sys, "stdout", stdout)
    assert docgen.main(list(argv)) == 0
    stdout.flush()
    return raw.getvalue()


def test_stdout_gets_utf8_when_its_encoding_is_cp1252(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _main(monkeypatch) == TEXT.encode("utf-8")


def test_out_gets_utf8_with_lf_and_stdout_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "schema.md"
    assert _main(monkeypatch, "--out", str(out)) == b""
    assert out.read_bytes() == TEXT.encode("utf-8")
