"""The four output formats.

Every command supports all four. Structured results go to **stdout**; logs,
warnings, and progress go to **stderr** (bootstrap §3.5) -- which is what makes
``pt holdings --format json | jq`` work while ``-v`` is on.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
from datetime import datetime
from typing import Any, TextIO

from portable_core.formatters.envelope import build_envelope
from portable_core.formatters.model import (
    ColumnKind,
    CommandResult,
    OutputFormat,
)
from portable_core.formatters.numbers import NULL_TEXT, human

__all__ = ["render", "supports_color"]

#: Columns whose values are numeric and therefore right-aligned.
_NUMERIC_KINDS = frozenset(
    {
        ColumnKind.MONEY,
        ColumnKind.QUANTITY,
        ColumnKind.PRICE,
        ColumnKind.RATE,
        ColumnKind.INTEGER,
        ColumnKind.RETURN,
    }
)


def supports_color(stream: TextIO, *, no_color: bool = False) -> bool:
    """Whether to emit colour.

    Honours ``NO_COLOR`` (the informal standard) and degrades on a non-TTY
    automatically, so piping into a file or another process yields plain text
    without anybody having to remember a flag.
    """
    if no_color or os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def render(
    result: CommandResult,
    output_format: OutputFormat,
    *,
    stream: TextIO | None = None,
    no_color: bool = False,
    generated_at: datetime | None = None,
) -> str:
    """Render *result* and write it to *stream*. Returns what was written."""
    out = stream if stream is not None else sys.stdout

    match output_format:
        case OutputFormat.JSON:
            text = _render_json(result, generated_at=generated_at)
        case OutputFormat.CSV:
            text = _render_csv(result)
        case OutputFormat.MARKDOWN:
            text = _render_markdown(result)
        case OutputFormat.TABLE:
            text = _render_table(result, no_color=supports_color(out, no_color=no_color))

    out.write(text)
    return text


# ── json ─────────────────────────────────────────────────────────────────────


def _render_json(result: CommandResult, *, generated_at: datetime | None) -> str:
    envelope = build_envelope(result, generated_at=generated_at)
    # sort_keys for determinism: CLAUDE.md invariant 6 means two runs must
    # produce identical bytes, and dict insertion order is not a stable
    # contract across code paths.
    return json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ── csv ──────────────────────────────────────────────────────────────────────


def _render_csv(result: CommandResult) -> str:
    """RFC 4180, one logical table, header row, **no formatting**.

    Full stored precision: a spreadsheet is where somebody re-does the
    arithmetic, and handing it a rounded number guarantees their total
    disagrees with ours.
    """
    if result.table is None:
        # A command with no table still has to produce something a pipeline
        # can consume, so its data block becomes a two-column key/value table
        # rather than an empty file that reads as "no results".
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(["key", "value"])
        for key in sorted(result.data):
            writer.writerow([key, _csv_value(result.data[key])])
        return buffer.getvalue()

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([column.header for column in result.table.columns])
    for row in result.table.rows:
        writer.writerow([_csv_value(row.get(column.key)) for column in result.table.columns])
    return buffer.getvalue()


def _csv_value(value: Any) -> str:
    """A CSV cell. Empty for null -- and empty is never the same as 0."""
    from portable_core.formatters.numbers import machine

    if value is None:
        return ""
    rendered = machine(value)
    return rendered if isinstance(rendered, str) else str(rendered)


# ── markdown ─────────────────────────────────────────────────────────────────


def _render_markdown(result: CommandResult) -> str:
    """GitHub-flavored tables, for notes or an LLM context window."""
    lines: list[str] = []

    if result.table is not None and result.table.title:
        lines.extend([f"### {result.table.title}", ""])

    if result.table is not None:
        columns = result.table.columns
        lines.append("| " + " | ".join(c.header for c in columns) + " |")
        lines.append(
            "|"
            + "|".join("---:" if c.kind in _NUMERIC_KINDS else ":---" for c in columns)
            + "|"
        )
        for row in result.table.rows:
            cells = [
                _escape_markdown(human(row.get(c.key), c.kind, null=c.null_text))
                for c in columns
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    if result.data:
        for key in sorted(result.data):
            lines.append(f"- **{key}**: {result.data[key]}")
        lines.append("")

    if result.table is not None and result.table.footnotes:
        for note in result.table.footnotes:
            lines.append(f"> {note}")
        lines.append("")

    if result.warnings:
        lines.append("**Warnings**")
        lines.extend(f"- {w}" for w in result.warnings)
        lines.append("")

    if result.disclaimer:
        lines.extend(_wrap_disclaimer(result.disclaimer))

    return "\n".join(lines).rstrip("\n") + "\n"


def _escape_markdown(text: str) -> str:
    return text.replace("|", "\\|")


def _wrap_disclaimer(disclaimer: str) -> list[str]:
    """Wrap the disclaimer as a block quote, without breaking hyphenated words.

    ``break_on_hyphens=False`` is load-bearing: the default splits
    "asset-owner-wide" across a line, which destroys the token the
    compliance-language lint rule matches on and makes the rule fire on the one
    wording that exists in order to be correct. See
    :data:`portable_core.disclaimer.WRAP_KWARGS`.
    """
    import textwrap

    from portable_core.disclaimer import WRAP_KWARGS

    wrapped = textwrap.fill(
        disclaimer,
        width=78,
        initial_indent="> ",
        subsequent_indent="> ",
        **WRAP_KWARGS,  # type: ignore[arg-type]
    )
    return ["---", "", wrapped, ""]


# ── table (the human default) ────────────────────────────────────────────────


def _render_table(result: CommandResult, *, no_color: bool) -> str:
    """Rich-rendered when colour is available, plain text otherwise.

    The plain path is not a fallback nobody exercises: it is what CI, a pipe,
    and ``NO_COLOR`` all get, so it is the one rendered by default in tests.
    """
    if no_color:
        return _render_rich(result)
    return _render_plain(result)


def _render_plain(result: CommandResult) -> str:
    lines: list[str] = []

    if result.table is not None:
        if result.table.title:
            lines.extend([result.table.title, ""])
        columns = result.table.columns
        cells = [
            [human(row.get(c.key), c.kind, null=c.null_text) for c in columns]
            for row in result.table.rows
        ]
        widths = [
            max(len(c.header), *(len(row[i]) for row in cells)) if cells else len(c.header)
            for i, c in enumerate(columns)
        ]
        lines.append(
            "  ".join(
                c.header.rjust(w) if c.kind in _NUMERIC_KINDS else c.header.ljust(w)
                for c, w in zip(columns, widths, strict=True)
            )
        )
        lines.append("  ".join("-" * w for w in widths))
        for row_cells in cells:
            lines.append(
                "  ".join(
                    value.rjust(w) if c.kind in _NUMERIC_KINDS else value.ljust(w)
                    for value, c, w in zip(row_cells, columns, widths, strict=True)
                )
            )
        if not cells:
            lines.append("(no rows)")
        lines.append("")
        for note in result.table.footnotes:
            lines.append(f"  {note}")
        if result.table.footnotes:
            lines.append("")

    if result.data:
        width = max((len(k) for k in result.data), default=0)
        for key in sorted(result.data):
            value = result.data[key]
            lines.append(f"{key.ljust(width)}  {NULL_TEXT if value is None else value}")
        lines.append("")

    if result.disclaimer:
        lines.extend(_wrap_disclaimer(result.disclaimer)[1:])

    return "\n".join(lines).rstrip("\n") + "\n"


def _render_rich(result: CommandResult) -> str:
    """The colour path. Falls back to plain if Rich is unavailable."""
    try:
        from rich.console import Console
        from rich.table import Table as RichTable
    except ImportError:  # pragma: no cover -- rich is a hard dependency
        return _render_plain(result)

    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=True, width=120)

    if result.table is not None:
        rich_table = RichTable(title=result.table.title, header_style="bold")
        for column in result.table.columns:
            rich_table.add_column(
                column.header,
                justify="right" if column.kind in _NUMERIC_KINDS else "left",
            )
        for row in result.table.rows:
            rich_table.add_row(
                *(human(row.get(c.key), c.kind, null=c.null_text) for c in result.table.columns)
            )
        console.print(rich_table)
        for note in result.table.footnotes:
            console.print(f"[dim]{note}[/dim]")

    if result.data:
        for key in sorted(result.data):
            value = result.data[key]
            console.print(f"[bold]{key}[/bold]  {NULL_TEXT if value is None else value}")

    if result.disclaimer:
        console.print()
        console.print(f"[dim]{result.disclaimer}[/dim]")

    return buffer.getvalue()
