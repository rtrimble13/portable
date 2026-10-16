"""Loading a history of prices, through the real CLI (ADR 0020).

`pt price load` used to default to every *active* instrument in the security
master over the whole range -- loading what was never held, and treating a
position sold last year like one held today. It turned every failure,
including an unreachable warehouse, into a warning and exit 0. And `--offline`
was accepted and ignored (issue #39).
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from tests.integration.conftest import CliRunner

pytestmark = pytest.mark.integration

#: Refused at once: nothing listens on port 1. A connection attempt fails fast
#: with PROVIDER-UNAVAILABLE, which is how a test can tell one was made.
UNREACHABLE = "host=127.0.0.1 port=1 connect_timeout=2"


def _price_file(tmp_path: Path) -> Path:
    """Weekday closes for four tickers. AAPX is what AAPL is renamed to below,
    priced far apart from it so a row's origin shows in its value."""
    bases = {"AAPL": 150, "AAPX": 900, "MSFT": 400, "NVDA": 50}
    lines = ["symbol,date,price,source,valuation_level"]
    on = date(2023, 12, 18)
    while on <= date(2024, 4, 30):
        if on.weekday() < 5:
            for symbol, base in bases.items():
                lines.append(f"{symbol},{on.isoformat()},{base}.{on.day:02d},test,1")
        on += timedelta(days=1)
    path = tmp_path / "prices.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def book(
    run_pt: CliRunner, portfolio: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    """AAPL held Jan-Feb and sold; MSFT held from Feb; NVDA known but never held."""
    port = str(portfolio)
    monkeypatch.setenv("PORTABLE_PRICE_FILE", str(_price_file(tmp_path)))
    for symbol in ("AAPL", "MSFT", "NVDA"):
        run_pt("--port", port, "instrument", "add", symbol, "--type", "equity")
    for verb, symbol, qty, on in (
        ("buy", "AAPL", "100", "2024-01-03"),
        ("sell", "AAPL", "100", "2024-03-01"),
        ("buy", "MSFT", "10", "2024-02-01"),
    ):
        run_pt("--port", port, verb, symbol, "--qty", qty, "--price", "100.00", "--date", on,
               "-a", "B")  # fmt: skip
    return port


def _dates(run_pt: CliRunner, port: str, symbol: str) -> list[str]:
    shown = run_pt(
        "--port", port, "price", "show", symbol, "--from", "2023-12-01", "--to", "2024-04-30"
    ).data  # fmt: skip
    return [row["price_date"] for row in shown["rows"]]


def test_a_load_prices_each_instrument_held_for_the_days_it_was_held(
    run_pt: CliRunner, book: str
) -> None:
    """ADR 0020 R2: the sold position is priced for its months, nothing else is."""
    run_pt("--port", book, "-S", "file", "price", "load", "--from", "2024-01-01",
           "--to", "2024-04-30")  # fmt: skip

    aapl = _dates(run_pt, book, "AAPL")
    assert (aapl[0], aapl[-1]) == ("2023-12-29", "2024-02-29"), (
        "from a lead-in before the purchase to the day before the sale"
    )
    msft = _dates(run_pt, book, "MSFT")
    assert (msft[0], msft[-1]) == ("2024-01-29", "2024-04-30")
    assert _dates(run_pt, book, "NVDA") == [], "never held, so never asked for"


def test_a_renamed_instrument_is_loaded_under_each_ticker(run_pt: CliRunner, book: str) -> None:
    """Asked for under the ticker it carried each day, not today's across all of them."""
    run_pt(
        "--port", book, "ca", "symbol-change", "AAPL", "--to", "AAPX", "--date", "2024-02-15"
    )

    run_pt("--port", book, "-S", "file", "price", "load", "--from", "2024-02-12",
           "--to", "2024-02-16")  # fmt: skip

    shown = run_pt(
        "--port", book, "price", "show", "AAPX", "--from", "2024-02-12", "--to", "2024-02-16"
    ).data  # fmt: skip
    by_day = {row["price_date"]: row["price"] for row in shown["rows"]}
    assert by_day["2024-02-14"] == "150.14", "before the rename: AAPL's row"
    assert by_day["2024-02-15"] == "900.15", "from the rename: AAPX's row"


def test_a_symbol_with_no_prices_is_a_warning_and_nothing_at_all_is_a_failure(
    run_pt: CliRunner, book: str
) -> None:
    run_pt("--port", book, "instrument", "add", "ZZZ", "--type", "equity")

    partly = run_pt("--port", book, "-S", "file", "price", "load", "AAPL", "ZZZ",
                    "--from", "2024-01-02", "--to", "2024-01-05")  # fmt: skip
    assert any("ZZZ" in warning for warning in partly.json()["warnings"])

    nothing = run_pt("--port", book, "-S", "file", "price", "load", "ZZZ",
                     "--from", "2024-01-02", "--to", "2024-01-05", expect=5)  # fmt: skip
    assert nothing.json()["error"]["code"] == "PT-E-PRICE-MISSING"


def test_an_unreachable_warehouse_fails_rather_than_warning(
    run_pt: CliRunner, book: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It used to be one warning per symbol and exit 0: an outage that 'succeeded'."""
    monkeypatch.setenv("PORTABLE_FAFNIR_DSN", UNREACHABLE)

    failed = run_pt("--port", book, "-S", "fafnir", "price", "load", "AAPL", expect=5)

    assert failed.json()["error"]["code"] == "PT-E-PROVIDER-UNAVAILABLE"


def test_offline_refuses_a_fetch_before_reaching_the_provider(
    run_pt: CliRunner, book: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #39: `test_offline_refuses_provider_access_immediately`.

    The warehouse is unreachable, so an attempt would fail as an outage. The
    offline refusal is its own error -- the data is not cached, and the session
    declined to go and get it -- which shows no attempt was made.
    """
    monkeypatch.setenv("PORTABLE_FAFNIR_DSN", UNREACHABLE)

    for command in (("price", "load", "AAPL"), ("ca", "sync", "AAPL"), ("instrument", "sync")):
        refused = run_pt("--port", book, "-S", "fafnir", "--offline", *command, expect=5)
        assert refused.json()["error"]["code"] == "PT-E-OFFLINE-CACHE-MISS", command


def test_offline_values_from_the_cache(run_pt: CliRunner, book: str) -> None:
    """Issue #39: `test_offline_uses_cached_prices` -- and `pt info` says it is offline."""
    run_pt("--port", book, "price", "set", "MSFT", "--price", "410.00", "--date", "2024-04-30",
           "--valuation-level", "1")  # fmt: skip

    row = run_pt("--port", book, "--offline", "value", "--date", "2024-04-30").data["rows"][0]
    assert row["complete"] is True
    assert run_pt("--port", book, "--offline", "info").data["offline"] is True
    assert run_pt("--port", book, "info").data["offline"] is False


def test_every_provider_command_fails_on_an_unreachable_warehouse(
    run_pt: CliRunner, book: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ca sync` and `instrument sync` turned an outage into warnings and exit 0 too."""
    monkeypatch.setenv("PORTABLE_FAFNIR_DSN", UNREACHABLE)

    for command in (("ca", "sync"), ("instrument", "sync")):
        failed = run_pt("--port", book, "-S", "fafnir", *command, expect=5)
        assert failed.json()["error"]["code"] == "PT-E-PROVIDER-UNAVAILABLE", command
