"""The `bmk` Typer application. ADR 0050.

`bmk` builds an index in a `.bmk` file: the owner's definition, the market
data it is built from, and the levels built from both. An index is
theoretical: a benchmark or a factor, never performance anyone earned, and it
is never linked onto a portfolio's own record (``PORT-GIPS-J04``).
"""

from __future__ import annotations

import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from portable_bmk import state
from portable_core import __version__
from portable_core.cli.context import parse_date, parse_decimal
from portable_core.cli.runner import configure_logging
from portable_core.domain.index import (
    EventType,
    IndexMeta,
    Member,
    RebalanceRule,
    Schedule,
    SecurityEvent,
    SecurityType,
    SyntheticSecurity,
    Weighting,
)
from portable_core.errors import UsageError, ValidationError
from portable_core.errors.kinds import E_INVARIANT_BROKEN
from portable_core.formatters import Column, ColumnKind, CommandResult, Table
from portable_core.persistence.index_repositories import IndexRepositories
from portable_core.services import index_construction, index_definition, index_inputs

app = typer.Typer(
    name="bmk",
    help=(
        "bmk -- benchmark and synthetic indexes, built from units of securities in a "
        ".bmk file (ADR 0050).\n\n"
        "Define an index (init, security add, methodology set, reconstitute), read its "
        "inputs (refresh), declare how each security pays (schedule), build it, and read "
        "its levels. The total-return level reinvests every dividend; the price level is "
        "price-only and is never a benchmark (PORT-GIPS-G01).\n\n"
        "Exit codes: 0 ok, 1 generic, 2 usage, 3 file, 4 validation, 5 data unavailable, "
        "6 reconciliation break."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    rich_markup_mode=None,
)
security_app = typer.Typer(help="The securities an index may hold.", no_args_is_help=True)
methodology_app = typer.Typer(
    help="How the index weights and rebalances.", no_args_is_help=True
)
event_app = typer.Typer(
    help="Delistings, mergers and spinoffs, declared (ADR 0050 §5).", no_args_is_help=True
)
schedule_app = typer.Typer(
    help="How each security pays dividends: the total return's safeguard.", no_args_is_help=True
)
app.add_typer(security_app, name="security")
app.add_typer(methodology_app, name="methodology")
app.add_typer(event_app, name="event")
app.add_typer(schedule_app, name="schedule")

Corrects = Annotated[
    bool,
    typer.Option(
        "--corrects",
        help="Replace the row entered wrongly on this date; it is kept as history.",
    ),
]
Note = Annotated[str | None, typer.Option("--note")]


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit(0)


@app.callback()
def main(
    bmk: Annotated[
        Path | None, typer.Option("--bmk", envvar="PORTABLE_BMK", help="The .bmk file.")
    ] = None,
    output_format: Annotated[
        str, typer.Option("--format", "-f", help="table | json | markdown | csv")
    ] = "table",
    as_of: Annotated[
        str | None, typer.Option("--as-of", help="Defaults to today, explicitly.")
    ] = None,
    source: Annotated[
        str | None, typer.Option("--source", help="Market data source: fafnir | file.")
    ] = None,
    offline: Annotated[bool, typer.Option("--offline", help="Reach no provider.")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Do the write, then roll it back.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Answer yes to any prompt.")] = False,
    verbose: Annotated[int, typer.Option("--verbose", "-v", count=True)] = 0,
    quiet: Annotated[bool, typer.Option("--quiet", "-q")] = False,
    no_color: Annotated[bool, typer.Option("--no-color")] = False,
    log_json: Annotated[
        bool, typer.Option("--log-json", help="One JSON object per log line, on stderr.")
    ] = False,
    version: Annotated[  # noqa: ARG001 -- Typer's eager-callback idiom
        bool, typer.Option("--version", callback=_version, is_eager=True)
    ] = False,
) -> None:
    """Global options, resolved once for whichever command follows."""
    configure_logging(verbose=verbose, quiet=quiet, log_json=log_json)
    state.set_options(
        bmk=bmk,
        output_format=output_format,
        as_of=as_of,
        source=source,
        offline=offline,
        dry_run=dry_run,
        yes=yes,
        verbose=verbose,
        quiet=quiet,
        no_color=no_color,
    )


def _date(value: str | None, ctx: state.IndexContext, what: str) -> date:
    parsed = parse_date(value, what=what)
    return parsed if parsed is not None else ctx.as_of


def _decimal(value: str | None, what: str) -> Decimal | None:
    return None if value is None else parse_decimal(value, what=what)


def _name(ctx: state.IndexContext) -> str | None:
    return ctx.repos.meta_get("index_name")


def _security(repos: IndexRepositories, spec: str, on: date) -> SyntheticSecurity:
    """A security by its symbol on *on*, or by fafnir's id: ``sid:N``."""
    if spec.lower().startswith("sid:"):
        found = repos.by_fafnir_sid(int(spec[4:]))
        if found is None:
            raise ValidationError(
                f"the index holds no fafnir security {spec[4:]}",
                remedy="Add it with `bmk security add`.",
            )
        return found
    return index_definition.resolve(repos, spec, on)


# ── the file ─────────────────────────────────────────────────────────────────


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="The .bmk file to create.")],
    name: Annotated[str, typer.Option("--name", help="Specific: reports show it.")],
    description: Annotated[str, typer.Option("--description")],
    base_date: Annotated[str, typer.Option("--base-date", help="A session of the calendar.")],
    base_level: Annotated[str, typer.Option("--base-level")] = "100",
    calendar: Annotated[
        str, typer.Option("--calendar", help="The exchange whose sessions it is computed on.")
    ] = "XNYS",
) -> None:
    """Create an index file. Its base date and level never move once it is built."""

    def action() -> CommandResult:
        options = state.options()
        target = path
        scratch: tempfile.TemporaryDirectory[str] | None = None
        if options.dry_run:
            # Rehearsed on a scratch file, so a dry run leaves nothing behind.
            if path.exists():
                raise UsageError(f"{path} already exists")
            scratch = tempfile.TemporaryDirectory()
            target = Path(scratch.name) / path.name
        try:
            ctx = state.open_context(create=target)
            meta = IndexMeta(
                index_name=name,
                description=description,
                base_date=_date(base_date, ctx, "--base-date"),
                base_level=parse_decimal(base_level, what="--base-level"),
                currency="USD",
                calendar=calendar,
            )
            with ctx.write():
                index_definition.create(ctx.repos, meta)
            data = {
                "path": str(path),
                "index_name": meta.index_name,
                "base_date": meta.base_date.isoformat(),
                "base_level": meta.base_level,
                "calendar": meta.calendar.upper(),
            }
            ctx.close()
        finally:
            if scratch is not None:
                state.forget()
                scratch.cleanup()
        return CommandResult(command="init", data=data)

    state.dispatch(action)


@app.command()
def show() -> None:
    """The index's identity, methodology and membership in force, and its build."""

    def action() -> CommandResult:
        ctx = state.open_context()
        repos = ctx.repos
        meta = repos.index_meta()
        methods = [m for m in repos.methodologies() if m.effective_from <= ctx.as_of]
        recons = [r for r in repos.reconstitutions() if r.effective_date <= ctx.as_of]
        build = repos.build()
        current = build is not None and build.inputs_hash == repos.inputs_hash()
        return CommandResult(
            command="show",
            data={
                "index_name": meta.index_name,
                "description": meta.description,
                "base_date": meta.base_date.isoformat(),
                "base_level": meta.base_level,
                "calendar": meta.calendar,
                "methodology": (
                    {
                        "effective_from": methods[-1].effective_from.isoformat(),
                        "weighting": str(methods[-1].weighting),
                        "rebalance_rule": str(methods[-1].rebalance_rule),
                        "drift_band": methods[-1].drift_band,
                    }
                    if methods
                    else None
                ),
                "members": (
                    [
                        {
                            "symbol": repos.security(m.security_id).symbol,
                            "weight": m.weight,
                            "free_float": m.free_float,
                        }
                        for m in recons[-1].members
                    ]
                    if recons
                    else []
                ),
                "membership_from": recons[-1].effective_date.isoformat() if recons else None,
                "built": (
                    {
                        "first_date": build.first_date.isoformat(),
                        "last_date": build.last_date.isoformat(),
                        "inputs_hash": build.inputs_hash,
                        "is_current": current,
                    }
                    if build
                    else None
                ),
                "is_theoretical": True,
            },
            as_of=ctx.as_of,
            portfolio=meta.index_name,
        )

    state.dispatch(action)


# ── securities ───────────────────────────────────────────────────────────────


@security_app.command(name="add")
def security_add(
    symbol: Annotated[str, typer.Argument(help="The ticker on --as-of.")],
    security_type: Annotated[
        str | None,
        typer.Option(
            "--type",
            help="equity | etf | mutual_fund | adr | reit. Given, the security is added by "
            "hand; omitted, it is looked up in the source by its ticker on --as-of.",
        ),
    ] = None,
    name: Annotated[str | None, typer.Option("--name")] = None,
    issuer: Annotated[
        str | None,
        typer.Option("--issuer", help="The issuer (a CIK), so one issuer is held once."),
    ] = None,
) -> None:
    """Add a security. From fafnir it is held by fafnir's id, whatever its ticker."""

    def action() -> CommandResult:
        ctx = state.open_context()
        with ctx.write() as repos:
            if security_type is not None:
                row_id = index_definition.add_security(
                    repos,
                    SyntheticSecurity(
                        security_id=0,
                        symbol=symbol.strip().upper(),
                        security_type=SecurityType(security_type),
                        source="manual",
                        name=name,
                        issuer_key=issuer,
                    ),
                )
                repos.add_symbol(row_id, symbol.strip().upper(), date.min, None, "manual")
                added = repos.security(row_id)
            else:
                added = index_definition.add_security_from_provider(
                    repos, ctx.provider(), symbol, on=ctx.as_of, issuer_key=issuer
                )
        return CommandResult(
            command="security add",
            data={
                "symbol": added.symbol,
                "name": added.name,
                "security_type": str(added.security_type),
                "fafnir_sid": added.fafnir_sid,
                "issuer_key": added.issuer_key,
                "source": added.source,
            },
            portfolio=_name(ctx),
        )

    state.dispatch(action)


@security_app.command(name="list")
def security_list() -> None:
    """Every security the index has, or may, hold."""

    def action() -> CommandResult:
        ctx = state.open_context()
        return CommandResult(
            command="security list",
            table=Table(
                columns=(
                    Column("symbol", "Symbol"),
                    Column("name", "Name"),
                    Column("security_type", "Type"),
                    Column("fafnir_sid", "fafnir id", ColumnKind.INTEGER),
                    Column("issuer_key", "Issuer"),
                    Column("source", "Source"),
                ),
                rows=tuple(
                    {
                        "symbol": s.symbol,
                        "name": s.name,
                        "security_type": str(s.security_type),
                        "fafnir_sid": s.fafnir_sid,
                        "issuer_key": s.issuer_key,
                        "source": s.source,
                    }
                    for s in ctx.repos.securities()
                ),
                title="Securities",
            ),
            portfolio=_name(ctx),
        )

    state.dispatch(action)


# ── the methodology and the membership ───────────────────────────────────────


@methodology_app.command(name="set")
def methodology_set(
    weighting: Annotated[str, typer.Option("--weighting", help="custom | equal | cap | price")],
    rebalance: Annotated[
        str,
        typer.Option(
            "--rebalance",
            help="daily | monthly | quarterly | semiannual | annual | drift_band | never: "
            "at the close of the period's last trading day.",
        ),
    ],
    effective_from: Annotated[
        str | None, typer.Option("--effective-from", help="Default: --as-of.")
    ] = None,
    drift_band: Annotated[
        str | None,
        typer.Option("--drift-band", help="With drift_band: absolute, 0.05 is 5 points."),
    ] = None,
    corrects: Corrects = False,
    note: Note = None,
) -> None:
    """How the index weights and rebalances, from a date's close."""

    def action() -> CommandResult:
        ctx = state.open_context()
        on = _date(effective_from, ctx, "--effective-from")
        try:
            chosen, rule = Weighting(weighting), RebalanceRule(rebalance)
        except ValueError as exc:
            raise UsageError(str(exc), remedy="See `bmk methodology set --help`.") from exc
        with ctx.write() as repos:
            row_id = index_definition.set_methodology(
                repos,
                on,
                chosen,
                rule,
                drift_band=_decimal(drift_band, "--drift-band"),
                corrects=corrects,
                note=note,
            )
        return CommandResult(
            command="methodology set",
            data={
                "methodology_id": row_id,
                "effective_from": on.isoformat(),
                "weighting": str(chosen),
                "rebalance_rule": str(rule),
                "drift_band": _decimal(drift_band, "--drift-band"),
                "corrects": corrects,
            },
            portfolio=_name(ctx),
        )

    state.dispatch(action)


@methodology_app.command(name="show")
def methodology_show() -> None:
    """Every methodology recorded, corrected ones included and marked."""

    def action() -> CommandResult:
        ctx = state.open_context()
        rows = ctx.repos.methodologies(include_corrected=True)
        replaced = {m.corrects_methodology_id for m in rows if m.corrects_methodology_id}
        return CommandResult(
            command="methodology show",
            table=Table(
                columns=(
                    Column("effective_from", "From", ColumnKind.DATE),
                    Column("weighting", "Weighting"),
                    Column("rebalance_rule", "Rebalance"),
                    Column("drift_band", "Band", ColumnKind.RATE),
                    Column("corrected", "Corrected", ColumnKind.BOOL),
                    Column("note", "Note"),
                ),
                rows=tuple(
                    {
                        "effective_from": m.effective_from.isoformat(),
                        "weighting": str(m.weighting),
                        "rebalance_rule": str(m.rebalance_rule),
                        "drift_band": m.drift_band,
                        "corrected": m.methodology_id in replaced,
                        "note": m.note,
                    }
                    for m in rows
                ),
                title="Methodology",
            ),
            portfolio=_name(ctx),
        )

    state.dispatch(action)


def _members(
    repos: IndexRepositories, specs: list[str], on: date, free_float_source: str | None
) -> list[Member]:
    """``SYMBOL``, ``SYMBOL=WEIGHT``, ``SYMBOL/FLOAT`` or ``SYMBOL=WEIGHT/FLOAT``."""
    members: list[Member] = []
    for spec in specs:
        head, _, floating = spec.partition("/")
        symbol, _, weight = head.partition("=")
        if floating and free_float_source is None:
            raise UsageError(
                f"{spec}: a float factor states its source",
                remedy="Pass --free-float-source TEXT: the provider and date it came from.",
            )
        security = _security(repos, symbol.strip(), on)
        members.append(
            Member(
                security_id=security.security_id,
                weight=parse_decimal(weight, what=f"{symbol}'s weight") if weight else None,
                free_float=(
                    parse_decimal(floating, what=f"{symbol}'s float factor")
                    if floating
                    else None
                ),
                free_float_source=free_float_source if floating else None,
            )
        )
    return members


@app.command()
def reconstitute(
    members: Annotated[
        list[str],
        typer.Argument(
            help="Every member: SYMBOL, SYMBOL=WEIGHT (custom), SYMBOL/FLOAT (cap)."
        ),
    ],
    effective_date: Annotated[
        str | None, typer.Option("--effective-date", help="Default: --as-of.")
    ] = None,
    free_float_source: Annotated[
        str | None, typer.Option("--free-float-source", help="Where the free floats came from.")
    ] = None,
    corrects: Corrects = False,
    note: Note = None,
) -> None:
    """The full membership from a date's close. Every member, not the changes."""

    def action() -> CommandResult:
        ctx = state.open_context()
        on = _date(effective_date, ctx, "--effective-date")
        with ctx.write() as repos:
            parsed = _members(repos, members, on, free_float_source)
            row_id = index_definition.reconstitute(
                repos, on, parsed, corrects=corrects, note=note
            )
        return CommandResult(
            command="reconstitute",
            data={
                "reconstitution_id": row_id,
                "effective_date": on.isoformat(),
                "members": [
                    {
                        "symbol": ctx.repos.security(m.security_id).symbol,
                        "weight": m.weight,
                        "free_float": m.free_float,
                    }
                    for m in parsed
                ],
                "corrects": corrects,
            },
            portfolio=_name(ctx),
        )

    state.dispatch(action)


# ── events and schedules ─────────────────────────────────────────────────────


@event_app.command(name="add")
def event_add(
    symbol: Annotated[str, typer.Argument()],
    event_type: Annotated[
        str, typer.Option("--type", help="delisting | cash_merger | stock_merger | spinoff")
    ],
    on: Annotated[
        str,
        typer.Option(
            "--date",
            help="A delisting or merger: the last day held. A spinoff: its ex-date.",
        ),
    ],
    source: Annotated[str, typer.Option("--source", help="Where the terms came from.")],
    cash: Annotated[
        str | None, typer.Option("--cash", help="Paid a share; a delisting at nothing is 0.")
    ] = None,
    receives: Annotated[
        str | None, typer.Option("--receives", help="The security received.")
    ] = None,
    ratio: Annotated[
        str | None, typer.Option("--ratio", help="Received per share held.")
    ] = None,
    corrects: Corrects = False,
    note: Note = None,
) -> None:
    """Declare what a member became. Nothing is assumed when one stops trading."""

    def action() -> CommandResult:
        ctx = state.open_context()
        when = _date(on, ctx, "--date")
        with ctx.write() as repos:
            security = _security(repos, symbol, when)
            received = _security(repos, receives, when) if receives else None
            row_id = index_definition.add_event(
                repos,
                SecurityEvent(
                    event_id=0,
                    security_id=security.security_id,
                    event_date=when,
                    event_type=EventType(event_type),
                    source=source,
                    cash_per_share=_decimal(cash, "--cash"),
                    received_security_id=received.security_id if received else None,
                    received_ratio=_decimal(ratio, "--ratio"),
                    note=note,
                ),
                corrects=corrects,
            )
        return CommandResult(
            command="event add",
            data={
                "event_id": row_id,
                "symbol": security.symbol,
                "event_type": event_type,
                "date": when.isoformat(),
            },
            portfolio=_name(ctx),
        )

    state.dispatch(action)


_PROPOSAL_COLUMNS = (
    Column("symbol", "Symbol"),
    Column("schedule", "Schedule"),
    Column("max_gap_days", "Max gap", ColumnKind.INTEGER),
    Column("dividends", "Dividends", ColumnKind.INTEGER),
    Column("median_gap_days", "Median gap", ColumnKind.INTEGER),
    Column("first_ex", "First ex", ColumnKind.DATE),
    Column("last_ex", "Last ex", ColumnKind.DATE),
)


def _proposal_rows(
    proposals: list[index_definition.ScheduleProposal],
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "symbol": p.symbol,
            "schedule": str(p.schedule),
            "max_gap_days": p.max_gap_days,
            "dividends": p.dividends,
            "median_gap_days": p.median_gap_days,
            "first_ex": p.first_ex.isoformat() if p.first_ex else None,
            "last_ex": p.last_ex.isoformat() if p.last_ex else None,
        }
        for p in proposals
    )


@schedule_app.command(name="propose")
def schedule_propose() -> None:
    """A schedule for each security without one, read from its dividend history.

    Read-only. `bmk schedule accept` declares what this proposes.
    """

    def action() -> CommandResult:
        ctx = state.open_context()
        proposals = index_definition.propose_schedules(ctx.repos)
        return CommandResult(
            command="schedule propose",
            table=Table(
                columns=_PROPOSAL_COLUMNS,
                rows=_proposal_rows(proposals),
                title="Proposed distribution schedules",
                footnotes=(
                    "Read from the dividends the file holds: `bmk refresh` first. Nothing "
                    "is declared until `bmk schedule accept`.",
                ),
            ),
            portfolio=_name(ctx),
        )

    state.dispatch(action)


@schedule_app.command(name="accept")
def schedule_accept() -> None:
    """Declare every proposed schedule, marked as proposed and accepted."""

    def action() -> CommandResult:
        ctx = state.open_context()
        with ctx.write() as repos:
            proposals = index_definition.propose_schedules(repos)
            index_definition.accept_proposals(
                repos, proposals, source=str(ctx.options.source or ctx.config.get("source"))
            )
        return CommandResult(
            command="schedule accept",
            table=Table(
                columns=_PROPOSAL_COLUMNS,
                rows=_proposal_rows(proposals),
                title="Distribution schedules declared",
            ),
            portfolio=_name(ctx),
        )

    state.dispatch(action)


@schedule_app.command(name="set")
def schedule_set(
    symbol: Annotated[str, typer.Argument()],
    schedule: Annotated[
        str,
        typer.Option(
            "--schedule", help="none | monthly | quarterly | semiannual | annual | irregular"
        ),
    ],
    source: Annotated[str, typer.Option("--source", help="Why this schedule.")],
    effective_from: Annotated[
        str | None, typer.Option("--effective-from", help="Default: --as-of.")
    ] = None,
    max_gap_days: Annotated[
        int | None, typer.Option("--max-gap-days", help="With irregular: the longest gap.")
    ] = None,
    corrects: Corrects = False,
) -> None:
    """Declare how a security pays, from a date."""

    def action() -> CommandResult:
        ctx = state.open_context()
        on = _date(effective_from, ctx, "--effective-from")
        with ctx.write() as repos:
            security = _security(repos, symbol, on)
            row_id = index_definition.set_schedule(
                repos,
                security.security_id,
                on,
                Schedule(schedule),
                source=source,
                max_gap_days=max_gap_days,
                corrects=corrects,
            )
        return CommandResult(
            command="schedule set",
            data={
                "schedule_id": row_id,
                "symbol": security.symbol,
                "schedule": schedule,
                "effective_from": on.isoformat(),
            },
            portfolio=_name(ctx),
        )

    state.dispatch(action)


# ── inputs and the build ─────────────────────────────────────────────────────


@app.command()
def refresh(
    through: Annotated[str | None, typer.Option("--through", help="Default: --as-of.")] = None,
    restate: Annotated[
        str | None,
        typer.Option(
            "--restate",
            help="Accept the source's changes to inputs already stored, for this reason.",
        ),
    ] = None,
) -> None:
    """Read the calendar, closes, splits, dividends and caps from the source."""

    def action() -> CommandResult:
        ctx = state.open_context()
        end = _date(through, ctx, "--through")
        with ctx.write() as repos:
            report = index_inputs.refresh(
                repos, ctx.provider(), through=end, restate_reason=restate
            )
        return CommandResult(
            command="refresh",
            data={
                "through": report.through.isoformat(),
                "trading_days": report.trading_days,
                "closes": report.closes,
                "splits": report.splits,
                "dividends": report.dividends,
                "market_caps": report.market_caps,
                "restated": report.restated,
                "not_read": report.not_read,
            },
            warnings=tuple(
                f"not read: {item}; a spinoff or merger is declared with `bmk event add`"
                for item in report.not_read
            ),
            portfolio=_name(ctx),
        )

    state.dispatch(action)


@app.command()
def build(
    through: Annotated[
        str | None, typer.Option("--through", help="Default: the last session held.")
    ] = None,
) -> None:
    """Build every level from the base date, replacing the built rows."""

    def action() -> CommandResult:
        ctx = state.open_context()
        end = parse_date(through, what="--through")
        with ctx.write() as repos:
            record = index_construction.build(
                repos, through=end, built_by=f"portable {__version__}"
            )
            levels = repos.levels(record.last_date, record.last_date)
        last = levels[-1]
        return CommandResult(
            command="build",
            data={
                "first_date": record.first_date.isoformat(),
                "last_date": record.last_date.isoformat(),
                "price_level": last.price_level,
                "total_return_level": last.total_return_level,
                "inputs_hash": record.inputs_hash,
            },
            portfolio=_name(ctx),
        )

    state.dispatch(action)


_LEVEL_COLUMNS = (
    Column("date", "Date", ColumnKind.DATE),
    Column("total_return_level", "Total return", ColumnKind.PRICE),
    Column("price_level", "Price only", ColumnKind.PRICE),
    Column("dividend_points", "Dividend pts", ColumnKind.PRICE),
    Column("rebalance", "Rebalance", ColumnKind.BOOL),
    Column("reconstitution", "Reconstitution", ColumnKind.BOOL),
    Column("members", "Held", ColumnKind.INTEGER),
)


@app.command()
def levels(
    start: Annotated[str | None, typer.Option("--from", help="Default: the base date.")] = None,
    end: Annotated[
        str | None, typer.Option("--to", help="Default: the last day built.")
    ] = None,
) -> None:
    """The levels built, refused if the inputs have changed since the build."""

    def action() -> CommandResult:
        ctx = state.open_context()
        repos = ctx.repos
        record = index_construction.current_build(repos)
        meta = repos.index_meta()
        rows = repos.levels(parse_date(start, what="--from"), parse_date(end, what="--to"))
        return CommandResult(
            command="levels",
            table=Table(
                columns=_LEVEL_COLUMNS,
                rows=tuple(
                    {
                        "date": r.level_date.isoformat(),
                        "total_return_level": r.total_return_level,
                        "price_level": r.price_level,
                        "dividend_points": r.dividend_points,
                        "rebalance": r.is_rebalance,
                        "reconstitution": r.is_reconstitution,
                        "members": r.member_count,
                    }
                    for r in rows
                ),
                title=meta.index_name,
                footnotes=(
                    "Synthetic and theoretical: built from its constituents' closes and "
                    "every dividend they paid, gross of withholding, with no costs. Never "
                    "linked onto a portfolio's own returns (PORT-GIPS-J04).",
                    "Total return reinvests each dividend across the index at its ex-date "
                    "close. The price-only level is never a benchmark (PORT-GIPS-G01).",
                ),
            ),
            data={
                "index_name": meta.index_name,
                "description": meta.description,
                "base_date": meta.base_date.isoformat(),
                "base_level": meta.base_level,
                "currency": meta.currency,
                "calendar": meta.calendar,
                "inputs_hash": record.inputs_hash,
                "is_theoretical": True,
                "is_net_of_withholding": False,
                "return_types": {
                    "total_return_level": "total_return",
                    "price_level": "price_only",
                },
            },
            human_data=False,
            schema_ref="bmk-levels-1.0.json",
            portfolio=meta.index_name,
        )

    state.dispatch(action)


@app.command()
def holdings() -> None:
    """What the index held on --as-of: units, close, points and weight."""

    def action() -> CommandResult:
        ctx = state.open_context()
        level, held = index_construction.composition(ctx.repos, ctx.as_of)
        return CommandResult(
            command="holdings",
            table=Table(
                columns=(
                    Column("symbol", "Symbol"),
                    Column("units", "Units", ColumnKind.QUANTITY),
                    Column("close", "Close", ColumnKind.PRICE),
                    Column("points", "Points", ColumnKind.PRICE),
                    Column("weight", "Weight", ColumnKind.RATE),
                ),
                rows=tuple(
                    {
                        "symbol": h.security.symbol,
                        "units": h.units,
                        "close": h.close,
                        "points": h.points,
                        "weight": h.weight,
                    }
                    for h in held
                ),
                title=f"Held at the close of {ctx.as_of.isoformat()}",
            ),
            data={
                "price_level": level.price_level,
                "total_return_level": level.total_return_level,
                "divisor": level.divisor,
            },
            human_data=False,
            schema_ref="bmk-holdings-1.0.json",
            as_of=ctx.as_of,
            portfolio=_name(ctx),
        )

    state.dispatch(action)


@app.command()
def validate() -> None:
    """Check the file: its metadata, its schema, and that a rebuild reproduces it."""

    def action() -> CommandResult:
        import portable_core.schema.bmk as bmk_schema

        ctx = state.open_context()
        problems = [("schema", p) for p in bmk_schema.verify_checksums(ctx.connection)]
        problems += index_construction.verify(ctx.repos)
        if problems:
            raise ValidationError(
                f"{len(problems)} problem(s) found",
                code=E_INVARIANT_BROKEN,
                remedy="Each names its check; `bmk build` rebuilds the levels.",
                problems=[f"{check}: {problem}" for check, problem in problems],
            )
        return CommandResult(
            command="validate",
            data={"problems": 0},
            portfolio=_name(ctx),
        )

    state.dispatch(action)
