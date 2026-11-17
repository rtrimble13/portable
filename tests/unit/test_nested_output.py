"""Nested data in the human formats: indented lines, never a Python repr.

Issue 63: `pt tax` printed `basis_provenance`, `bonds` and `wash_sales` under
its table as Python dicts and lists, and the colour path showed
``Decimal('...')``. A command's ``data`` and an error's ``context`` now go
through one renderer, ``portable_core.formatters.nested``.

GIPS acceptance tests:
    test_a_return_in_data_is_labelled_and_never_annualized_under_a_year
        (PORT-GIPS-B07, PORT-GIPS-H04)
"""

from __future__ import annotations

import io
import re
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from portable_core.cli.runner import render_error
from portable_core.errors import GipsRefusalError, ValidationError
from portable_core.errors.kinds import E_REPLAY_MISMATCH
from portable_core.formatters import CommandResult, OutputFormat, ReturnValue, render

pytestmark = pytest.mark.unit

#: The shapes `pt tax` puts in `data`, with keys out of order at every depth.
DATA: dict[str, object] = {
    "total_gain": D("15250.68"),
    "basis_provenance": [{"gain": D("15250.68"), "basis_source": "derived", "dispositions": 2}],
    "bonds": {
        "not_computed": ["bond premium amortization (IRC §171)", "market discount (IRC §1276)"],
        "held": ["T-4.25-2030"],
    },
    "unreportable": [],
    "account": None,
    "sheltered_accounts": ["Rollover IRA", "Roth IRA"],
}

PLAIN = """\
account             —
basis_provenance
  - basis_source  derived
    dispositions  2
    gain          15250.68
bonds
  held          T-4.25-2030
  not_computed
    - bond premium amortization (IRC §171)
    - market discount (IRC §1276)
sheltered_accounts
  - Rollover IRA
  - Roth IRA
total_gain          15250.68
unreportable        (none)
"""

MARKDOWN = """\
- **account**: —
- **basis_provenance**:
  1. - **basis_source**: derived
     - **dispositions**: 2
     - **gain**: 15250.68
- **bonds**:
  - **held**: T-4.25-2030
  - **not_computed**:
    - bond premium amortization (IRC §171)
    - market discount (IRC §1276)
- **sheltered_accounts**:
  - Rollover IRA
  - Roth IRA
- **total_gain**: 15250.68
- **unreportable**: (none)
"""

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class _Tty(io.StringIO):
    """A stream that says it is a terminal, so the table takes the colour path."""

    def isatty(self) -> bool:
        return True


def _result(data: dict[str, object]) -> CommandResult:
    return CommandResult(command="tax", data=data)


def _plain(data: dict[str, object]) -> str:
    return render(_result(data), OutputFormat.TABLE, stream=io.StringIO())


def _colour(data: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.delenv("NO_COLOR", raising=False)
    return _ANSI.sub("", render(_result(data), OutputFormat.TABLE, stream=_Tty()))


def test_nested_data_is_indented_lines_never_a_repr() -> None:
    """Keys sort at every depth, as `--format json` sorts them; lists keep order."""
    assert _plain(DATA) == PLAIN


def test_empty_is_not_null() -> None:
    """A check that found nothing and a figure that is missing must differ."""
    text = _plain({"found": [], "figure": None, "nested": {}})
    assert "found   (none)" in text
    assert "figure  —" in text
    assert "nested  (none)" in text


def test_markdown_nests_the_same_structure() -> None:
    assert render(_result(DATA), OutputFormat.MARKDOWN, stream=io.StringIO()) == MARKDOWN


def test_the_colour_path_renders_the_same_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """What a person at a terminal sees. It printed `Decimal('15250.68')`."""
    text = _colour(DATA, monkeypatch)
    assert "Decimal(" not in text and "{'" not in text and "['" not in text
    assert [line.rstrip() for line in text.splitlines()] == PLAIN.splitlines()


def test_rich_markup_in_a_value_is_printed_not_obeyed(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _colour({"note": "[bold]kept[/bold]", "items": ["[dim]a[/dim]", "b c"]}, monkeypatch)
    assert "[bold]kept[/bold]" in text
    assert "[dim]a[/dim]" in text


def test_a_run_of_dates_wraps_at_a_fixed_width() -> None:
    """Hundreds of stale-price days one to a line would bury the report.

    The width is fixed, never read from the terminal, so the bytes are the
    same wherever the report is rendered (`CLAUDE.md` invariant 6).
    """
    days = [(date(2024, 1, 1) + timedelta(days=n)).isoformat() for n in range(40)]
    lines = _plain({"stale_price_days": days, "large_flow_days": days[:2]}).splitlines()

    assert lines[0] == "large_flow_days   2024-01-01, 2024-01-02"
    assert lines[1] == "stale_price_days"
    body = lines[2:]
    assert all(line.startswith("  ") and len(line) <= 2 + 76 for line in body)
    assert ", ".join(line.strip().rstrip(",") for line in body).split(", ") == days


def test_an_item_with_a_space_or_comma_is_never_run_together() -> None:
    """Joined with commas, "a, b" could not be told from two items."""
    text = _plain({"items": ["a, b", "c"]})
    assert text.splitlines()[:3] == ["items", "  - a, b", "  - c"]


@pytest.mark.gips
@pytest.mark.parametrize("fmt", [OutputFormat.TABLE, OutputFormat.MARKDOWN])
def test_a_return_in_data_is_labelled_and_never_annualized_under_a_year(
    fmt: OutputFormat,
) -> None:
    """PORT-GIPS-B07, PORT-GIPS-H04: a return in `data` obeys the column's rules.

    It went through `machine`, which printed a bare 34-digit fraction and
    skipped the annualization guard.
    """
    start = date(2025, 1, 1)

    def value(*, days: int, annualized: bool) -> ReturnValue:
        return ReturnValue(
            value=D("0.05"),
            method="twr",
            basis="net_of_fees",
            period_start=start,
            period_end=start + timedelta(days=days),
            is_annualized=annualized,
        )

    text = render(
        _result({"returns": [value(days=90, annualized=False)]}), fmt, stream=io.StringIO()
    )
    assert "5.00% (TWR, net-of-fees)" in text
    assert "0.05" not in text

    annualized = _result({"returns": [value(days=90, annualized=True)]})
    with pytest.raises(GipsRefusalError) as exc:
        render(annualized, fmt, stream=io.StringIO())
    assert exc.value.context["requirement"] == "PORT-GIPS-B07"


def test_an_error_lists_its_problems_one_to_a_line(capsys: pytest.CaptureFixture[str]) -> None:
    """`pt validate` printed `problems: ['replay: ...']` to stderr."""
    error = ValidationError(
        "2 invariant problem(s) found",
        code=E_REPLAY_MISMATCH,
        problems=["replay: position_leg differs", "cash: 1.00 short"],
        warnings=[],
    )
    render_error(error, OutputFormat.TABLE)
    err = capsys.readouterr().err
    assert "['" not in err
    assert err.splitlines()[1:] == [
        "  problems",
        "    - replay: position_leg differs",
        "    - cash: 1.00 short",
        "  warnings  (none)",
    ]
