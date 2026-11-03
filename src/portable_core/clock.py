"""The clock `portable` stamps the rows it writes with (ADR 0035).

Every ``created_at`` and ``updated_at`` written into a `.port` file, and every
migration's ``applied_at``, is read from a :class:`Clock` the writer was handed.
None of them comes from ``datetime.now()`` at the call site. Real use hands
over :data:`SYSTEM_CLOCK`, which reads the wall clock. The fixture generator
hands over a :class:`FixedClock`, and that is why two builds of
`examples/sample.port` are byte-identical (`CLAUDE.md` invariant 6).

A stamp records when a row was written. It is not a business date: nothing
orders, replays or values by it (ADR 0010), so pinning it changes no figure.

A stdlib-only leaf, like :mod:`portable_core.decimals`, so that any layer that
writes a row can depend on it without depending on anything else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Final

__all__ = ["STAMP_FORMAT", "SYSTEM_CLOCK", "Clock", "FixedClock", "SystemClock"]

#: How a stamp is stored: UTC, to the second, with a literal ``Z``.
STAMP_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"


def _require_aware(moment: datetime) -> None:
    """Refuse a naive datetime.

    Converting one to UTC would assume the machine's local zone, which makes
    the stamp depend on where the code ran. That is the dependence this module
    exists to remove.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"a clock reading must be timezone-aware, got naive {moment!r}")


class Clock(ABC):
    """Where a writer reads the time it stamps a row with."""

    @abstractmethod
    def now(self) -> datetime:
        """The current moment. Always timezone-aware."""

    def stamp(self) -> str:
        """:meth:`now` as it is stored, in :data:`STAMP_FORMAT`."""
        moment = self.now()
        _require_aware(moment)
        return moment.astimezone(UTC).strftime(STAMP_FORMAT)


class SystemClock(Clock):
    """The wall clock, in UTC. Every real command uses it."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def __repr__(self) -> str:
        return "SystemClock()"


class FixedClock(Clock):
    """A clock that always reads one moment.

    For a generated fixture and for tests, where a stamp must not depend on
    when the code ran. Never for a real portfolio: every row it stamps claims
    the same moment, however far apart the rows were written.
    """

    def __init__(self, moment: datetime) -> None:
        _require_aware(moment)
        self._moment = moment

    def now(self) -> datetime:
        return self._moment

    def __repr__(self) -> str:
        return f"FixedClock({self._moment.isoformat()})"


#: The default for every writer that is not told otherwise. It holds no state,
#: so sharing one instance is not shared state.
SYSTEM_CLOCK: Final[Clock] = SystemClock()
