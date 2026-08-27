"""Output formatting, and the two return rules that live here by design.

GIPS acceptance tests:
    test_subyear_return_never_annualized   (PORT-GIPS-B07)
    test_every_return_carries_method_basis_and_period  (PORT-GIPS-H04)
"""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from portable_core.disclaimer import GIPS_DISCLAIMER
from portable_core.errors import GipsRefusalError
from portable_core.formatters import (
    Column,
    ColumnKind,
    CommandResult,
    OutputFormat,
    ReturnValue,
    Table,
    content_hash,
    render,
)
from portable_core.formatters.numbers import machine, money, quantity, rate

pytestmark = pytest.mark.unit

D = Decimal
STAMP = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def holdings_result(*, disclaimer: str | None = None) -> CommandResult:
    return CommandResult(
        command="holdings",
        table=Table(
            columns=(
                Column("symbol", "Symbol", ColumnKind.TEXT),
                Column("quantity", "Qty", ColumnKind.QUANTITY),
                Column("price", "Price", ColumnKind.PRICE),
                Column("value", "Market Value", ColumnKind.MONEY),
                Column("weight", "Weight", ColumnKind.RATE),
                Column("cost", "Cost", ColumnKind.MONEY),
            ),
            rows=(
                {
                    "symbol": "AAPL",
                    "quantity": D("100.000000"),
                    "price": D("190.50"),
                    "value": D("19050.00"),
                    "weight": D("0.6350"),
                    "cost": D("18500.00"),
                },
                {
                    "symbol": "CASH",
                    "quantity": None,
                    "price": None,
                    "value": D("10950.00"),
                    "weight": D("0.3650"),
                    "cost": None,
                },
            ),
            title="Holdings as of 2026-06-30",
        ),
        as_of=date(2026, 6, 30),
        portfolio="Demo",
        disclaimer=disclaimer,
    )


def _render(result: CommandResult, fmt: OutputFormat) -> str:
    return render(result, fmt, stream=io.StringIO(), generated_at=STAMP)


# ── json ─────────────────────────────────────────────────────────────────────


def test_json_serialises_decimal_as_a_string_never_a_float() -> None:
    """A consumer parsing a JSON number as a float loses the guarantee.

    `json.dumps(Decimal("0.1"))` cannot be made to round-trip through a JSON
    number, so every Decimal leaves as a string and the raw text is checked
    for a bare numeric literal.
    """
    text = _render(holdings_result(), OutputFormat.JSON)
    payload = json.loads(text)
    row = payload["data"]["rows"][0]

    assert row["value"] == "19050.00"
    assert isinstance(row["value"], str)
    assert '"value": 19050' not in text


def test_json_preserves_full_stored_precision() -> None:
    """No rounding in a machine format. Trailing zeros carry significance."""
    payload = json.loads(_render(holdings_result(), OutputFormat.JSON))
    assert payload["data"]["rows"][0]["quantity"] == "100.000000"
    assert payload["data"]["rows"][0]["weight"] == "0.6350"


def test_json_null_is_explicit_and_is_not_zero() -> None:
    """CLAUDE.md: never let blank and zero mean the same thing."""
    payload = json.loads(_render(holdings_result(), OutputFormat.JSON))
    cash = payload["data"]["rows"][1]
    assert cash["quantity"] is None
    assert cash["cost"] is None
    assert cash["value"] == "10950.00"


def test_the_envelope_carries_the_documented_fields() -> None:
    payload = json.loads(_render(holdings_result(), OutputFormat.JSON))
    assert set(payload) >= {
        "schema_version",
        "command",
        "generated_at",
        "as_of",
        "portfolio",
        "data",
        "warnings",
        "disclaimer",
    }
    assert payload["as_of"] == "2026-06-30"
    assert payload["command"] == "holdings"


def test_the_disclaimer_is_a_field_not_a_rendered_string() -> None:
    """A consumer who drops it has to drop a named key.

    Losing a footnote off the end of a formatted block is invisible; deleting
    `payload["disclaimer"]` is not.
    """
    payload = json.loads(
        _render(holdings_result(disclaimer=GIPS_DISCLAIMER), OutputFormat.JSON)
    )
    assert payload["disclaimer"] == GIPS_DISCLAIMER
    assert GIPS_DISCLAIMER not in json.dumps(payload["data"])


def test_a_command_with_no_disclaimer_says_so_explicitly() -> None:
    """Absent and null must not look the same to a consumer."""
    payload = json.loads(_render(holdings_result(), OutputFormat.JSON))
    assert "disclaimer" in payload
    assert payload["disclaimer"] is None


def test_json_output_is_byte_identical_across_runs() -> None:
    """CLAUDE.md invariant 6, at the output boundary."""
    outputs = {_render(holdings_result(), OutputFormat.JSON) for _ in range(20)}
    assert len(outputs) == 1


def test_the_content_hash_ignores_the_clock_and_the_version() -> None:
    """PORT-GIPS-J01's error detection depends on this.

    Two runs of the same report must hash the same, or comparing hashes tells
    you only that time passed.
    """
    from portable_core.formatters.envelope import build_envelope

    early = build_envelope(holdings_result(), generated_at=STAMP)
    later = build_envelope(holdings_result(), generated_at=datetime(2027, 1, 1, tzinfo=UTC))
    assert early["generated_at"] != later["generated_at"]
    assert content_hash(early) == content_hash(later)


def test_the_content_hash_changes_when_a_number_changes() -> None:
    """A hash that never changed would prove nothing."""
    from portable_core.formatters.envelope import build_envelope

    baseline = build_envelope(holdings_result(), generated_at=STAMP)
    changed_result = holdings_result()
    assert changed_result.table is not None
    changed = build_envelope(
        CommandResult(
            command="holdings",
            table=Table(
                columns=changed_result.table.columns,
                rows=({**changed_result.table.rows[0], "value": D("19050.01")},),
            ),
        ),
        generated_at=STAMP,
    )
    assert content_hash(baseline) != content_hash(changed)


# ── csv ──────────────────────────────────────────────────────────────────────


def test_csv_is_rfc4180_with_a_header_and_no_formatting() -> None:
    text = _render(holdings_result(), OutputFormat.CSV)
    lines = text.split("\r\n")
    assert lines[0] == "Symbol,Qty,Price,Market Value,Weight,Cost"
    assert lines[1] == "AAPL,100.000000,190.50,19050.00,0.6350,18500.00"
    assert "," in text and "$" not in text


def test_csv_renders_null_as_empty_and_never_as_zero() -> None:
    """A spreadsheet sums an empty cell as nothing and a zero as a zero."""
    lines = _render(holdings_result(), OutputFormat.CSV).split("\r\n")
    assert lines[2] == "CASH,,,10950.00,0.3650,"


# ── markdown ─────────────────────────────────────────────────────────────────


def test_markdown_aligns_numeric_columns_right() -> None:
    text = _render(holdings_result(), OutputFormat.MARKDOWN)
    assert "| Symbol | Qty | Price | Market Value | Weight | Cost |" in text
    assert "|:---|---:|---:|---:|---:|---:|" in text
    assert "| AAPL | 100 | 190.50 | 19,050.00 | 63.50% | 18,500.00 |" in text


def test_markdown_renders_null_as_an_em_dash() -> None:
    text = _render(holdings_result(), OutputFormat.MARKDOWN)
    assert "| CASH | — | — | 10,950.00 | 36.50% | — |" in text


def test_the_wrapped_disclaimer_survives_the_lint_rule_that_protects_it() -> None:
    """The rendered form must still match the allow-listed canonical text.

    A wrap that breaks a hyphenated word defeats the match and makes the
    compliance-language rule fire on a correct report -- invisible until it
    happens. See portable_core.disclaimer.WRAP_KWARGS.
    """
    from portable_core.lint.gips_language import _DISCLAIMER_RE

    text = _render(holdings_result(disclaimer=GIPS_DISCLAIMER), OutputFormat.MARKDOWN)
    assert "> Returns are calculated" in text
    assert _DISCLAIMER_RE.search(text) is not None


# ── table ────────────────────────────────────────────────────────────────────


def test_the_table_format_degrades_to_plain_text_off_a_tty() -> None:
    """A pipe or a CI log gets plain text without anybody passing a flag."""
    text = _render(holdings_result(), OutputFormat.TABLE)
    assert "\x1b[" not in text, "no ANSI escapes when the stream is not a terminal"
    assert "AAPL" in text and "19,050.00" in text


def test_no_color_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    from portable_core.formatters import supports_color

    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    stream = Tty()
    assert supports_color(stream) is True
    assert supports_color(stream, no_color=True) is False
    monkeypatch.setenv("NO_COLOR", "1")
    assert supports_color(stream) is False


def test_an_empty_table_says_so_rather_than_printing_nothing() -> None:
    result = CommandResult(
        command="holdings",
        table=Table(columns=(Column("symbol", "Symbol"),), rows=()),
    )
    assert "(no rows)" in _render(result, OutputFormat.TABLE)


# ── the two return rules ─────────────────────────────────────────────────────


@pytest.mark.gips
@pytest.mark.parametrize(
    ("days", "annualized", "allowed"),
    [
        (1, True, False),
        (30, True, False),
        (364, True, False),
        (365, True, True),
        (366, True, True),
        (1, False, True),  # not annualized: fine at any length
        (364, False, True),
    ],
)
def test_subyear_return_never_annualized(days: int, annualized: bool, allowed: bool) -> None:
    """PORT-GIPS-B07 -- unconditional, and enforced in the formatter.

    Firms 2.A.12 / Asset Owners 22.A.9. It binds the since-inception
    money-weighted return too, which is the one place the natural
    implementation returns an annualized rate by construction
    (PORT-GIPS-C03).

    The check lives here because anywhere else, a call site would eventually
    be added that does not call it.
    """
    from datetime import timedelta

    start = date(2025, 1, 1)
    value = ReturnValue(
        value=D("0.05"),
        method="twr",
        basis="net_of_fees",
        period_start=start,
        period_end=start + timedelta(days=days),
        is_annualized=annualized,
    )

    if allowed:
        assert "%" in render_return_safe(value)
    else:
        with pytest.raises(GipsRefusalError) as exc:
            render_return_safe(value)
        assert exc.value.code == "PT-E-GIPS-ANNUALIZE-SUB-YEAR"
        assert exc.value.context["requirement"] == "PORT-GIPS-B07"
        assert exc.value.exit_code == 4


def render_return_safe(value: ReturnValue) -> str:
    from portable_core.formatters import render_return

    return render_return(value)


@pytest.mark.gips
def test_every_return_carries_method_basis_and_period() -> None:
    """PORT-GIPS-H04 -- there is no way to render a bare number.

    A return without its basis is not interpretable, and a reader given one
    will assume whichever basis flatters the manager.
    """
    value = ReturnValue(
        value=D("0.0842"),
        method="twr",
        basis="net_of_fees",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
    )
    rendered = render_return_safe(value)
    assert "8.42%" in rendered
    assert "TWR" in rendered
    assert "net-of-fees" in rendered

    payload = machine(value)
    assert isinstance(payload, dict)
    assert set(payload) >= {
        "value",
        "method",
        "basis",
        "period_start",
        "period_end",
        "is_annualized",
    }
    assert payload["value"] == "0.0842"


@pytest.mark.gips
def test_supplemental_returns_are_labelled_as_such() -> None:
    """PORT-GIPS-H08 -- after-tax, model, and backtested results, always."""
    value = ReturnValue(
        value=D("0.06"),
        method="after_tax_twr",
        basis="net_of_fees",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        is_supplemental=True,
    )
    assert "SUPPLEMENTAL" in render_return_safe(value)


# ── number rules ─────────────────────────────────────────────────────────────


def test_negatives_use_a_leading_minus_consistently() -> None:
    """One convention, chosen and applied. A lone ')' at a line break is worse."""
    assert money(D("-1234.5")) == "-1,234.50"
    assert rate(D("-0.05")) == "-5.00%"


def test_small_returns_render_in_basis_points() -> None:
    """ "0.03%" and "3 bps" are the same number; only one reads in a column."""
    assert rate(D("0.0003")) == "3.0 bps"
    assert rate(D("0.05")) == "5.00%"


def test_a_whole_share_count_is_not_padded_to_six_places() -> None:
    """Padding reads as a precision claim nobody made."""
    assert quantity(D("100.000000")) == "100"
    assert quantity(D("100.5")) == "100.5"
    assert quantity(D("1234567")) == "1,234,567"


def test_a_sub_penny_price_does_not_floor_to_zero() -> None:
    """A back-adjusted series and a deep OTM option genuinely trade there."""
    from portable_core.formatters import price

    assert price(D("0.0003123")) == "0.0003123"
    assert price(D("190.5")) == "190.50"
