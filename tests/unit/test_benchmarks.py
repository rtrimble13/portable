"""The benchmark returns are set beside: cash at 0% until one is assigned (ADR 0030)."""

from __future__ import annotations

from datetime import date

import pytest

from portable_core.domain.enums import BenchmarkReturnType
from portable_core.domain.models import Benchmark
from portable_core.errors import GipsRefusalError, ValidationError
from portable_core.persistence.repositories import Repositories
from portable_core.services.benchmarks import CASH_ZERO, history, in_effect

pytestmark = pytest.mark.unit


def _benchmark(repos: Repositories, name: str, return_type: str = "total_return") -> int:
    return repos.benchmarks.add(
        Benchmark(
            benchmark_id=0,
            name=name,
            description=f"{name}, total return",
            return_type=BenchmarkReturnType(return_type),
        )
    )


@pytest.mark.gips
def test_port_gips_g02_no_benchmark_assigned_is_cash_at_zero_labelled_as_such(
    repos: Repositories,
) -> None:
    """PORT-GIPS-G02: never a silent omission. The owner's default, and it says so."""
    current = in_effect(repos, date(2024, 6, 30))

    assert current.benchmark is CASH_ZERO
    assert current.is_default
    assert current.benchmark.name == "Cash (0%)"
    assert "zero in every period" in current.benchmark.description
    assert "Not a Treasury-bill" in current.benchmark.description
    assert "No benchmark has been assigned" in current.disclosure


@pytest.mark.gips
def test_port_gips_g04_leaving_the_default_is_a_change_disclosed_with_date_and_reason(
    repos: Repositories,
) -> None:
    """PORT-GIPS-G04: the default is a benchmark too, so leaving it is a change."""
    index = _benchmark(repos, "Blend 60/40 TR")
    repos.benchmarks.assign(
        index, date(2025, 1, 1), change_type="prospective", change_reason="IPS adopted"
    )

    assert in_effect(repos, date(2024, 12, 31)).benchmark is CASH_ZERO
    assert in_effect(repos, date(2025, 1, 1)).benchmark.name == "Blend 60/40 TR"
    assert [a.disclosure for a in history(repos)][1] == (
        "Benchmark changed on 2025-01-01 from Cash (0%) to Blend 60/40 TR "
        "(prospective): IPS adopted"
    )


@pytest.mark.gips
def test_port_gips_g01_a_price_only_benchmark_is_refused_not_warned_about(
    repos: Repositories,
) -> None:
    """PORT-GIPS-G01, at the one place a benchmark is resolved."""
    index = _benchmark(repos, "S&P 500 price", return_type="price_only")
    repos.benchmarks.assign(
        index, date(2025, 1, 1), change_type="prospective", change_reason="mistake"
    )

    with pytest.raises(GipsRefusalError) as refused:
        in_effect(repos, date(2025, 6, 30))
    assert refused.value.code == "PT-E-GIPS-PRICE-ONLY-BENCHMARK"


def test_two_benchmarks_from_one_date_is_ambiguous(repos: Repositories) -> None:
    for name in ("A TR", "B TR"):
        repos.benchmarks.assign(
            _benchmark(repos, name),
            date(2025, 1, 1),
            change_type="prospective",
            change_reason="x",
        )

    with pytest.raises(ValidationError, match="two benchmarks are assigned from 2025-01-01"):
        history(repos)
