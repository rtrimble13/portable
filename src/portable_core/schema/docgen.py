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

Migrations are read in order and describe **the schema they produce**, not the
history of it. A table re-created by a later migration -- by ``DROP`` and
``CREATE``, or by building ``x_new`` and renaming it onto ``x`` (ADR 0019) --
replaces the earlier entry, keeping the table's and each column's description
where the new text does not restate it. Indexes and triggers a rebuild
recreates are listed once.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import TextIO, TypeVar

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
#: An index ``(name, columns)`` or a trigger ``(name, doc)``.
_Named = TypeVar("_Named", tuple[str, str], tuple[str, list[str]])

_ALTER_RENAME = re.compile(
    r'^\s*ALTER\s+TABLE\s+"?(?P<old>\w+)"?\s+RENAME\s+TO\s+"?(?P<new>\w+)"?', re.IGNORECASE
)
#: ``ALTER TABLE t ADD [COLUMN] c <definition>``, the definition possibly
#: wrapped onto indented lines that follow and end with ``;``.
_ALTER_ADD = re.compile(
    r'^\s*ALTER\s+TABLE\s+"?(?P<table>\w+)"?\s+ADD\s+(COLUMN\s+)?(?P<name>\w+)\s+(?P<rest>.*)$',
    re.IGNORECASE,
)
_DROP_INDEX = re.compile(r"^\s*DROP\s+INDEX(\s+IF\s+EXISTS)?\s+(?P<name>\w+)", re.IGNORECASE)
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


def parse(sql: str, into: list[Table] | None = None) -> list[Table]:
    """Extract tables, columns, indexes, and triggers with their comments.

    Pass the tables parsed from earlier migrations as *into*, and a table this
    text rebuilds replaces its entry there rather than appearing twice.
    """
    tables: list[Table] = into if into is not None else []
    by_name: dict[str, Table] = {table.name: table for table in tables}
    #: A table being re-created, until its body is parsed and it can inherit.
    replacing: dict[int, Table] = {}

    lines = _join_wrapped_indexes(sql.splitlines())
    pending_doc: list[str] = []
    pending_table_name: str | None = None
    current: Table | None = None
    open_constraint = False
    #: A column added by ALTER TABLE whose definition has not ended yet.
    adding: Column | None = None

    for raw in lines:
        if adding is not None:
            adding.sql_type = f"{adding.sql_type} {raw.strip()}"
            if adding.sql_type.rstrip().endswith(";"):
                adding.sql_type = _clean_type(adding.sql_type.rstrip().rstrip(";"))
                adding = None
            continue

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
                previous = by_name.get(name)
                if previous is None:
                    tables.append(current)
                else:
                    tables[tables.index(previous)] = current
                    replacing[id(current)] = previous
                by_name[name] = current
                pending_doc = []
                pending_table_name = None
                continue

            rename = _ALTER_RENAME.match(raw)
            if rename and rename.group("old") in by_name:
                moved = by_name.pop(rename.group("old"))
                moved.name = rename.group("new")
                previous = by_name.get(moved.name)
                if previous is not None:
                    tables.remove(moved)
                    tables[tables.index(previous)] = moved
                    _inherit(moved, previous)
                by_name[moved.name] = moved
                pending_doc = []
                continue

            added = _ALTER_ADD.match(raw)
            if added and added.group("table") in by_name:
                rest = added.group("rest").rstrip()
                new_column = Column(
                    name=added.group("name"),
                    sql_type=rest,
                    is_decimal=bool(_DECIMAL_MARKER.search(raw)),
                    doc=list(pending_doc),
                )
                by_name[added.group("table")].columns.append(new_column)
                if rest.endswith(";"):
                    new_column.sql_type = _clean_type(rest.rstrip(";"))
                else:
                    adding = new_column
                pending_doc = []
                continue

            dropped = _DROP_INDEX.match(raw)
            if dropped:
                for table in tables:
                    table.indexes = [i for i in table.indexes if i[0] != dropped.group("name")]
                pending_doc = []
                continue

            index = _CREATE_INDEX.match(raw)
            if index and index.group("table") in by_name:
                _put(
                    by_name[index.group("table")].indexes,
                    (index.group("name"), index.group("cols").strip()),
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
                        _put(table.triggers, (_pending_trigger, list(pending_doc)))
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
            previous = replacing.pop(id(current), None)
            if previous is not None:
                _inherit(current, previous)
            current = None
            pending_doc = []
            open_constraint = False
            continue

        # A table-level CHECK wrapped over several lines continues as that
        # constraint until its parentheses balance. Recognised first: its
        # inner lines are indented like a column's continuation, and used to be
        # glued onto whichever column preceded the constraint.
        if open_constraint:
            stripped = raw.strip()
            if stripped and not stripped.startswith("--"):
                current.constraints[-1] = f"{current.constraints[-1]} {stripped}"
            if not _unbalanced(current.constraints[-1]):
                current.constraints[-1] = _clean_constraint(current.constraints[-1])
                open_constraint = False
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
            open_constraint = _unbalanced(current.constraints[-1])
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


def _inherit(table: Table, previous: Table) -> None:
    """Carry *previous*'s descriptions onto the table that replaces it.

    A rebuild restates a table's shape but rarely its prose: the column
    comments written when a column was added live in the migration that added
    it. So a description the new text does not give is kept from the old one,
    and indexes and triggers the rebuild does not mention are kept as well --
    the migration runner refuses a rebuild that loses one (ADR 0019, ADR 0022).
    """
    if not table.doc:
        table.doc = previous.doc
    earlier = {column.name: column for column in previous.columns}
    for column in table.columns:
        prior = earlier.get(column.name)
        if prior is not None and not column.doc:
            column.doc = prior.doc
    for index in previous.indexes:
        if all(name != index[0] for name, _ in table.indexes):
            table.indexes.insert(0, index)
    for trigger in previous.triggers:
        mine = [i for i, (name, _) in enumerate(table.triggers) if name == trigger[0]]
        if not mine:
            table.triggers.insert(0, trigger)
        elif not table.triggers[mine[0]][1]:
            table.triggers[mine[0]] = (trigger[0], trigger[1])


def _put(items: list[_Named], item: _Named) -> None:
    """Add *item*, replacing one of the same name: a rebuild recreates by name.

    A recreation that says nothing about itself keeps the earlier
    description: the comment explaining a trigger was written once, where the
    trigger first appeared, and a rebuild's ``CREATE TRIGGER`` restates only
    its SQL.
    """
    for position, existing in enumerate(items):
        if existing[0] == item[0]:
            if item[1]:
                items[position] = item
            return
    items.append(item)


_INDEX_START = re.compile(r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\b", re.IGNORECASE)


def _join_wrapped_indexes(lines: list[str]) -> list[str]:
    """Rejoin a ``CREATE INDEX`` whose ``ON`` clause wrapped onto the next line.

    Migration 0002's unique index on ``(account_id, external_ref)`` is written
    that way, and a line-at-a-time match never saw it -- so the one index that
    enforces duplicate detection was missing from the reference.
    """
    joined: list[str] = []
    for line in lines:
        if (
            joined
            and _INDEX_START.match(joined[-1])
            and not _CREATE_INDEX.match(joined[-1])
            and line.strip()
        ):
            joined[-1] = f"{joined[-1].rstrip()} {line.strip()}"
            continue
        joined.append(line)
    return joined


def _clean_constraint(text: str) -> str:
    """One line, no padding inside parentheses, no trailing comma."""
    text = re.sub(r"\s+", " ", text).strip().rstrip(",")
    return re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", text))


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
        parse(migration.sql, into=tables)
    render(tables, stream)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
