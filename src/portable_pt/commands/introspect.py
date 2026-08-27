"""`pt introspect` -- the command tree, for generators and agents.

Bootstrap §6.6: emit "the complete command tree -- commands, arguments, types,
defaults, help text, and output schema references -- sufficient for a generator
to produce MCP tool definitions **without parsing --help**".

That last clause is the requirement. Anything derived from help text is a
scraper, and a scraper breaks silently the first time the help is reworded.

**Deliberately click-agnostic.** Typer vendors its own copy of click under a
private module name, and which one it uses is Typer's business and subject to
change between releases. So this walks the command objects by duck typing --
``.commands``, ``.params``, ``.help`` -- rather than importing click and
running ``isinstance`` against a class that may not be the same class Typer
built. Adding `click` as a direct dependency would give us a *second* click
whose types would not match Typer's at all.
"""

from __future__ import annotations

from typing import Any

import typer

from portable_core import OUTPUT_SCHEMA_VERSION
from portable_core.errors.kinds import ERROR_CODES
from portable_core.formatters import CommandResult
from portable_pt.commands._shared import dispatch


def _describe_parameter(param: Any) -> dict[str, Any]:
    """One argument or option, by duck typing.

    A parameter with no ``--opts`` is positional; that is the distinction click
    draws between an Argument and an Option, and it holds whichever click this
    is.
    """
    opts = list(getattr(param, "opts", []) or [])
    param_type = getattr(param, "type", None)
    default = getattr(param, "default", None)

    return {
        "name": getattr(param, "name", None),
        "opts": opts,
        "secondary_opts": list(getattr(param, "secondary_opts", []) or []),
        "type": getattr(param_type, "name", None) or type(param_type).__name__,
        "required": bool(getattr(param, "required", False)),
        "default": None if default is None else str(default),
        "is_flag": bool(getattr(param, "is_flag", False)),
        "multiple": bool(getattr(param, "multiple", False)),
        "help": getattr(param, "help", None),
        "kind": "option" if any(o.startswith("-") for o in opts) else "argument",
    }


def _describe_command(name: str, command: Any, path: list[str]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "path": [*path, name],
        "invocation": " ".join(["pt", *path, name]),
        "help": (getattr(command, "help", "") or "").strip(),
        "parameters": [_describe_parameter(p) for p in getattr(command, "params", []) or []],
    }
    children = getattr(command, "commands", None)
    if isinstance(children, dict) and children:
        entry["commands"] = [
            _describe_command(child_name, child, [*path, name])
            for child_name, child in sorted(children.items())
        ]
    return entry


def introspect() -> None:
    """Emit the complete command tree as JSON.

    Enough for a generator to produce MCP tool definitions directly. Also
    publishes the stable error codes, so a caller can branch on a failure
    without matching on message text -- which would break the first time a
    message is improved.
    """

    def action() -> CommandResult:
        from portable_pt.app import app as pt_app

        group = typer.main.get_command(pt_app)
        commands = getattr(group, "commands", {}) or {}

        return CommandResult(
            command="introspect",
            data={
                "tool": "pt",
                "output_schema_version": OUTPUT_SCHEMA_VERSION,
                "formats": ["table", "json", "markdown", "csv"],
                "global_options": [
                    _describe_parameter(p) for p in getattr(group, "params", []) or []
                ],
                "exit_codes": {
                    "0": "success",
                    "1": "generic error",
                    "2": "usage error",
                    "3": "portfolio/file error",
                    "4": "validation failure",
                    "5": "data unavailable",
                    "6": "reconciliation break",
                },
                "error_codes": list(ERROR_CODES),
                "schemas_directory": "schemas/",
                "commands": [
                    _describe_command(name, command, [])
                    for name, command in sorted(commands.items())
                ],
            },
            portfolio=None,
        )

    dispatch(action)
