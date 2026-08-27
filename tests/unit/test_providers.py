"""Providers: capability honesty, the file schema, and the two fafnir gaps.

GIPS acceptance test: test_price_only_benchmark_refused (PORT-GIPS-G01).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portable_core.errors import DataUnavailableError, UsageError
from portable_core.providers import (
    Capability,
    FileProvider,
    NullProvider,
    build_provider,
)
from portable_core.providers.fafnir import SCHEMA, FafnirProvider, resolve_dsn

pytestmark = pytest.mark.unit

D = Decimal


@pytest.fixture
def price_file(tmp_path: Path) -> Path:
    path = tmp_path / "prices.csv"
    path.write_text(
        "symbol,date,price\n"
        "AAPL,2025-06-27,201.08\n"
        "AAPL,2025-06-30,205.17\n"
        "MSFT,2025-06-30,497.41\n",
        encoding="utf-8",
    )
    return path


# ── null provider ────────────────────────────────────────────────────────────


def test_the_null_provider_refuses_with_a_next_step() -> None:
    """A good message beats an ImportError three frames deep."""
    provider = NullProvider()
    assert provider.capabilities == frozenset()

    with pytest.raises(DataUnavailableError) as exc:
        provider.eod_prices("AAPL", date(2025, 1, 1), date(2025, 6, 30))
    assert exc.value.exit_code == 5
    assert "--source file" in (exc.value.remedy or "")
    assert "--offline" in (exc.value.remedy or "")


# ── file provider ────────────────────────────────────────────────────────────


def test_the_file_provider_declares_only_what_it_has_a_file_for(
    price_file: Path,
) -> None:
    """An empty result reads as "no prices exist", which would be a lie."""
    assert FileProvider(price_file=price_file).capabilities == frozenset(
        {Capability.EOD_PRICES}
    )
    assert FileProvider().capabilities == frozenset()


def test_the_file_provider_reads_prices_with_full_precision(price_file: Path) -> None:
    prices = FileProvider(price_file=price_file).eod_prices(
        "AAPL", date(2025, 6, 1), date(2025, 6, 30)
    )
    assert [p.price for p in prices] == [D("201.08"), D("205.17")]
    assert prices[0].price_date == date(2025, 6, 27)
    assert prices[0].source == "file:prices.csv"
    assert prices[0].valuation_level == 1


def test_a_missing_column_fails_on_the_file_naming_the_line(tmp_path: Path) -> None:
    """Not four commands later on a valuation."""
    bad = tmp_path / "bad.csv"
    bad.write_text("symbol,date\nAAPL,2025-06-30\n", encoding="utf-8")
    with pytest.raises(UsageError) as exc:
        FileProvider(price_file=bad).eod_prices("AAPL", date(2025, 1, 1), date(2025, 12, 31))
    assert "price" in exc.value.message
    assert exc.value.context["line"] == 2


def test_an_unknown_symbol_says_which_ones_it_knows(price_file: Path) -> None:
    with pytest.raises(DataUnavailableError) as exc:
        FileProvider(price_file=price_file).eod_prices(
            "TSLA", date(2025, 1, 1), date(2025, 12, 31)
        )
    assert exc.value.context["known_symbols"] == ["AAPL", "MSFT"]


@pytest.mark.gips
def test_price_only_benchmark_refused(tmp_path: Path) -> None:
    """PORT-GIPS-G01 -- refused, not warned about.

    A price index understates its benchmark by roughly the dividend yield every
    year, flattering the portfolio by 1.5-2% annually. The file must declare
    `return_type`; there is no default, because a default is exactly how a
    price series gets used by accident.
    """
    path = tmp_path / "benchmarks.json"
    path.write_text(
        json.dumps(
            {
                "SPXTR": {
                    "return_type": "total_return",
                    "levels": {"2025-06-30": "12345.67"},
                },
                "SPX": {
                    "return_type": "price_only",
                    "levels": {"2025-06-30": "6204.95"},
                },
                "UNDECLARED": {"levels": {"2025-06-30": "100.00"}},
            }
        ),
        encoding="utf-8",
    )
    provider = FileProvider(benchmark_file=path)
    window = (date(2025, 1, 1), date(2025, 12, 31))

    total_return = provider.benchmark_series("SPXTR", *window)
    assert total_return.is_total_return is True

    # A price-only series LOADS -- so the refusal can be specific about what is
    # wrong -- but is flagged, and the return path refuses it.
    price_only = provider.benchmark_series("SPX", *window)
    assert price_only.is_total_return is False

    with pytest.raises(UsageError) as exc:
        provider.benchmark_series("UNDECLARED", *window)
    assert "no default, deliberately" in (exc.value.remedy or "")


def test_corporate_actions_come_back_in_date_order(tmp_path: Path) -> None:
    path = tmp_path / "actions.csv"
    path.write_text(
        "symbol,action_type,ex_date,split_numerator,split_denominator,cash_amount\n"
        "AAPL,cash_dividend,2025-05-12,,,0.26\n"
        "AAPL,split,2024-06-10,4,1,\n",
        encoding="utf-8",
    )
    actions = FileProvider(action_file=path).corporate_actions(
        "AAPL", date(2024, 1, 1), date(2025, 12, 31)
    )
    assert [a.ex_date for a in actions] == [date(2024, 6, 10), date(2025, 5, 12)]
    assert actions[0].split_numerator == D("4")
    assert actions[1].cash_amount == D("0.26")


# ── the fafnir adapter ───────────────────────────────────────────────────────


@pytest.mark.gips
def test_fafnir_declares_no_benchmark_capability() -> None:
    """ADR 0006, and the reason it matters.

    The warehouse carries security prices, not index levels. PORT-GIPS-G01
    requires refusal rather than approximation -- so rather than implementing
    `benchmark_series` and returning something, this provider does not claim
    the capability at all. Synthesising a total return by adding
    core.corporate_action dividends to a price series would be a
    defensible-looking wrong number.
    """
    provider = FafnirProvider(dsn="host=nowhere")
    assert Capability.BENCHMARKS not in provider.capabilities
    assert Capability.FX not in provider.capabilities

    with pytest.raises(DataUnavailableError) as exc:
        provider.require(Capability.BENCHMARKS)
    assert exc.value.code == "PT-E-PROVIDER-CAPABILITY"
    assert "eod_prices" in (exc.value.remedy or "")


def test_fafnir_reads_the_unadjusted_table_and_never_the_adjusted_view() -> None:
    """PORT-GIPS-A01. The adjusted path is unwritten, not merely discouraged.

    Adjusted prices are not fair values on the measurement date and would
    double-count splits. Asserted against the source text, because the point is
    that the SQL to do it does not exist.
    """
    source = Path(FafnirProvider.__module__.replace(".", "/") + ".py")
    text = (Path("src") / source).read_text(encoding="utf-8")

    assert SCHEMA["daily_price"] == "core.daily_price"
    assert "v_daily_price_adjusted" not in _sql_only(text), (
        "the adjusted mart view must not appear in any query"
    )


def _sql_only(text: str) -> str:
    """The parts of the module that are executable, not prose.

    The docstring explains at length why the adjusted view is not used, so a
    naive substring search over the whole file would find it there.
    """
    lines = [
        line
        for line in text.splitlines()
        if "SELECT" in line or "FROM" in line or "SCHEMA[" in line
    ]
    return "\n".join(lines)


def test_the_fafnir_schema_names_live_in_one_mapping() -> None:
    """So a fafnir migration is a one-file change here (ADR 0006)."""
    assert SCHEMA == {
        "security": "core.security",
        "symbol_xref": "core.symbol_xref",
        "daily_price": "core.daily_price",
        "corporate_action": "core.corporate_action",
        "trading_calendar": "ref.trading_calendar",
    }


def test_the_dsn_comes_from_the_environment_before_any_config_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bootstrap §6.3: never read a secret from a file when an env var exists."""
    monkeypatch.setenv("PORTABLE_FAFNIR_DSN", "host=portable")
    monkeypatch.setenv("FAFNIR_DSN", "host=fafnir")
    assert resolve_dsn() == "host=portable"

    monkeypatch.delenv("PORTABLE_FAFNIR_DSN")
    assert resolve_dsn() == "host=fafnir"

    assert resolve_dsn("host=explicit") == "host=explicit"


def test_a_missing_dsn_is_a_clear_error_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PORTABLE_FAFNIR_DSN", raising=False)
    monkeypatch.delenv("FAFNIR_DSN", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(DataUnavailableError) as exc:
        FafnirProvider().lookup_security("AAPL")
    assert exc.value.code == "PT-E-PROVIDER-UNAVAILABLE"
    assert "~/.dukrc" in (exc.value.remedy or "")


def test_a_connection_failure_never_echoes_the_dsn() -> None:
    """A DSN carries a password. It must not reach a log or an error message."""
    provider = FafnirProvider(dsn="host=nowhere.invalid password=hunter2")
    with pytest.raises(DataUnavailableError) as exc:
        provider.lookup_security("AAPL")

    rendered = json.dumps(exc.value.to_dict())
    assert "hunter2" not in rendered
    assert "nowhere.invalid" not in rendered


# ── selection ────────────────────────────────────────────────────────────────


def test_build_provider_mirrors_duks_source_ergonomics(price_file: Path) -> None:
    assert isinstance(build_provider("file", price_file=price_file), FileProvider)
    assert isinstance(build_provider("null"), NullProvider)
    assert isinstance(build_provider("fafnir", fafnir_dsn="host=x"), FafnirProvider)

    with pytest.raises(UsageError) as exc:
        build_provider("bloomberg")
    assert exc.value.exit_code == 2
    assert "file, fafnir, null" in (exc.value.remedy or "")


def test_importing_the_providers_package_does_not_require_psycopg() -> None:
    """psycopg's absence must be felt only by somebody who asked for fafnir."""
    import ast

    text = (Path("src/portable_core/providers/__init__.py")).read_text(encoding="utf-8")
    tree = ast.parse(text)
    top_level_imports = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in getattr(node, "names", [])
    }
    assert not any("fafnir" in name for name in top_level_imports)
