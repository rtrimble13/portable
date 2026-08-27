"""The command context: what every `pt` command is handed.

A CLI command's whole job is: parse, build one of these, call one service, hand
the result to a formatter, choose an exit code (`docs/architecture.md` §2). The
context is what carries the first step's output to the rest.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from portable_core.config import Config, load_config
from portable_core.errors import PortfolioFileError, UsageError
from portable_core.errors.kinds import E_PORTFOLIO_NOT_FOUND
from portable_core.formatters import OutputFormat
from portable_core.persistence.connection import open_portfolio
from portable_core.persistence.repositories import Repositories
from portable_core.providers import MarketDataProvider, NullProvider, build_provider
from portable_core.schema import migrations as M

__all__ = ["CommandContext", "GlobalOptions", "build_context"]


@dataclass(frozen=True, slots=True)
class GlobalOptions:
    """The flags every `pt` command accepts (bootstrap §7.2)."""

    port: Path | None = None
    output_format: OutputFormat = OutputFormat.TABLE
    as_of: date | None = None
    source: str | None = None
    offline: bool = False
    dry_run: bool = False
    yes: bool = False
    verbose: int = 0
    quiet: bool = False
    no_color: bool = False


@dataclass(slots=True)
class CommandContext:
    """Everything a command needs, resolved once.

    ``as_of`` is **always set** -- defaulted explicitly to today at build time
    rather than left as None for a service to interpret as "now". `CLAUDE.md`
    invariant 6: every command takes ``--as-of`` and defaults it explicitly, so
    that a report can be reproduced by passing back the date it printed.
    """

    options: GlobalOptions
    config: Config
    as_of: date
    connection: sqlite3.Connection | None = None
    repos: Repositories | None = None
    _provider: MarketDataProvider | None = field(default=None, repr=False)

    @property
    def output_format(self) -> OutputFormat:
        return self.options.output_format

    @property
    def dry_run(self) -> bool:
        return self.options.dry_run

    def require_portfolio(self) -> Repositories:
        """The repositories, or a clear error saying how to point at a file."""
        if self.repos is None:
            raise PortfolioFileError(
                "no portfolio file specified",
                code=E_PORTFOLIO_NOT_FOUND,
                remedy=(
                    "Pass --port FILE, set PORTABLE_PORT, or set `port` in "
                    "~/.portablerc. `pt init FILE` creates a new one."
                ),
            )
        return self.repos

    def provider(self) -> MarketDataProvider:
        """The configured market data provider, built once and reused.

        Defaults to :class:`NullProvider` rather than to a guess, so a command
        that needs prices with nothing configured fails with a sentence telling
        the user what to do.
        """
        if self._provider is None:
            name = self.options.source or str(self.config.get("source") or "null")
            self._provider = (
                NullProvider()
                if name == "null"
                else build_provider(
                    name,
                    price_file=self.config.get("price_file"),
                    action_file=self.config.get("action_file"),
                    benchmark_file=self.config.get("benchmark_file"),
                    fafnir_dsn=self.config.get("fafnir_dsn"),
                    duk_path=self.config.get("duk_path"),
                )
            )
        return self._provider

    def portfolio_name(self) -> str | None:
        if self.repos is None:
            return None
        return self.repos.meta.get("portfolio_name")

    def confirm(self, prompt: str) -> bool:
        """Ask for confirmation, unless ``--yes`` supplied the answer.

        Non-interactive operation is a requirement, not a nicety (bootstrap
        §6.6): anything that could prompt has a flag that supplies the answer.
        On a non-TTY with no ``--yes`` this refuses rather than blocking
        forever, because a CI job that hangs on a hidden prompt is worse than
        one that fails.
        """
        import sys

        if self.options.yes:
            return True
        if not sys.stdin.isatty():
            raise UsageError(
                f"{prompt} -- refusing to prompt on a non-interactive stream",
                remedy="Pass --yes to confirm, or --dry-run to see the effects first.",
            )
        answer = input(f"{prompt} [y/N] ").strip().lower()
        return answer in {"y", "yes"}

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
            self.repos = None


def build_context(
    options: GlobalOptions,
    *,
    require_portfolio: bool = True,
    check_schema: bool = True,
) -> CommandContext:
    """Resolve config, open the portfolio, and default ``as_of`` explicitly."""
    config = load_config(
        {
            "port": str(options.port) if options.port else None,
            "format": str(options.output_format),
            "source": options.source,
            "offline": options.offline or None,
            "no_color": options.no_color or None,
            "quiet": options.quiet or None,
            "dry_run": options.dry_run or None,
            "yes": options.yes or None,
        }
    )

    # Defaulted here, once, and explicitly -- never implicitly to "now" deeper
    # in a service where it would become a hidden clock dependency.
    as_of = options.as_of or datetime.now(UTC).date()

    path_text = config.get("port")
    connection: sqlite3.Connection | None = None
    repos: Repositories | None = None

    if path_text:
        path = Path(str(path_text))
        connection = open_portfolio(path)
        if check_schema:
            M.check_openable(connection, path)
        repos = Repositories(connection)
    elif require_portfolio:
        raise PortfolioFileError(
            "no portfolio file specified",
            code=E_PORTFOLIO_NOT_FOUND,
            remedy=(
                "Pass --port FILE, set PORTABLE_PORT, or set `port` in "
                "~/.portablerc. `pt init FILE` creates a new one."
            ),
        )

    return CommandContext(
        options=options,
        config=config,
        as_of=as_of,
        connection=connection,
        repos=repos,
    )


def parse_date(value: str | None, *, what: str = "date") -> date | None:
    """Parse an ISO date from the command line, refusing anything else.

    No natural-language dates, no locale-dependent formats. `CLAUDE.md`
    invariant 6 forbids locale dependence in output, and accepting "03/04/2025"
    on input would make the *meaning* of a command locale-dependent -- which is
    worse, because it changes a trade date rather than a rendering.
    """
    if value is None:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise UsageError(
            f"{what} must be an ISO date (YYYY-MM-DD), got {value!r}",
            remedy="Dates are always YYYY-MM-DD, in every command and every format.",
            value=value,
        ) from exc


def parse_decimal(value: str | None, *, what: str = "amount") -> Any:
    """Parse a Decimal from the command line.

    Goes through the canonical text form, so a value typed at a terminal and a
    value read from the database are the same object by the same route
    (ADR 0005).
    """
    from portable_core.decimals import from_text

    if value is None:
        return None
    try:
        return from_text(str(value).replace(",", "").replace("$", "").strip())
    except ValueError as exc:
        raise UsageError(
            f"{what} must be a decimal number, got {value!r}",
            value=value,
        ) from exc
