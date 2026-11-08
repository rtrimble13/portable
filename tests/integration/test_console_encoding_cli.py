"""What `pt` writes does not depend on the console's code page.

On Windows, once stdout is a pipe or a file -- a redirect, ``| jq``, an agent,
this harness -- Python encodes it in the ANSI code page, cp1252 on a US
install. ``PYTHONIOENCODING=cp1252`` gives a Linux run the same stdout, so CI
on both platforms reproduces what the owner's console does.

Before `portable_core.cli.streams`, a ``→`` or a ``Δ`` in the output raised
``UnicodeEncodeError`` after the command had done its work, and a ``—`` came
out as a different byte on Windows than everywhere else. The harness decodes
stdout as strict UTF-8, so a cp1252 byte fails these tests as surely as the
exception did.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.integration.conftest import CliResult, CliRunner

pytestmark = [pytest.mark.integration]

CP1252 = {"PYTHONIOENCODING": "cp1252"}
UTF8 = {"PYTHONIOENCODING": "utf-8"}


@pytest.fixture
def held(run_pt: CliRunner, portfolio: Path) -> Path:
    """One lot of a fund with a sibling share class to convert it into."""
    for symbol in ("FUNDI", "FUNDR"):
        run_pt(
            "--port",
            str(portfolio),
            "instrument",
            "add",
            symbol,
            "--name",
            symbol,
            "--type",
            "mutual_fund",
        )
    run_pt(
        "--port",
        str(portfolio),
        "buy",
        "FUNDI",
        "-a",
        "B",
        "--qty",
        "100",
        "--price",
        "10.00",
        "--date",
        "2024-02-01",
    )
    return portfolio


def _convert(
    run_pt: CliRunner, port: Path, *, fmt: str, env: dict[str, str], dry_run: bool = False
) -> CliResult:
    return run_pt(
        "--port",
        str(port),
        *(["--dry-run"] if dry_run else []),
        "ca",
        "convert",
        "FUNDI",
        "--to",
        "FUNDR",
        "--units",
        "99",
        "--date",
        "2024-03-10",
        "-a",
        "B",
        fmt=fmt,
        extra_env=env,
    )


def _without_clock(json_text: str) -> str:
    """The envelope with ``generated_at`` blanked: the one wall-clock value."""
    return re.sub(r'"generated_at": "[^"]*"', '"generated_at": ""', json_text)


def test_a_table_with_an_arrow_is_written_to_a_cp1252_console(
    run_pt: CliRunner, held: Path
) -> None:
    """The reported crash: `pt ca convert`, whose table title is ``FUNDI → FUNDR``."""
    converted = _convert(run_pt, held, fmt="table", env=CP1252).ok()

    assert "FUNDI → FUNDR in B, 2024-03-10" in converted.stdout
    # The write the crash used to hide happened, and the next command sees it.
    rows = {r["symbol"]: r for r in run_pt("--port", str(held), "holdings").data["rows"]}
    assert "FUNDI" not in rows
    assert rows["FUNDR"]["quantity"] == "99"


def test_json_is_the_same_whatever_the_console_encoding(run_pt: CliRunner, held: Path) -> None:
    """Invariant 6: the envelope is UTF-8, and identical, on every console.

    A dry run, so the same conversion can be rendered twice from the same
    ledger (ADR 0026): its title carries ``→``.
    """
    utf8, cp1252 = (
        _convert(run_pt, held, fmt="json", env=env, dry_run=True).stdout
        for env in (UTF8, CP1252)
    )
    assert "FUNDI → FUNDR" in utf8
    assert _without_clock(cp1252) == _without_clock(utf8)


def test_a_lot_reads_the_same_whatever_the_console_encoding(
    run_pt: CliRunner, held: Path
) -> None:
    """`pt lot show`: ``Basis Δ`` and ``Qty Δ``, neither of which cp1252 has."""
    (lot,) = run_pt("--port", str(held), "lot", "list").data["rows"]
    for fmt in ("table", "json", "markdown", "csv"):
        utf8, cp1252 = (
            run_pt(
                "--port", str(held), "lot", "show", str(lot["lot_id"]), fmt=fmt, extra_env=env
            ).stdout
            for env in (UTF8, CP1252)
        )
        assert "Basis Δ" in utf8, fmt
        assert _without_clock(cp1252) == _without_clock(utf8), fmt
