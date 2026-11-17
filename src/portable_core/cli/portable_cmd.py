"""Entry point for the `portable` command: what belongs to the family, not one CLI.

Its one verb is `portable config show`, which `docs/architecture.md` §6
specifies: the effective configuration, and where each value came from.

It lives in `portable_core` rather than in a CLI package because configuration
is shared by every CLI, and no CLI may import another.

**It reports only what takes effect.** Eight keys are read from configuration
by some command. The rest of :data:`~portable_core.config.DEFAULTS` -- the
output, confirmation and offline flags -- are taken by every command from its
own command line alone, so a value a file or a ``PORTABLE_*`` variable gives one
does nothing. Listing such a value beside the others would claim an effect it
does not have, so each is a warning instead, and so is a key no command reads
at all, which is usually a typo.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Final

import typer

from portable_core import __version__
from portable_core.cli import argv as _argv
from portable_core.cli.context import GlobalOptions, make_options
from portable_core.cli.runner import configure_logging, render_error, run_command
from portable_core.cli.streams import configure_streams
from portable_core.config import FLAG_ONLY, Config, ConfigValue, Source, load_config
from portable_core.config.settings import PROJECT_CONFIG, user_config_path
from portable_core.errors import PortableError
from portable_core.formatters import Column, CommandResult, OutputFormat, Table

__all__ = [
    "GLOBAL_FLAGS",
    "GLOBAL_VALUE_FLAGS",
    "app",
    "config_result",
    "main",
    "normalise_argv",
]

#: Global flags that take no value. ``-vv`` and ``-vvv`` are ``-v`` repeated,
#: written as one argument. Held to the root callback's options by
#: `tests/integration/test_global_flags_cli.py`.
GLOBAL_FLAGS: Final[frozenset[str]] = frozenset(
    {"--quiet", "-q", "--no-color", "--log-json", "-v", "-vv", "-vvv", "--verbose"}
)

#: Global flags that consume the following arg as their value.
GLOBAL_VALUE_FLAGS: Final[frozenset[str]] = frozenset({"--port", "--format", "-f", "--as-of"})

_PRECEDENCE: Final = (
    "Highest precedence first: a flag, a PORTABLE_* variable, ./portable.toml, "
    "~/.portablerc, the built-in default."
)

app = typer.Typer(
    name="portable",
    help=(
        "portable -- what the whole CLI family shares.\n\n"
        "`portable config show` prints the effective configuration and where each "
        "value came from.\n\n"
        "Exit codes: 0 ok, 1 generic, 2 usage, 3 portfolio/file, 4 validation, "
        "5 data unavailable, 6 reconciliation break."
    ),
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    rich_markup_mode=None,
)
config_app = typer.Typer(
    help="Configuration, shared by every portable CLI.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    rich_markup_mode=None,
)
app.add_typer(config_app, name="config")

_OPTIONS = GlobalOptions()


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit(0)


@app.callback()
def _root(
    port: Annotated[
        Path | None,
        # No `envvar`: PORTABLE_PORT is read by the configuration layers, so
        # `config show` can say it came from the environment, not a flag.
        typer.Option("--port", help="The .port file, as a flag."),
    ] = None,
    output_format: Annotated[
        str, typer.Option("--format", "-f", help="table | json | markdown | csv")
    ] = "table",
    as_of: Annotated[
        str | None,
        typer.Option(
            "--as-of",
            help=(
                "The date the report is labelled with. Defaults to today, explicitly. "
                "Configuration is read as it stands when the command runs."
            ),
        ),
    ] = None,
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
    global _OPTIONS
    configure_logging(verbose=verbose, quiet=quiet, log_json=log_json)
    try:
        _OPTIONS = make_options(
            port=port,
            output_format=output_format,
            as_of=as_of,
            verbose=verbose,
            quiet=quiet,
            no_color=no_color,
        )
    except PortableError as error:
        # Rendered, never a traceback: a bad --format or --as-of is a usage
        # error with a remedy like any other (`CLAUDE.md`, "Errors").
        json_wanted = output_format == str(OutputFormat.JSON)
        render_error(error, OutputFormat.JSON if json_wanted else OutputFormat.TABLE)
        raise typer.Exit(int(error.exit_code)) from None


def _dispatch(action: Callable[[], CommandResult]) -> None:
    code = run_command(
        action,
        output_format=_OPTIONS.output_format,
        no_color=_OPTIONS.no_color,
        verbose=_OPTIONS.verbose,
        quiet=_OPTIONS.quiet,
    )
    sys.stdout.flush()
    raise typer.Exit(code)


@config_app.command("show")
def show() -> None:
    """The effective configuration, and where each value came from.

    Only settings a command reads. A value set in a file or a PORTABLE_*
    variable that no command reads is a warning, not a row. Secrets are
    redacted. Writes nothing.
    """

    def action() -> CommandResult:
        flags = {"port": str(_OPTIONS.port) if _OPTIONS.port else None}
        return config_result(
            load_config(flags),
            # Defaulted here, explicitly, as every command defaults it
            # (`CLAUDE.md` invariant 6).
            as_of=_OPTIONS.as_of or datetime.now(UTC).date(),
        )

    _dispatch(action)


def config_result(config: Config, *, as_of: date | None = None) -> CommandResult:
    """Lay out *config*: one row per setting in effect, a warning per ignored value."""
    user = user_config_path()
    return CommandResult(
        command="config show",
        table=Table(
            columns=(
                Column("key", "Setting"),
                Column("value", "Value"),
                Column("source", "Source"),
                Column("origin", "From"),
            ),
            rows=tuple(config.to_rows()),
            title="Effective configuration",
            footnotes=(_PRECEDENCE, "Secrets are redacted."),
        ),
        data={
            "precedence": [str(source) for source in Source],
            "files": {
                "project": {"path": str(PROJECT_CONFIG), "found": PROJECT_CONFIG.is_file()},
                "user": {
                    "path": None if user is None else str(user),
                    "found": user is not None and user.is_file(),
                },
            },
            "flag_only": dict(sorted(FLAG_ONLY.items())),
        },
        warnings=tuple(_ignored(entry) for entry in config.ignored()),
        as_of=as_of,
    )


def _ignored(entry: ConfigValue) -> str:
    """Why *entry* has no effect, naming where it was set. Never its value."""
    if entry.source is Source.ENV:
        subject = f"`{entry.origin}`"
    else:
        subject = f"`{entry.key}` in {entry.origin}"
    flag = FLAG_ONLY.get(entry.key)
    if flag is not None:
        return f"{subject} has no effect: it is set only by {flag}, on the command line."
    return f"{subject} has no effect: no command reads a setting named `{entry.key}`."


def normalise_argv(argv: list[str]) -> list[str]:
    """``portable config show --format json`` becomes ``portable --format json config show``."""
    return _argv.normalise_argv(argv, flags=GLOBAL_FLAGS, value_flags=GLOBAL_VALUE_FLAGS)


def main() -> None:
    """Run the Typer application, with UTF-8 streams and normalised ``argv``.

    As for `pt` and `pert`: see :mod:`portable_core.cli.streams` and
    :mod:`portable_core.cli.argv`.
    """
    configure_streams()
    app(args=normalise_argv(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover
    main()
