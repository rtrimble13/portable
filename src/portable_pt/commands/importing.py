"""`pt import` -- the broker import pipeline (ADR 0012).

`inspect` is the extract stage's front door: it reads a custodian's
exports through the generic tabular adapter and reports what they can
support before anything is written. `batch` is the commit stage: it takes a
reviewed batch file and writes the ledger.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.importers import ABSENCE_MEANS, TabularAdapter
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


def import_inspect(
    adapter: Annotated[
        Path,
        typer.Argument(help="An adapter directory holding source.toml and activity_map.toml."),
    ],
) -> None:
    """Read a custodian's exports and report what they can support.

    The first stage of the pipeline (ADR 0012) and the first thing to run
    against a new custodian. It reads both required documents -- a holdings
    snapshot carrying cash, and a transaction history -- maps every activity
    string, and reports the capability set.

    **Read the capability table before anything else.** A portfolio built
    without `cost_basis` is permanently different from one built with it, and
    the difference is not visible in any later number: `pt tax` simply refuses
    on pre-cutover lots. The table says which capabilities this custodian's
    export earned, which it did not, and what each absence costs (ADR 0018).

    A capability is declared on *validated data*, never on a present column. A
    settlement-date column whose dates precede their own trade dates does not
    earn `settlement_date`: the column is not read, and the reason is reported
    here — which is more useful than the column being absent, because it says
    the export is broken rather than the field unavailable.

    Nothing is written. This reads files and reports.
    """

    def action() -> CommandResult:
        report = TabularAdapter.load(adapter).read()
        capabilities = report.capabilities

        table = Table(
            columns=(
                Column("capability", "Capability"),
                Column("declared", "Declared", ColumnKind.BOOL),
                Column("check", "Check"),
                Column("detail", "Detail"),
            ),
            rows=tuple(
                {
                    "capability": finding.capability.value,
                    "declared": finding.declared,
                    "check": finding.check,
                    "detail": finding.summary(),
                }
                for finding in capabilities.findings
            ),
            title=f"{report.name} — declared capabilities",
            # The consequences go under the table rather than in a column: what
            # a missing capability costs is a paragraph, and a paragraph in a
            # cell is a paragraph nobody reads.
            footnotes=(
                "A capability is declared on validated data, not on a present "
                "column (ADR 0018 §3). Data behind a withheld capability is not "
                "read at all.",
                *(
                    f"without {finding.capability.value}: {ABSENCE_MEANS[finding.capability]}"
                    for finding in capabilities.withheld
                ),
            ),
        )

        return CommandResult(
            command="import inspect",
            data={
                "adapter": str(adapter),
                "broker": report.broker,
                "name": report.name,
                "accounts": list(report.accounts),
                "as_of": report.as_of.isoformat() if report.as_of else None,
                "period": ([d.isoformat() for d in report.period] if report.period else None),
                "holdings": len(report.holdings),
                "transactions": len(report.transactions),
                "skipped": len(report.skipped),
                "files": [{"name": name, "sha256": digest} for name, digest in report.files],
                "capabilities": {
                    "declared": [c.value for c in capabilities.declared],
                    "withheld": [
                        {
                            "capability": finding.capability.value,
                            "check": finding.check,
                            "reason": finding.reason,
                            "absence_means": ABSENCE_MEANS[finding.capability],
                        }
                        for finding in capabilities.withheld
                    ],
                },
                "skipped_rows": [
                    {"row": row.index, "activity": row.activity, "reason": row.reason}
                    for row in report.skipped
                ],
            },
            table=table,
            as_of=report.as_of,
            schema_ref="import-inspect-1.0.json",
        )

    dispatch(action)
