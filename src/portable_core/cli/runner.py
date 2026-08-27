"""Turning a service result -- or an error -- into output and an exit code.

Every `pt` command's outermost layer. Two guarantees live here:

* **No bare exception reaches the user.** A `PortableError` renders in the
  active format and exits with its code; anything else is caught, reported as
  ``PT-E-GENERIC``, and exits 1, with the traceback on stderr under ``-vv``.
* **Structured output goes to stdout; everything else to stderr.** That is what
  makes ``pt holdings --format json | jq`` work while ``-v`` is on
  (bootstrap §3.5).
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from typing import Any, TypeVar

from portable_core.errors import ExitCode, PortableError
from portable_core.formatters import CommandResult, OutputFormat, render

__all__ = ["configure_logging", "emit", "run_command"]

T = TypeVar("T")


def configure_logging(*, verbose: int = 0, quiet: bool = False, log_json: bool = False) -> None:
    """Send logs to **stderr**, never stdout.

    stdout is the structured result; a log line mixed into it would corrupt
    JSON for every consumer downstream.
    """
    level = (
        logging.ERROR
        if quiet
        else [logging.WARNING, logging.INFO, logging.DEBUG][min(verbose, 2)]
    )
    handler = logging.StreamHandler(sys.stderr)
    if log_json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    root = logging.getLogger("portable")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False


class _JsonFormatter(logging.Formatter):
    """One JSON object per line, for agentic use (``--log-json``)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, str] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True)


def emit(
    result: CommandResult,
    output_format: OutputFormat,
    *,
    no_color: bool = False,
) -> None:
    """Write a command result to stdout in the active format."""
    render(result, output_format, stream=sys.stdout, no_color=no_color)


def render_error(
    error: PortableError,
    output_format: OutputFormat,
) -> None:
    """Render an error.

    In `json` it goes to **stdout** as a well-formed envelope, because a
    consumer parsing stdout should get structured JSON whether the command
    succeeded or failed -- otherwise every caller needs a special case for
    "the output is not JSON today". In human formats it goes to stderr, where
    a human expects errors.
    """
    if output_format is OutputFormat.JSON:
        payload: dict[str, Any] = {
            "error": error.to_dict(),
            "data": None,
            "warnings": [],
        }
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return

    sys.stderr.write(f"error [{error.code}]: {error.message}\n")
    if error.remedy:
        sys.stderr.write(f"  {error.remedy}\n")
    if error.context:
        for key in sorted(error.context):
            sys.stderr.write(f"  {key}: {error.context[key]}\n")


def run_command(
    action: Callable[[], CommandResult],
    *,
    output_format: OutputFormat = OutputFormat.TABLE,
    no_color: bool = False,
    verbose: int = 0,
) -> int:
    """Run *action*, render whatever it produced, and return an exit code."""
    try:
        result = action()
    except PortableError as error:
        render_error(error, output_format)
        if verbose >= 2:
            logging.getLogger("portable").exception("traceback")
        return int(error.exit_code)
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return int(ExitCode.GENERIC)
    except Exception as unexpected:
        # An unexpected exception is still not allowed to reach the user raw.
        # It is wrapped so the output shape is the same as every other failure,
        # and the traceback is available under -vv rather than by default.
        wrapped = PortableError(
            f"unexpected error: {type(unexpected).__name__}: {unexpected}",
            code="PT-E-GENERIC",
            remedy=(
                "This is a bug. Re-run with -vv for a traceback and please report "
                "it with the command you ran."
            ),
        )
        render_error(wrapped, output_format)
        if verbose >= 2:
            logging.getLogger("portable").exception("traceback")
        return int(ExitCode.GENERIC)

    emit(result, output_format, no_color=no_color)
    return int(ExitCode.OK)


def dry_run_note(result: CommandResult) -> CommandResult:
    """Mark a result as the product of a dry run.

    ``--dry-run`` cuts between the service and persistence, so the effects
    shown are the effects that would occur -- the same code path with the write
    suppressed, not a separate estimate (`docs/architecture.md` §2).
    """
    from dataclasses import replace

    return replace(
        result,
        warnings=(*result.warnings, "DRY RUN: nothing was written to the portfolio."),
        data={**result.data, "dry_run": True},
    )
