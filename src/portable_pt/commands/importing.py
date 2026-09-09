"""`pt import batch` -- the commit stage of the import pipeline (ADR 0012)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.persistence.connection import scratch_transaction
from portable_core.persistence.connection import transaction as db_transaction
from portable_core.services.import_batch import BatchImporter, ImportBatch, load_batch
from portable_pt import state
from portable_pt.commands._shared import dispatch, maybe_dry_run

app = typer.Typer(help="Import into a portfolio.", no_args_is_help=True)


def import_batch(
    source: Annotated[Path, typer.Argument(help="A reviewed batch file.")],
) -> None:
    """Commit a reviewed batch to the ledger.

    The third stage of the pipeline: `pt import broker` extracts, a person
    reviews the batch file, and this writes it. The review is the point --
    the ledger is append-only, so a wrong row is corrected with a reversing
    entry that stays visible for the life of the portfolio, and the cheap place
    to catch one is the file.

    Every row goes through the same service a typed command uses, so an
    unclassified fee, a sale with no matching lot, a duplicate reference and a
    fractional share in an account that cannot hold one are all refused here
    exactly as they would be at the keyboard.

    Rows are appended in trade-date order and the ledger is replayed once at
    the end (ADR 0016): a historical batch is back-dated relative to whatever
    the file already holds, and only a replay puts derived state in the
    ledger's own order.
    """

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()

        batch = load_batch(source)
        counts = batch.counted()
        importer = BatchImporter(repos)

        payload: dict[str, object] = {
            "batch": str(source),
            "broker": batch.source.broker,
            "capabilities": list(batch.source.capabilities),
            **counts,
        }
        if batch.source.period is not None:
            payload["period"] = [d.isoformat() for d in batch.source.period]

        table = Table(
            columns=(
                Column("action", "Action"),
                Column("rule", "Rule"),
                Column("rows", "Rows", ColumnKind.INTEGER),
            ),
            # Grouped by rule rather than listed per row: a review is per rule,
            # and a thousand-row batch read one row at a time is not reviewed.
            rows=tuple(_by_rule(batch)),
            title=f"Batch from {batch.source.broker}",
        )

        if ctx.dry_run:
            # The real commit, rolled back. Validating each row against the
            # state *before* the batch would refuse a batch that commits
            # perfectly well -- a sale's lot relief has to see the purchase
            # earlier in the same batch. Running it for real and discarding the
            # result is the only dry run that answers the question asked.
            with scratch_transaction(repos.con):
                rehearsal = importer.commit(batch, batch_path=source)
            return maybe_dry_run(
                CommandResult(
                    command="import batch",
                    data={**payload, "digest": rehearsal.digest},
                    table=table,
                )
            )

        with db_transaction(repos.con):
            outcome = importer.commit(batch, batch_path=source)

        warnings = list(outcome.warnings)
        if outcome.unverified_files:
            # Said out loud, because "verified" and "not checked" are different
            # claims about whether the review covers what was committed.
            warnings.append(
                "could not verify "
                + ", ".join(outcome.unverified_files)
                + ": the source document was not found next to the batch"
            )

        return CommandResult(
            command="import batch",
            data={**payload, "digest": outcome.digest},
            table=table,
            warnings=tuple(warnings),
            portfolio=ctx.portfolio_name(),
        )

    dispatch(action)


def _by_rule(batch: ImportBatch) -> list[dict[str, object]]:
    """Every (action, rule) pair with a count, in a stable order."""
    tally: dict[tuple[str, str], int] = {}
    for row in batch.rows:
        key = (row.action, row.rule)
        tally[key] = tally.get(key, 0) + 1
    return [
        {"action": action, "rule": rule, "rows": count}
        for (action, rule), count in sorted(tally.items())
    ]
