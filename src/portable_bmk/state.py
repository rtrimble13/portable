"""Global flag state and the open index, shared by `bmk`'s callback and commands.

The `.bmk` counterpart of `portable_pt.state`. The flags `pt` has that mean
nothing to an index (`--port`) are absent; `--bmk` names the file.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path

import typer

import portable_core.schema.bmk as bmk_schema
from portable_core.cli.context import parse_date
from portable_core.cli.runner import dry_run_note, run_command
from portable_core.config import Config, load_config
from portable_core.errors import PortfolioFileError, UsageError
from portable_core.errors.kinds import E_PORTFOLIO_NOT_FOUND
from portable_core.formatters import CommandResult, OutputFormat
from portable_core.persistence.connection import open_index, transaction
from portable_core.persistence.index_repositories import IndexRepositories
from portable_core.providers import (
    MarketDataProvider,
    NullProvider,
    OfflineProvider,
    build_provider,
)

__all__ = ["IndexContext", "Options", "dispatch", "open_context", "set_options"]


@dataclass(frozen=True, slots=True)
class Options:
    """The flags every `bmk` command accepts."""

    bmk: Path | None = None
    output_format: OutputFormat = OutputFormat.TABLE
    as_of: date | None = None
    source: str | None = None
    offline: bool = False
    dry_run: bool = False
    yes: bool = False
    verbose: int = 0
    quiet: bool = False
    no_color: bool = False


_OPTIONS = Options()
_OPENED: list[IndexContext] = []


def set_options(
    *,
    bmk: Path | None,
    output_format: str,
    as_of: str | None,
    source: str | None,
    offline: bool,
    dry_run: bool,
    yes: bool,
    verbose: int,
    quiet: bool,
    no_color: bool,
) -> None:
    global _OPTIONS
    try:
        fmt = OutputFormat(output_format)
    except ValueError as exc:
        raise UsageError(
            f"unknown output format {output_format!r}",
            remedy="Choose one of: table, json, markdown, csv.",
        ) from exc
    _OPTIONS = Options(
        bmk=bmk,
        output_format=fmt,
        as_of=parse_date(as_of, what="--as-of"),
        source=source,
        offline=offline,
        dry_run=dry_run,
        yes=yes,
        verbose=verbose,
        quiet=quiet,
        no_color=no_color,
    )


@dataclass(slots=True)
class IndexContext:
    """The open index, the configuration, and an explicit ``as_of``."""

    options: Options
    config: Config
    as_of: date
    path: Path
    connection: sqlite3.Connection
    repos: IndexRepositories
    wrote: bool = False
    _provider: MarketDataProvider | None = field(default=None, repr=False)

    def provider(self) -> MarketDataProvider:
        if self._provider is None:
            name = self.options.source or str(self.config.get("source") or "null")
            self._provider = (
                OfflineProvider(name)
                if self.options.offline
                else NullProvider()
                if name == "null"
                else build_provider(
                    name,
                    price_file=self.config.get("price_file"),
                    action_file=self.config.get("action_file"),
                    calendar_file=self.config.get("calendar_file"),
                    cap_file=self.config.get("cap_file"),
                    fafnir_dsn=self.config.get("fafnir_dsn"),
                    duk_path=self.config.get("duk_path"),
                )
            )
        return self._provider

    @contextmanager
    def write(self) -> Iterator[IndexRepositories]:
        """One transaction, or under --dry-run the real write rolled back (ADR 0026)."""
        self.wrote = True
        con = self.connection
        if not self.options.dry_run:
            with transaction(con):
                yield self.repos
            return
        if not con.in_transaction:
            con.execute("BEGIN IMMEDIATE")
        con.execute("SAVEPOINT dry_run")
        try:
            yield self.repos
        except BaseException:
            con.execute("ROLLBACK TO dry_run")
            con.execute("RELEASE dry_run")
            raise
        con.execute("RELEASE dry_run")

    def close(self) -> None:
        if self.connection.in_transaction:
            self.connection.execute("ROLLBACK")
        self.connection.close()


def _config() -> Config:
    options = _OPTIONS
    return load_config(
        {
            "bmk": str(options.bmk) if options.bmk else None,
            "format": str(options.output_format),
            "source": options.source,
            "offline": options.offline or None,
        }
    )


def as_of() -> date:
    """``--as-of``, defaulted explicitly to today, once (`CLAUDE.md` invariant 6)."""
    return _OPTIONS.as_of or datetime.now(UTC).date()


def options() -> Options:
    return _OPTIONS


def open_context(*, create: Path | None = None) -> IndexContext:
    """Open the index the flags name, or create the file *create* names."""
    config = _config()
    if create is not None:
        if create.exists():
            raise PortfolioFileError(
                f"{create} already exists",
                code="PT-E-PORTFOLIO-EXISTS",
                remedy="Choose another name: an index file is never overwritten.",
                path=str(create),
            )
        path = create
        connection = open_index(path, must_exist=False)
        bmk_schema.initialise(connection)
    else:
        text = config.get("bmk")
        if not text:
            raise PortfolioFileError(
                "no index file specified",
                code=E_PORTFOLIO_NOT_FOUND,
                remedy="Pass --bmk FILE or set PORTABLE_BMK. `bmk init FILE` creates one.",
            )
        path = Path(str(text))
        connection = open_index(path)
        try:
            bmk_schema.check_openable(connection, path)
        except BaseException:
            connection.close()
            raise
    ctx = IndexContext(
        options=_OPTIONS,
        config=config,
        as_of=as_of(),
        path=path,
        connection=connection,
        repos=IndexRepositories(connection),
    )
    _OPENED.append(ctx)
    return ctx


def forget() -> None:
    """Close and forget what is open: a dry-run `init`'s scratch file is removed."""
    while _OPENED:
        ctx = _OPENED.pop()
        with suppress(sqlite3.ProgrammingError):  # closed already
            ctx.close()


def dispatch(action: Callable[[], CommandResult]) -> None:
    """Run a command, render it, close what it opened, and exit with its code."""
    _OPENED.clear()

    def reported() -> CommandResult:
        result = action()
        if _OPTIONS.dry_run and (any(c.wrote for c in _OPENED) or result.command == "init"):
            noted = dry_run_note(result)
            result = replace(
                noted,
                warnings=tuple(w.replace("the portfolio", "the index") for w in noted.warnings),
            )
        return result

    try:
        code = run_command(
            reported,
            output_format=_OPTIONS.output_format,
            no_color=_OPTIONS.no_color,
            verbose=_OPTIONS.verbose,
            quiet=_OPTIONS.quiet,
        )
    finally:
        forget()
    raise typer.Exit(code)
