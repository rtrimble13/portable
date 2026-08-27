"""The `--format json` envelope.

Schema-stable and versioned (bootstrap §6.2). Consumers pin against
``schema_version``, not against the tool version, so a patch release that adds
nothing to the output does not look like a breaking change.

The ``disclaimer`` is a **field**, not a rendered string. That is deliberate:
a consumer who drops it has to drop a named key, which shows up in their code,
rather than losing a footnote off the end of a formatted block.

``generated_at`` is the one wall-clock value `portable` emits, and it is why
determinism is asserted over the ``data`` block rather than over the whole
envelope -- see :func:`canonical_payload`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from portable_core import OUTPUT_SCHEMA_VERSION, __version__
from portable_core.formatters.model import CommandResult, Table
from portable_core.formatters.numbers import machine

__all__ = ["build_envelope", "canonical_payload", "content_hash", "table_to_rows"]


def table_to_rows(table: Table) -> list[dict[str, Any]]:
    """Render a table's rows for a machine format: full precision, no rounding."""
    return [
        {column.key: machine(row.get(column.key)) for column in table.columns}
        for row in table.rows
    ]


def build_envelope(
    result: CommandResult,
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the JSON envelope for a command result.

    Args:
        generated_at: injectable so that golden-file tests and content hashing
            are not at the mercy of the clock. Defaults to now, in UTC.
    """
    stamp = generated_at or datetime.now(UTC)

    payload: dict[str, Any] = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "portable_version": __version__,
        "command": result.command,
        "generated_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": result.as_of.isoformat() if result.as_of else None,
        "portfolio": result.portfolio,
        "data": _data_block(result),
        "warnings": list(result.warnings),
    }
    # Present as an explicit null rather than absent when the command emits no
    # return: "this command has no disclaimer" and "the disclaimer went
    # missing" must not look the same to a consumer.
    payload["disclaimer"] = result.disclaimer
    if result.schema_ref:
        payload["$schema"] = result.schema_ref
    return payload


def _data_block(result: CommandResult) -> Any:
    if result.table is not None:
        block: dict[str, Any] = {
            "rows": table_to_rows(result.table),
            "columns": [
                {"key": c.key, "header": c.header, "kind": str(c.kind)}
                for c in result.table.columns
            ],
        }
        if result.table.title:
            block["title"] = result.table.title
        if result.table.footnotes:
            block["footnotes"] = list(result.table.footnotes)
        if result.data:
            block.update({k: machine(v) for k, v in result.data.items()})
        return block
    return {k: machine(v) for k, v in result.data.items()}


def canonical_payload(envelope: dict[str, Any]) -> str:
    """The envelope as canonical JSON, with the clock removed.

    ``generated_at`` and ``portable_version`` are excluded: neither is part of
    what the command *computed*, and including them would make every hash
    differ from every other. What remains is exactly the content whose
    stability `CLAUDE.md` invariant 6 promises.
    """
    stripped = {
        k: v for k, v in envelope.items() if k not in {"generated_at", "portable_version"}
    }
    return json.dumps(stripped, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(envelope: dict[str, Any]) -> str:
    """SHA-256 of the canonical payload.

    This is what ``report_issue.content_hash`` stores, and what makes
    ``PORT-GIPS-J01``'s error detection work: rebuild a report, hash it,
    compare. It is meaningful only because output is deterministic, which is
    what ``PORT-GIPS-J06`` records.
    """
    return hashlib.sha256(canonical_payload(envelope).encode("utf-8")).hexdigest()
