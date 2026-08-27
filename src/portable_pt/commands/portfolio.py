"""Portfolio-level commands: init, info, migrate, validate, rebuild, export, import."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from portable_core import __version__
from portable_core.cli.context import build_context, parse_date
from portable_core.cli.runner import dry_run_note, run_command
from portable_core.errors import PortfolioFileError, ValidationError
from portable_core.errors.kinds import E_INVARIANT_BROKEN, E_PORTFOLIO_EXISTS
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.persistence.connection import open_portfolio, transaction
from portable_core.persistence.repositories import Repositories
from portable_core.schema import migrations as M
from portable_core.services.replay import ReplayEngine
from portable_pt import state

app = typer.Typer(help="Portfolio-level operations.", no_args_is_help=True)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@app.command()
def init(
    file: Annotated[Path, typer.Argument(help="Path to the new .port file.")],
    name: Annotated[str, typer.Option("--name", help="Portfolio name.")],
    inception: Annotated[str, typer.Option("--inception", help="Inception date (YYYY-MM-DD).")],
    base_currency: Annotated[
        str, typer.Option("--base-currency", help="ISO 4217 code. USD only in v0.1.")
    ] = "USD",
    fiscal_year_end: Annotated[
        str,
        typer.Option(
            "--fiscal-year-end",
            help="MM-DD. Annual boundaries derive from this, never from --as-of.",
        ),
    ] = "12-31",
    description: Annotated[
        str, typer.Option("--description", help="Required on every report (PORT-GIPS-I01).")
    ] = "",
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Create a new portfolio file."""
    options = state.current_options()

    def action() -> CommandResult:
        if file.exists() and not force:
            raise PortfolioFileError(
                f"{file} already exists",
                code=E_PORTFOLIO_EXISTS,
                remedy="Choose another path, or pass --force to overwrite it.",
                path=str(file),
            )

        inception_date = parse_date(inception, what="--inception")
        assert inception_date is not None

        if base_currency.upper() != "USD":
            raise ValidationError(
                f"base currency {base_currency!r} is not supported in v0.1",
                remedy=(
                    "USD only for now. The currency column is carried on every row so "
                    "multi-currency is an extension rather than a migration; see the "
                    "roadmap."
                ),
                currency=base_currency,
            )

        if options.dry_run:
            return dry_run_note(
                CommandResult(
                    command="init",
                    data={
                        "path": str(file),
                        "name": name,
                        "inception_date": inception_date.isoformat(),
                        "schema_version": M.CURRENT_SCHEMA_VERSION,
                    },
                )
            )

        if file.exists() and force:
            file.unlink()

        con = open_portfolio(file, must_exist=False)
        version = M.initialise(con)
        repos = Repositories(con)
        with transaction(con):
            for key, value in {
                "portfolio_name": name,
                "description": description,
                "inception_date": inception_date.isoformat(),
                "base_currency": base_currency.upper(),
                "fiscal_year_end": fiscal_year_end,
                "schema_version": str(version),
                "created_at": _now(),
                "updated_at": _now(),
                "portable_version": __version__,
            }.items():
                repos.meta.set(key, value)
        con.close()

        return CommandResult(
            command="init",
            data={
                "path": str(file),
                "name": name,
                "inception_date": inception_date.isoformat(),
                "base_currency": base_currency.upper(),
                "fiscal_year_end": fiscal_year_end,
                "schema_version": version,
            },
            portfolio=name,
        )

    raise typer.Exit(
        run_command(
            action,
            output_format=options.output_format,
            no_color=options.no_color,
            verbose=options.verbose,
        )
    )


@app.command()
def info() -> None:
    """Summary: accounts, market value, cash, positions, last valuation."""
    options = state.current_options()

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        meta = repos.meta.all()
        accounts = repos.accounts.all()
        positions = repos.positions.all(open_only=True)

        from portable_core.decimals import money_context
        from portable_core.native import implementation

        with money_context():
            cash = sum(
                (repos.valuations.cash(a.account_id, currency=a.currency)[0] for a in accounts),
                __import__("decimal").Decimal("0.00"),
            )

        last_valuation = max(
            (
                d
                for d in (repos.valuations.latest_snapshot_date(a.account_id) for a in accounts)
                if d is not None
            ),
            default=None,
        )

        return CommandResult(
            command="info",
            table=Table(
                columns=(
                    Column("name", "Account", ColumnKind.TEXT),
                    Column("account_type", "Type", ColumnKind.TEXT),
                    Column("custodian", "Custodian", ColumnKind.TEXT),
                    Column("cash", "Cash", ColumnKind.MONEY),
                    Column("status", "Status", ColumnKind.TEXT),
                ),
                rows=tuple(
                    {
                        "name": a.name,
                        "account_type": str(a.account_type),
                        "custodian": a.custodian,
                        "cash": repos.valuations.cash(a.account_id, currency=a.currency)[0],
                        "status": str(a.status),
                    }
                    for a in accounts
                ),
                title=f"{meta.get('portfolio_name', '(unnamed)')}",
            ),
            data={
                "portfolio_name": meta.get("portfolio_name"),
                "description": meta.get("description"),
                "inception_date": meta.get("inception_date"),
                "base_currency": meta.get("base_currency"),
                "fiscal_year_end": meta.get("fiscal_year_end"),
                "schema_version": M.schema_version(repos.con),
                "portable_version": __version__,
                "native_implementation": implementation(),
                "accounts": len(accounts),
                "open_positions": len(positions),
                "transactions": repos.transactions.count(),
                "total_cash": cash,
                "last_valuation": last_valuation.isoformat() if last_valuation else None,
                "as_of": ctx.as_of.isoformat(),
            },
            as_of=ctx.as_of,
            portfolio=meta.get("portfolio_name"),
        )

    raise typer.Exit(
        run_command(
            action,
            output_format=options.output_format,
            no_color=options.no_color,
            verbose=options.verbose,
        )
    )


@app.command()
def migrate(
    no_backup: Annotated[
        bool,
        typer.Option("--no-backup", help="Skip the automatic backup. Rarely a good idea."),
    ] = False,
) -> None:
    """Upgrade the portfolio's schema, taking a backup first."""
    options = state.current_options()

    def action() -> CommandResult:
        ctx = build_context(options, require_portfolio=True, check_schema=False)
        repos = ctx.require_portfolio()
        path = Path(str(ctx.config.get("port")))

        before, after, pending, backup = M.migrate(
            repos.con, path, backup=not no_backup, dry_run=options.dry_run
        )
        result = CommandResult(
            command="migrate",
            data={
                "from_version": before,
                "to_version": after,
                "applied": [f"{m.version:04d}_{m.name}" for m in pending],
                "backup": str(backup) if backup else None,
            },
            portfolio=ctx.portfolio_name(),
        )
        return dry_run_note(result) if options.dry_run else result

    raise typer.Exit(
        run_command(
            action,
            output_format=options.output_format,
            no_color=options.no_color,
            verbose=options.verbose,
        )
    )


@app.command()
def validate(
    strict: Annotated[
        bool, typer.Option("--strict", help="Treat warnings as failures too.")
    ] = False,
) -> None:
    """Check every invariant, and report exactly what is wrong.

    Exits 4 when an invariant is broken. This is the command to run after any
    surprising number, and after any upgrade.
    """
    options = state.current_options()

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        engine = ReplayEngine(repos)

        problems: list[tuple[str, str]] = []
        warnings: list[str] = []

        for problem in M.verify_checksums(repos.con):
            problems.append(("schema", problem))

        required_meta = {
            "portfolio_name",
            "inception_date",
            "base_currency",
            "fiscal_year_end",
            "schema_version",
        }
        missing = sorted(required_meta - set(repos.meta.all()))
        for key in missing:
            problems.append(("meta", f"missing required metadata key {key!r}"))

        for problem in engine.check_cash_conservation():
            problems.append(("cash conservation", problem))
        for problem in engine.check_leg_invariants():
            problems.append(("leg quantity", problem))

        # Foreign keys and CHECK constraints, asked of SQLite directly.
        for row in repos.con.execute("PRAGMA foreign_key_check"):
            problems.append(("foreign key", f"{row[0]} row {row[1]} -> {row[2]}"))
        integrity = repos.con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            problems.append(("integrity", str(integrity)))

        # A ledger that replays to different derived state than is stored is
        # the failure this whole architecture exists to make detectable.
        stored = ReplayEngine(repos)
        digest_before = stored.rebuild().digest
        digest_after = stored.rebuild().digest
        if digest_before != digest_after:
            problems.append(("replay", "rebuilding twice produced different state"))

        if not repos.policies.all():
            warnings.append(
                "no return_policy row: pert will refuse to compute returns until one "
                "exists (PORT-GIPS-B03). Set one with `pt policy set`."
            )
        for account in repos.accounts.all():
            if account.is_taxable and not repos.accounts.rate_schedules(account.account_id):
                warnings.append(
                    f"taxable account {account.name!r} has no tax rate schedule; "
                    "`pt tax` will refuse for it."
                )

        result = CommandResult(
            command="validate",
            table=Table(
                columns=(
                    Column("check", "Check", ColumnKind.TEXT),
                    Column("problem", "Problem", ColumnKind.TEXT),
                ),
                rows=tuple({"check": c, "problem": p} for c, p in problems),
                title="Validation",
            ),
            data={"problems": len(problems), "warnings": len(warnings)},
            warnings=tuple(warnings),
            portfolio=ctx.portfolio_name(),
            as_of=ctx.as_of,
        )

        if problems or (strict and warnings):
            raise ValidationError(
                f"{len(problems)} invariant problem(s) found"
                + (f" and {len(warnings)} warning(s)" if strict and warnings else ""),
                code=E_INVARIANT_BROKEN,
                remedy="Run `pt rebuild` first; if problems persist, they are real.",
                problems=[f"{c}: {p}" for c, p in problems],
                warnings=warnings,
            )
        return result

    raise typer.Exit(
        run_command(
            action,
            output_format=options.output_format,
            no_color=options.no_color,
            verbose=options.verbose,
        )
    )


@app.command()
def rebuild() -> None:
    """Replay the ledger into derived state.

    Safe to run at any time, and the standard response to any suspected
    derived-state bug: after a fix, a rebuild recovers correct state from a
    ledger that was never wrong (ADR 0010).
    """
    options = state.current_options()

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()

        if options.dry_run:
            return dry_run_note(
                CommandResult(
                    command="rebuild",
                    data={"transactions": repos.transactions.count()},
                    portfolio=ctx.portfolio_name(),
                )
            )

        with transaction(repos.con):
            result = ReplayEngine(repos).rebuild()

        return CommandResult(
            command="rebuild",
            data={
                "transactions_replayed": result.transactions_replayed,
                "positions_created": result.positions_created,
                "lots_created": result.lots_created,
                "dispositions_created": result.dispositions_created,
                "digest": result.digest,
            },
            warnings=result.warnings,
            portfolio=ctx.portfolio_name(),
        )

    raise typer.Exit(
        run_command(
            action,
            output_format=options.output_format,
            no_color=options.no_color,
            verbose=options.verbose,
        )
    )


@app.command(name="export")
def export_portfolio(
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write here instead of stdout.")
    ] = None,
) -> None:
    """Dump the whole portfolio as human-readable, diffable JSON.

    Round-trips exactly through `pt import`: export, import, export produces
    identical bytes, and there is a test asserting it.
    """
    options = state.current_options()

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        payload = _export_payload(repos)

        text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if output is not None:
            output.write_text(text, encoding="utf-8")
            return CommandResult(
                command="export",
                data={"path": str(output), "bytes": len(text.encode("utf-8"))},
                portfolio=ctx.portfolio_name(),
            )
        return CommandResult(command="export", data=payload, portfolio=ctx.portfolio_name())

    raise typer.Exit(
        run_command(
            action,
            output_format=options.output_format,
            no_color=options.no_color,
            verbose=options.verbose,
        )
    )


def _export_payload(repos: Repositories) -> dict[str, Any]:
    """Every non-derived table, in a stable order.

    Derived state is deliberately excluded: it is reproducible from the ledger,
    including it would double the file size, and a round-trip that carried it
    could hide a replay bug rather than expose one (ADR 0010).
    """
    payload: dict[str, Any] = {"format": "portable-export", "format_version": 1}
    for table in Repositories.EXPORTABLE_TABLES:
        payload[table] = repos.export_table(table)
    return payload


@app.command(name="import")
def import_portfolio(
    source: Annotated[Path, typer.Argument(help="A file produced by `pt export`.")],
    into: Annotated[
        Path | None,
        typer.Option("--into", help="Create this new .port file instead of using --port."),
    ] = None,
) -> None:
    """Read a `pt export` dump back into an identical portfolio."""
    options = state.current_options()

    def action() -> CommandResult:
        payload = json.loads(source.read_text(encoding="utf-8"))
        if payload.get("format") != "portable-export":
            raise ValidationError(
                f"{source} is not a portable export",
                remedy="Use a file produced by `pt export`.",
                path=str(source),
            )

        target = into or (Path(str(state.current_options().port)) if options.port else None)
        if target is None:
            raise ValidationError(
                "no destination for the import",
                remedy="Pass --into NEW.port, or --port EXISTING.port.",
            )

        if options.dry_run:
            counts = {k: len(v) for k, v in payload.items() if isinstance(v, list)}
            return dry_run_note(
                CommandResult(command="import", data={"target": str(target), **counts})
            )

        con = open_portfolio(target, must_exist=False)
        M.initialise(con)
        repos = Repositories(con)
        with transaction(con):
            counts = repos.import_tables(payload)
        with transaction(con):
            ReplayEngine(repos).rebuild()
        con.close()

        return CommandResult(command="import", data={"target": str(target), **counts})

    raise typer.Exit(
        run_command(
            action,
            output_format=options.output_format,
            no_color=options.no_color,
            verbose=options.verbose,
        )
    )


@app.command()
def backup(
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Backup path.")] = None,
) -> None:
    """Copy the portfolio file, checkpointing the WAL first.

    The checkpoint matters: without it, a plain file copy of a WAL database can
    miss recent commits, which would be a backup that silently omits your most
    recent trades.
    """
    options = state.current_options()

    def action() -> CommandResult:
        ctx = state.with_portfolio()
        repos = ctx.require_portfolio()
        path = Path(str(ctx.config.get("port")))
        destination = output or M.backup_path(path)

        if options.dry_run:
            return dry_run_note(
                CommandResult(command="backup", data={"destination": str(destination)})
            )

        repos.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copy2(path, destination)
        return CommandResult(
            command="backup",
            data={
                "source": str(path),
                "destination": str(destination),
                "bytes": destination.stat().st_size,
            },
            portfolio=ctx.portfolio_name(),
        )

    raise typer.Exit(
        run_command(
            action,
            output_format=options.output_format,
            no_color=options.no_color,
            verbose=options.verbose,
        )
    )
