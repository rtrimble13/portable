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
from portable_core.providers.fafnir import DUK_COMMANDS, FafnirProvider, resolve_dsn

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
def test_fafnir_declares_no_benchmark_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR 0006, and the reason it matters.

    The warehouse carries security prices, not index levels. PORT-GIPS-G01
    requires refusal rather than approximation -- so rather than implementing
    `benchmark_series` and returning something, this provider does not claim
    the capability at all. Synthesising a total return by adding fafnir's
    dividends to a price series would be a defensible-looking wrong number.
    """
    monkeypatch.setattr("portable_core.providers.fafnir.shutil.which", lambda _: "/bin/duk")
    provider = FafnirProvider(dsn="host=nowhere")
    assert Capability.BENCHMARKS not in provider.capabilities
    assert Capability.FX not in provider.capabilities

    with pytest.raises(DataUnavailableError) as exc:
        provider.require(Capability.BENCHMARKS)
    assert exc.value.code == "PT-E-PROVIDER-CAPABILITY"
    assert "eod_prices" in (exc.value.remedy or "")


def test_fafnir_asks_for_raw_prices_and_never_adjusted_ones() -> None:
    """PORT-GIPS-A01. The adjusted path is unwritten, not merely discouraged.

    Adjusted prices are not fair values on the measurement date and would
    double-count splits. Asserted against the source text, because the point is
    that the `duk` argument that would do it does not exist.
    """
    text = _adapter_source()
    assert '"--adj"' not in text, "duk must never be passed --adj"


def test_fafnir_is_read_through_duk_alone() -> None:
    """ADR 0032: no database driver, no SQL, no relation name to query.

    Every read is a `duk` command, so a fafnir change reaches portable only
    through duk's own contract, which fafnir tests
    (test/duk/test_db_portable_reads_integration.py there).
    """
    import ast

    tree = ast.parse(_adapter_source())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (
            node.names if isinstance(node, ast.Import) else [ast.alias(node.module or "")]
        )
    }
    assert not imported & {"psycopg", "psycopg2", "sqlite3", "sqlalchemy"}, imported
    assert set(DUK_COMMANDS) == {"th", "ph", "ca", "tc", "yc"}


def _adapter_source() -> str:
    source = Path(FafnirProvider.__module__.replace(".", "/") + ".py")
    return (Path("src") / source).read_text(encoding="utf-8")


def test_the_warehouse_override_is_portables_own_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bootstrap §6.3, ADR 0032. portable reads no fafnir configuration: duk does.

    An explicit DSN (``fafnir_dsn`` in portable's config) or PORTABLE_FAFNIR_DSN
    is handed to duk as FAFNIR_DSN; without either, duk's own FAFNIR_DSN and
    ~/.dukrc are duk's business, and portable does not open a file under home.
    """
    monkeypatch.setenv("PORTABLE_FAFNIR_DSN", "host=portable")
    monkeypatch.setenv("FAFNIR_DSN", "host=fafnir")
    assert resolve_dsn() == "host=portable"
    assert resolve_dsn("host=explicit") == "host=explicit"

    monkeypatch.delenv("PORTABLE_FAFNIR_DSN")
    assert resolve_dsn() is None, "FAFNIR_DSN is read by duk, not by portable"

    def no_home() -> Path:
        raise AssertionError("portable must not read a fafnir config file")

    monkeypatch.setattr(Path, "home", no_home)
    assert resolve_dsn() is None


def test_a_missing_dsn_is_duks_clear_error_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no warehouse configured anywhere, duk says so, and the run stops."""
    import subprocess

    said = (
        "Error: no fafnir DSN configured. Set FAFNIR_DSN or [database].dsn in "
        "~/.dukrc, or read a vendor directly with -S fmp / -S sharadar."
    )
    monkeypatch.delenv("PORTABLE_FAFNIR_DSN", raising=False)
    monkeypatch.setattr("portable_core.providers.fafnir.shutil.which", lambda _: "/bin/duk")
    monkeypatch.setattr(
        "portable_core.providers.fafnir.subprocess.run",
        lambda argv, **_: subprocess.CompletedProcess(argv, 1, stdout="", stderr=said),
    )

    with pytest.raises(DataUnavailableError) as exc:
        FafnirProvider().lookup_security("AAPL", on=date(2025, 6, 30))
    assert exc.value.code == "PT-E-PROVIDER-UNAVAILABLE"
    assert "~/.dukrc" in exc.value.message


def test_build_provider_mirrors_duks_source_ergonomics(price_file: Path) -> None:
    assert isinstance(build_provider("file", price_file=price_file), FileProvider)
    assert isinstance(build_provider("null"), NullProvider)
    assert isinstance(build_provider("fafnir", fafnir_dsn="host=x"), FafnirProvider)

    with pytest.raises(UsageError) as exc:
        build_provider("bloomberg")
    assert exc.value.exit_code == 2
    assert "file, fafnir, null" in (exc.value.remedy or "")


def test_importing_the_providers_package_does_not_import_the_fafnir_adapter() -> None:
    """The adapter is imported only by somebody who asked for fafnir."""
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


def test_prefetch_is_a_hint_that_changes_no_answer(price_file: Path) -> None:
    """ADR 0032 §2. A provider that cannot read ahead ignores it; offline never fetches."""
    from portable_core.providers import OfflineProvider

    provider = FileProvider(price_file=price_file)
    before = provider.eod_prices("AAPL", date(2025, 6, 27), date(2025, 6, 30))
    provider.prefetch(["AAPL", "MSFT"])
    assert provider.eod_prices("AAPL", date(2025, 6, 27), date(2025, 6, 30)) == before

    NullProvider().prefetch(["AAPL"])
    OfflineProvider("fafnir").prefetch(["AAPL"])  # inherits the no-op: nothing is read
