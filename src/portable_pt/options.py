"""Shared Typer options, defined once.

Every `pt` command accepts the same global flags (bootstrap §7.2). Defining
them here rather than per command means they cannot drift, and means adding one
is a single edit rather than fifty.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

__all__ = [
    "AccountOpt",
    "AsOfOpt",
    "DateOpt",
    "DryRunOpt",
    "FormatOpt",
    "NoColorOpt",
    "NoteOpt",
    "OfflineOpt",
    "PortOpt",
    "QuietOpt",
    "RefOpt",
    "SourceOpt",
    "VerboseOpt",
    "YesOpt",
]

PortOpt = Annotated[
    Path | None,
    typer.Option(
        "--port",
        envvar="PORTABLE_PORT",
        help="The .port file. Also settable in ~/.portablerc.",
    ),
]

FormatOpt = Annotated[
    str,
    typer.Option(
        "--format",
        "-f",
        help="Output format: table, json, markdown, or csv.",
    ),
]

AsOfOpt = Annotated[
    str | None,
    typer.Option(
        "--as-of",
        help=(
            "Report state as it was known on this date (YYYY-MM-DD). "
            "Defaults to today, explicitly."
        ),
    ),
]

SourceOpt = Annotated[
    str | None,
    typer.Option("--source", "-S", help="Market data source: file, fafnir, or null."),
]

OfflineOpt = Annotated[
    bool,
    typer.Option("--offline", help="Use only prices already cached in the .port file."),
]

DryRunOpt = Annotated[
    bool,
    typer.Option("--dry-run", help="Show the exact effects without writing anything."),
]

YesOpt = Annotated[
    bool,
    typer.Option("--yes", "-y", help="Answer yes to every confirmation."),
]

VerboseOpt = Annotated[
    int,
    typer.Option("--verbose", "-v", count=True, help="Log more to stderr. -vv for debug."),
]

QuietOpt = Annotated[bool, typer.Option("--quiet", "-q", help="Suppress warnings.")]

NoColorOpt = Annotated[
    bool, typer.Option("--no-color", help="Plain text output. NO_COLOR is also honoured.")
]

AccountOpt = Annotated[str, typer.Option("--account", "-a", help="Account name or id.")]

DateOpt = Annotated[str | None, typer.Option("--date", "-d", help="Trade date (YYYY-MM-DD).")]

NoteOpt = Annotated[str | None, typer.Option("--note", help="Free-text note.")]

RefOpt = Annotated[
    str | None,
    typer.Option("--ref", help="External reference, e.g. the broker confirm id."),
]
