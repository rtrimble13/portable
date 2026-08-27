"""Generate `docs/schema.md` from the DDL comments.

``make docs`` runs this. The bootstrap (§5) requires every table and column to
be documented "generated from the DDL comments so it cannot drift" -- which is
the whole point: prose kept next to the thing it describes, in the file a
reviewer is already reading, and rendered rather than retyped.

The markup it reads is deliberately minimal, because a comment format nobody
can remember does not get written:

* ``-- @table <name>`` immediately before a ``CREATE TABLE`` starts that
  table's description; every ``--`` line after it, up to the ``CREATE``, is the
  description.
* A ``--`` comment block immediately before a column line describes that
  column.
* A trailing ``-- decimal`` marks a column as canonical decimal ``TEXT``
  (ADR 0005) and is rendered as a type rather than as prose.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import TextIO

from portable_core.schema.migrations import CURRENT_SCHEMA_VERSION, available_migrations

_TABLE_MARKER = re.compile(r"^\s*--\s*@table\s+(?P<name>\S+)\s*$")
_CREATE_TABLE = re.compile(
    r'^\s*CREATE\s+TABLE(\s+IF\s+NOT\s+EXISTS)?\s+"?(?P<name>\w+)"?\s*\(', re.IGNORECASE
)
_CREATE_INDEX = re.compile(
    r"^\s*CREATE\s+(UNIQUE\s+)?INDEX(\s+IF\s+NOT\s+EXISTS)?\s+(?P<name>\w+)\s+ON\s+"
    r'"?(?P<table>\w+)"?\s*\((?P<cols>[^)]*)\)',
    re.IGNORECASE,
)
_CREATE_TRIGGER = re.compile(
    r"^\s*CREATE\s+TRIGGER(\s+IF\s+NOT\s+EXISTS)?\s+(?P<name>\w+)", re.IGNORECASE
)
_COMMENT = re.compile(r"^\s*--\s?(?P<text>.*)$")
_DECIMAL_MARKER = re.compile(r"--\s*decimal\s*$")
# A column definition: an identifier followed by a type, at the table's
# indentation. Deliberately conservative -- it must not match a table-level
# CHECK or a trailing UNIQUE.
_COLUMN = re.compile(r"^\s{4}(?P<name>[a-z_][a-z0-9_]*)\s+(?P<rest>[A-Z].*?)[,]?\s*(--.*)?$")
_TABLE_CONSTRAINT = re.compile(
    r"^\s{4}(CHECK|UNIQUE|PRIMARY\s+KEY|FOREIGN\s+KEY)\b", re.IGNORECASE
)


@dataclass(slots=True)
class Column:
    name: str
    sql_type: str
    is_decimal: bool
    doc: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Table:
    name: str
    doc: list[str] = field(default_factory=list)
    columns: list[Column] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    indexes: list[tuple[str, str]] = field(default_factory=list)
    triggers: list[tuple[str, list[str]]] = field(default_factory=list)


def parse(sql: str) -> list[Table]:
    """Extract tables, columns, indexes, and triggers with their comments."""
    tables: list[Table] = []
    by_name: dict[str, Table] = {}

    lines = sql.splitlines()
    pending_doc: list[str] = []
    pending_table_name: str | None = None
    current: Table | None = None

    for raw in lines:
        if current is None:
            marker = _TABLE_MARKER.match(raw)
            if marker:
                pending_table_name = marker.group("name")
                pending_doc = []
                continue

            create = _CREATE_TABLE.match(raw)
            if create:
                name = create.group("name")
                current = Table(name=name, doc=list(pending_doc))
                tables.append(current)
                by_name[name] = current
                pending_doc = []
                pending_table_name = None
                continue

            index = _CREATE_INDEX.match(raw)
            if index and index.group("table") in by_name:
                by_name[index.group("table")].indexes.append(
                    (index.group("name"), index.group("cols").strip())
                )
                pending_doc = []
                continue

            trigger = _CREATE_TRIGGER.match(raw)
            if trigger:
                # The trigger's target table appears on the following line; the
                # doc block that precedes it is the explanation worth keeping.
                pending_doc = list(pending_doc)
                _pending_trigger = trigger.group("name")
                for table in tables[::-1]:
                    if table.name in raw or _pending_trigger.startswith(f"trg_{table.name}"):
                        table.triggers.append((_pending_trigger, list(pending_doc)))
                        break
                pending_doc = []
                continue

            comment = _COMMENT.match(raw)
            if comment:
                if pending_table_name is not None:
                    pending_doc.append(comment.group("text").rstrip())
                else:
                    pending_doc.append(comment.group("text").rstrip())
                continue

            if not raw.strip():
                pending_doc = []
            continue

        # Inside a CREATE TABLE body.
        if raw.startswith(");"):
            current = None
            pending_doc = []
            continue

        # A continuation line -- a CHECK or REFERENCES wrapped onto the next
        # line, or a group label inside a long enumeration. Recognised BEFORE
        # the comment check: `-- trades` inside a CHECK list is a label for the
        # enumeration, not documentation of the next column.
        if (
            current.columns
            and raw.strip()
            and (raw.startswith(" " * 8) or _unbalanced(current.columns[-1].sql_type))
        ):
            stripped = raw.strip()
            if not stripped.startswith("--"):
                last = current.columns[-1]
                if not last.is_decimal:
                    last.sql_type = f"{last.sql_type} {stripped}"
            continue

        comment = _COMMENT.match(raw)
        if comment:
            pending_doc.append(comment.group("text").rstrip())
            continue

        if _TABLE_CONSTRAINT.match(raw):
            current.constraints.append(raw.strip().rstrip(","))
            pending_doc = []
            continue

        column = _COLUMN.match(raw)
        if column:
            rest = column.group("rest").rstrip().rstrip(",")
            current.columns.append(
                Column(
                    name=column.group("name"),
                    sql_type=_clean_type(rest),
                    is_decimal=bool(_DECIMAL_MARKER.search(raw)),
                    doc=list(pending_doc),
                )
            )
            pending_doc = []

    return tables


def _unbalanced(text: str) -> bool:
    """True when *text* has an unclosed parenthesis.

    A wrapped CHECK closes on a line indented back to the column level, so
    indentation alone cannot tell a continuation from the next column. Paren
    balance can. Parens inside string literals are not a concern here: SQL
    identifiers and enum values in this schema contain none.
    """
    return text.count("(") > text.count(")")


def _clean_type(rest: str) -> str:
    """Reduce a column definition to its type and salient constraints."""
    text = re.sub(r"\s+", " ", rest).strip().rstrip(",")
    text = re.sub(r"--.*$", "", text).strip()
    return text


def render(tables: list[Table], out: TextIO) -> None:
    """Write the Markdown document."""
    w = out.write
    w("# `.port` schema reference\n\n")
    w(
        "**Generated from the DDL comments by `make docs`. Do not edit this file by "
        "hand** -- edit the comments in `src/portable_core/schema/*.sql` and "
        "regenerate, so the documentation cannot drift from the database.\n\n"
    )
    w(f"Schema version: **{CURRENT_SCHEMA_VERSION}**\n\n")
    w(
        "Money, quantity, price, and rate columns are `TEXT` holding the canonical "
        "decimal form and are shown below as **`decimal`**. There is no `REAL`, "
        "`FLOAT`, `DOUBLE`, or `NUMERIC` column in this schema and a lint rule fails "
        "the build if one appears -- see "
        "[ADR 0005](adr/0005-decimal-representation-and-rounding.md).\n\n"
        "Dates are `TEXT` `YYYY-MM-DD`; timestamps are `TEXT` ISO-8601 UTC. Booleans "
        "are `INTEGER` constrained to `(0, 1)`.\n\n"
    )

    w("## Tables\n\n")
    for table in tables:
        anchor = table.name.replace("_", "-")
        w(f"- [`{table.name}`](#{anchor})")
        summary = _first_sentence(table.doc)
        if summary:
            w(f" — {summary}")
        w("\n")
    w("\n---\n\n")

    for table in tables:
        w(f"## `{table.name}`\n\n")
        if table.doc:
            w(_paragraphs(table.doc) + "\n\n")

        w("| Column | Type | Notes |\n|---|---|---|\n")
        for col in table.columns:
            sql_type = re.sub(r"\s+", " ", col.sql_type).strip().rstrip(",")
            type_text = "**`decimal`**" if col.is_decimal else f"`{sql_type}`"
            notes = " ".join(line.strip() for line in col.doc if line.strip())
            notes = notes.replace("|", "\\|")
            w(f"| `{col.name}` | {type_text} | {notes} |\n")
        w("\n")

        if table.constraints:
            w("**Table constraints**\n\n")
            for constraint in table.constraints:
                w(f"- `{constraint}`\n")
            w("\n")

        if table.indexes:
            w("**Indexes**\n\n")
            for name, cols in table.indexes:
                w(f"- `{name}` on ({cols})\n")
            w("\n")

        if table.triggers:
            w("**Triggers**\n\n")
            for name, doc in table.triggers:
                explanation = " ".join(line.strip() for line in doc if line.strip())
                w(f"- `{name}`{f' — {explanation}' if explanation else ''}\n")
            w("\n")

    w("---\n\n")
    w(
        "*Regenerate with `make docs`. If this file and the DDL disagree, the DDL is "
        "right and this file is stale.*\n"
    )


def _first_sentence(doc: list[str]) -> str:
    """The first sentence of a doc block, for the index.

    Taken from the joined paragraph rather than the first physical line: the
    DDL wraps at 79 columns, so a first line is usually half a thought.
    """
    paragraphs = _paragraphs(doc)
    if not paragraphs:
        return ""
    first = paragraphs.split("\n\n")[0]
    match = re.search(r"(?<=[.!?])\s", first)
    return (first[: match.start() + 1] if match else first).strip()


def _paragraphs(doc: list[str]) -> str:
    """Join comment lines into paragraphs, preserving blank-line breaks."""
    out: list[str] = []
    buffer: list[str] = []
    for line in doc:
        if line.strip():
            buffer.append(line.strip())
        elif buffer:
            out.append(" ".join(buffer))
            buffer = []
    if buffer:
        out.append(" ".join(buffer))
    return "\n\n".join(out)


def main(out: TextIO | None = None) -> int:
    stream = out or sys.stdout
    tables: list[Table] = []
    for migration in available_migrations():
        tables.extend(parse(migration.sql))
    render(tables, stream)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
