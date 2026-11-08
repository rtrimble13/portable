"""`docs/schema.md` is what the generator writes, on every platform.

On Windows, Python encodes a redirected stdout in the ANSI code page, cp1252 on
a US install, so `make docs` -- then ``docgen > docs/schema.md`` -- wrote each
``—`` as byte ``0x97`` and raised ``UnicodeEncodeError`` at the first ``─``,
leaving the reference truncated. ``PYTHONIOENCODING=cp1252`` gives a Linux run
the same stdout, so CI on both platforms reproduces the owner's console.

Each run compares the generator's bytes with the committed file, so these
tests also hold the reference to the DDL comments: a comment edited without
``make docs`` fails here. Only the committed copy has CRLF normalised, because
git may convert line endings in a Windows checkout. The generator's own output
must have none.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from portable_core.lint._common import repo_root

pytestmark = pytest.mark.integration

ROOT = repo_root()
COMMITTED = ROOT / "docs" / "schema.md"
REMEDY = "Run `make docs` and commit docs/schema.md."

#: A Windows console's stdout once it is redirected to a file.
CP1252 = {"PYTHONIOENCODING": "cp1252"}


def _python(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    """Run a fresh interpreter, as `make docs` does, and keep stdout as bytes."""
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        capture_output=True,
        env={**os.environ, **env},
        check=False,
    )


def _docgen(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    completed = _python("-m", "portable_core.schema.docgen", *args, env=env)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    return completed


def _expected() -> bytes:
    return COMMITTED.read_bytes().replace(b"\r\n", b"\n")


def _explain(actual: bytes, expected: bytes) -> str:
    """Say whether *actual* is in the wrong encoding or the reference is stale."""
    if b"\r" in actual:
        return "the generator wrote CR line endings"
    try:
        text = actual.decode("utf-8")
    except UnicodeDecodeError as exc:
        return f"the generator's output is not UTF-8: {exc}"
    got, want = text.splitlines(), expected.decode("utf-8").splitlines()
    for number, (a, b) in enumerate(zip(got, want, strict=False), start=1):
        if a != b:
            return f"docs/schema.md is stale at line {number}: {b!r} != {a!r}. {REMEDY}"
    return f"docs/schema.md is stale: {len(want)} lines, generated {len(got)}. {REMEDY}"


def test_the_harness_reproduces_a_redirect_on_windows() -> None:
    """Otherwise the tests below would pass whatever the generator did."""
    completed = _python("-c", "import sys; print(sys.stdout.encoding)", env=CP1252)
    assert completed.stdout.strip() == b"cp1252"


def test_reference_written_to_stdout_is_utf8_whatever_the_code_page() -> None:
    """``docgen > docs/schema.md`` gives the committed bytes under cp1252."""
    actual = _docgen(env=CP1252).stdout
    expected = _expected()
    assert actual == expected, _explain(actual, expected)


def test_reference_written_with_out_is_utf8_with_lf_whatever_the_code_page(
    tmp_path: Path,
) -> None:
    """`make docs` writes the committed bytes, and nothing to stdout."""
    out = tmp_path / "schema.md"
    completed = _docgen("--out", str(out), env=CP1252)
    assert completed.stdout == b""
    actual = out.read_bytes()
    expected = _expected()
    assert actual == expected, _explain(actual, expected)
