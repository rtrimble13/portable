"""Output formatting. One subsystem, four formats, used by every CLI.

Number presentation rules live in :mod:`portable_core.formatters.numbers`,
once, and not inline at call sites -- including the two return rules that
``PORT-GIPS-B07`` and ``PORT-GIPS-H04`` place here specifically so that no call
site can bypass them.
"""

from __future__ import annotations

from portable_core.formatters.envelope import build_envelope, content_hash
from portable_core.formatters.model import (
    Column,
    ColumnKind,
    CommandResult,
    OutputFormat,
    ReturnValue,
    Table,
)
from portable_core.formatters.numbers import (
    NULL_TEXT,
    human,
    machine,
    money,
    price,
    quantity,
    rate,
    render_return,
    require_not_annualized,
)
from portable_core.formatters.renderers import render, supports_color

__all__ = [
    "NULL_TEXT",
    "Column",
    "ColumnKind",
    "CommandResult",
    "OutputFormat",
    "ReturnValue",
    "Table",
    "build_envelope",
    "content_hash",
    "human",
    "machine",
    "money",
    "price",
    "quantity",
    "rate",
    "render",
    "render_return",
    "require_not_annualized",
    "supports_color",
]
