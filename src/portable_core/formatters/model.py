"""What a command hands to a formatter.

A command produces a :class:`CommandResult`; a formatter renders it. Neither
knows about the other's concerns, which is what lets one command support four
output formats without four code paths -- and what keeps number presentation in
one place instead of at every call site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

__all__ = [
    "Column",
    "ColumnKind",
    "CommandResult",
    "OutputFormat",
    "ReturnValue",
    "Table",
]


class OutputFormat(StrEnum):
    TABLE = "table"
    JSON = "json"
    MARKDOWN = "markdown"
    CSV = "csv"


class ColumnKind(StrEnum):
    """How a column is presented.

    The kind, not the Python type, decides presentation: a share count and a
    dollar amount are both ``Decimal`` and must not render the same way.
    """

    TEXT = "text"
    MONEY = "money"
    QUANTITY = "quantity"
    PRICE = "price"
    RATE = "rate"
    DATE = "date"
    INTEGER = "integer"
    BOOL = "bool"
    RETURN = "return"


@dataclass(frozen=True, slots=True)
class Column:
    key: str
    header: str
    kind: ColumnKind = ColumnKind.TEXT
    #: Rendered when the value is None. Never the same as zero
    #: (`CLAUDE.md`: explicit null, never let blank and zero mean the same
    #: thing).
    null_text: str = "—"


@dataclass(frozen=True, slots=True)
class Table:
    """One logical table. `csv` renders exactly one of these per invocation."""

    columns: tuple[Column, ...]
    rows: tuple[dict[str, Any], ...]
    title: str | None = None
    #: Rendered under the table in human formats; a field in JSON.
    footnotes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReturnValue:
    """A return, with everything needed to label it honestly.

    ``PORT-GIPS-H04`` requires the period and the return basis to be labelled;
    ``CLAUDE.md`` adds the method. All three travel **with the value**, so a
    formatter cannot render a bare number and a consumer cannot receive one.

    ``period_days`` is what the non-annualization guard reads
    (``PORT-GIPS-B07``): a return for a period shorter than a year is never
    annualized, and the check has to happen where rendering happens or a call
    site will eventually bypass it.
    """

    value: Decimal
    #: 'twr', 'mwr', 'modified_dietz', ...
    method: str
    #: 'gross_of_fees' | 'net_of_external_costs_only' | 'net_of_fees'
    basis: str
    period_start: date
    period_end: date
    is_annualized: bool = False
    #: Anything GIPS would not recognise as a return -- after-tax, model,
    #: backtested, a sleeve without allocated cash (``PORT-GIPS-H08``).
    is_supplemental: bool = False
    label: str | None = None

    @property
    def period_days(self) -> int:
        return (self.period_end - self.period_start).days


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Everything a command produced, in a form every formatter can render."""

    command: str
    #: The primary tabular result, if the command has one.
    table: Table | None = None
    #: Structured data for `--format json`, when the result is not a table.
    data: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    as_of: date | None = None
    portfolio: str | None = None
    #: Set on any command that emits a return. Carried as an envelope *field*
    #: in JSON, never a rendered string, so a consumer cannot drop it without
    #: noticing (bootstrap §6.2).
    disclaimer: str | None = None
    #: Reference to the JSON Schema this output validates against.
    schema_ref: str | None = None
