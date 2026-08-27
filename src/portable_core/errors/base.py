"""The root of the error hierarchy, and the exit-code enumeration."""

from __future__ import annotations

import enum
from typing import Any


class ExitCode(enum.IntEnum):
    """Process exit codes. Documented in README.md and in ``--help``.

    These are a public interface: a script that branches on exit code must keep
    working across versions, so a code never changes meaning. A new failure
    mode gets a new code or reuses the closest existing one -- it does not
    redefine one.
    """

    OK = 0
    GENERIC = 1
    USAGE = 2
    PORTFOLIO = 3
    VALIDATION = 4
    DATA_UNAVAILABLE = 5
    RECONCILIATION = 6


class PortableError(Exception):
    """Base class for every error `portable` raises deliberately.

    Subclasses fix ``exit_code``; instances fix ``code`` and ``context``.

    ``context`` is for **structured facts**, not for prose: the account name,
    the instrument, the quantity that could not be matched, the tolerance that
    was exceeded. It is rendered as a JSON object under ``--format json``, so
    an agent can branch on it without parsing the message.
    """

    #: Overridden by each subclass.
    exit_code: ExitCode = ExitCode.GENERIC

    #: Overridden per raise site. Stable; treat as public API.
    code: str = "PT-E-GENERIC"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        remedy: str | None = None,
        **context: Any,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        #: What the user can do about it. Rendered after the message, and kept
        #: separate from it so a formatter can style or suppress it.
        self.remedy = remedy
        self.context: dict[str, Any] = dict(context)

    def __str__(self) -> str:
        return self.message

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code!r}, {self.message!r})"

    def to_dict(self) -> dict[str, Any]:
        """Render for ``--format json``.

        Deterministic: keys are fixed and context is sorted, because
        ``CLAUDE.md`` invariant 6 applies to error output too. A diff of two
        failing runs should show the failure, not the dict ordering.
        """
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "exit_code": int(self.exit_code),
        }
        if self.remedy is not None:
            payload["remedy"] = self.remedy
        payload["context"] = {k: self.context[k] for k in sorted(self.context)}
        return payload
