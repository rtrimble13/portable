"""A CLI writes UTF-8, whatever the console's code page.

Python encodes ``sys.stdout`` with the locale's encoding. On Linux and macOS
that is UTF-8; on Windows, once stdout is a pipe or a file rather than the
console itself, it is the ANSI code page -- cp1252 on a US or Western European
install. That is the case for ``pt holdings --format json > out.json``, for
``| jq``, for anything a script or an agent captures, and for the test harness.

Two things followed, and both break a `CLAUDE.md` rule:

* **A character outside the code page raised.** ``→`` in a table title, ``Δ``
  in a column header, or anything in an instrument name a custodian supplied
  stopped the command with ``UnicodeEncodeError`` -- after a mutating command
  had already committed its write, so the crash also hid what was written.
  A bare exception reached the user.
* **A character inside the code page came out as different bytes.** ``—``, the
  null placeholder in every table, was ``0x97`` on Windows and ``E2 80 94``
  everywhere else, so the same command over the same portfolio produced
  different bytes by locale. That is invariant 6 -- and JSON exchanged
  between systems must be UTF-8 (RFC 8259 §8.1), so cp1252 JSON was not
  something a consumer could be relied on to read.

So a CLI's streams are set to UTF-8 at its entry point, once, before anything
is written: one rule that covers every character any formatter, any
mapping file or any custodian can put in the output, rather than a list of
characters somebody has to keep current.

The two alternatives were rejected for a reason each:

* ``errors="replace"`` turns ``→`` into ``?``. In ``--format json`` that is a
  silent change to the data a consumer parses, which is worse than the crash.
* An ASCII fallback for table output covers only the characters on its list,
  never user data, and does nothing for JSON -- which is where the failure the
  tests caught actually was, since the JSON envelope carries the table title.

Line endings are left as the platform sets them; only the encoding changes.
"""

from __future__ import annotations

import codecs
import sys
from typing import TextIO

__all__ = ["configure_streams"]


def configure_streams() -> None:
    """Set ``sys.stdout`` and ``sys.stderr`` to UTF-8.

    Call it first in a program's entry point. It is a process-wide setting, so
    it belongs to whoever owns the process, not to a library function that
    happens to write.

    * **stdout** is strict. UTF-8 can encode every character a ``str`` can hold
      except an unpaired surrogate, and a result containing one is broken data
      that should fail, not be patched.
    * **stderr** keeps ``backslashreplace``, the handler Python gives it by
      default, so that reporting an error can never itself raise.

    A stream already in UTF-8 is left exactly as it is: the Windows console
    (which Python writes in UTF-16 regardless), a UTF-8 locale, and pytest's
    capture all arrive here that way. ``PYTHONIOENCODING`` is overridden on
    purpose: the bytes a command writes are part of what invariant 6 holds
    fixed, and an environment variable that changed them would be a locale
    dependence by another name.

    The streams are reconfigured in place, never replaced, so a logging handler
    or a Rich console already holding a reference keeps writing to the right
    object.
    """
    _to_utf8(sys.stdout, errors="strict")
    _to_utf8(sys.stderr, errors="backslashreplace")


def _to_utf8(stream: TextIO | None, *, errors: str) -> None:
    # None under pythonw.exe, or with a stream closed by whoever launched us:
    # there is nothing to write to and nothing to configure.
    if stream is None or _is_utf8(getattr(stream, "encoding", None)):
        return
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:  # pragma: no cover -- a stand-in that is not a TextIOWrapper
        return
    reconfigure(encoding="utf-8", errors=errors)


def _is_utf8(encoding: str | None) -> bool:
    if not encoding:
        return False
    try:
        return codecs.lookup(encoding).name == "utf-8"
    except LookupError:
        return False
