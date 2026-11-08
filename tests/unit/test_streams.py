"""`configure_streams`: a program's output is UTF-8 whatever the console's code page.

The streams here are what Python hands a program on Windows once stdout is a
pipe or a file: a text wrapper in cp1252. The end-to-end proof, through the
real `pt` with a cp1252 stdout, is ``tests/integration/test_console_encoding_cli.py``.
"""

from __future__ import annotations

import io
import sys

import pytest

from portable_core.cli.streams import configure_streams

pytestmark = pytest.mark.unit

#: One character from each source the sweep found in rendered output: a table
#: title (→), a column header (Δ), the null placeholder (—), the disclaimer (®),
#: and a symbol outside every single-byte code page, as a custodian may supply.
SAMPLE = "FUNDI → FUNDR  Basis Δ  —  GIPS®  株"


def _console(encoding: str, errors: str = "strict") -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors=errors)


def _bytes(stream: io.TextIOWrapper) -> bytes:
    stream.flush()
    buffer = stream.buffer
    assert isinstance(buffer, io.BytesIO)
    return buffer.getvalue()


def test_a_cp1252_stdout_cannot_write_the_sample_unconfigured() -> None:
    """The failure, so the test below is known to be testing something."""
    with pytest.raises(UnicodeEncodeError):
        _console("cp1252").write(SAMPLE)


def test_a_cp1252_stdout_writes_utf8_once_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = _console("cp1252"), _console("cp1252", "backslashreplace")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    configure_streams()
    sys.stdout.write(SAMPLE)

    assert sys.stdout is out, "reconfigured in place, never replaced"
    assert _bytes(out) == SAMPLE.encode("utf-8")
    assert (out.encoding, out.errors) == ("utf-8", "strict")


def test_stderr_becomes_utf8_and_still_cannot_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    err = _console("cp1252", "backslashreplace")
    monkeypatch.setattr(sys, "stdout", _console("utf-8"))
    monkeypatch.setattr(sys, "stderr", err)

    configure_streams()
    sys.stderr.write(SAMPLE + "\ud800")  # an unpaired surrogate: unencodable anywhere

    assert (err.encoding, err.errors) == ("utf-8", "backslashreplace")
    assert _bytes(err) == SAMPLE.encode("utf-8") + b"\\ud800"


def test_a_stream_already_in_utf8_is_left_as_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows console, a UTF-8 locale, pytest's capture: nothing to change.

    Reconfiguring with an encoding resets the error handler to strict, which
    would quietly undo a ``surrogateescape`` the interpreter chose for itself.
    """
    out = _console("UTF8", "surrogateescape")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", _console("utf_8", "backslashreplace"))

    configure_streams()

    assert (out.encoding, out.errors) == ("UTF8", "surrogateescape")


def test_a_missing_stream_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    """``pythonw.exe`` starts a program with no stdout or stderr at all."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    configure_streams()
