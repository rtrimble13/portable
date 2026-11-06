"""A command's warnings reach the reader in every format.

Table and CSV output used to drop them: a dry run's notice that nothing was
written never appeared, so a dry run and the real write looked the same. Those
formats now write each warning to stderr. JSON keeps them in the envelope and
Markdown in a section of its own, and neither writes to stderr.
"""

from __future__ import annotations

import json

import pytest

from portable_core.cli.runner import dry_run_note, run_command
from portable_core.formatters import CommandResult, OutputFormat

pytestmark = pytest.mark.unit


def _warned() -> CommandResult:
    return dry_run_note(
        CommandResult(command="demo", data={"n": 1}, warnings=("a back-dated row rebuilt",))
    )


@pytest.mark.parametrize("output_format", [OutputFormat.TABLE, OutputFormat.CSV])
def test_table_and_csv_write_warnings_to_stderr_and_only_there(
    output_format: OutputFormat, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_command(_warned, output_format=output_format) == 0
    out, err = capsys.readouterr()
    assert err.splitlines() == [
        "warning: a back-dated row rebuilt",
        "warning: DRY RUN: nothing was written to the portfolio.",
    ]
    assert "DRY RUN" not in out


def test_json_carries_warnings_in_the_envelope_not_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_command(_warned, output_format=OutputFormat.JSON) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert "DRY RUN: nothing was written to the portfolio." in json.loads(out)["warnings"]


def test_markdown_renders_warnings_in_band_not_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_command(_warned, output_format=OutputFormat.MARKDOWN) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert "**Warnings**" in out and "DRY RUN" in out


def test_quiet_suppresses_them(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_command(_warned, output_format=OutputFormat.TABLE, quiet=True) == 0
    assert capsys.readouterr().err == ""


def test_a_result_with_no_warnings_writes_nothing_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_command(lambda: CommandResult(command="demo", data={"n": 1}))
    assert capsys.readouterr().err == ""
