"""The benchmark a portfolio's returns are set beside, and its history (ADR 0030).

``PORT-GIPS-G02`` allows no silent omission: every return either has a
benchmark beside it or says why it has none. The owner decided the default:
**cash at 0%**, for every date no benchmark has been assigned to. It is
labelled as exactly that wherever it appears -- a return of zero in every
period, not a Treasury-bill or money-market series -- so that nobody reads a
column of zeros as a market having gone nowhere.

An assignment is a ``portfolio_benchmark`` row, effective-dated. Moving from
one benchmark to the next, the default included, is a benchmark change, and
``PORT-GIPS-G04`` requires its date and description to be disclosed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

from portable_core.domain.enums import BenchmarkReturnType
from portable_core.domain.models import Benchmark
from portable_core.errors import GipsRefusalError, ValidationError
from portable_core.errors.kinds import E_GIPS_PRICE_ONLY_BENCHMARK
from portable_core.persistence.repositories import Repositories

__all__ = ["CASH_ZERO", "Assignment", "history", "in_effect"]

#: The default. Not stored: it is what an unassigned date means, so a
#: portfolio created before this decision has it too. A return of zero is a
#: total return, trivially, so ``PORT-GIPS-G01`` has nothing to refuse.
CASH_ZERO: Final = Benchmark(
    benchmark_id=0,
    name="Cash (0%)",
    description=(
        "Cash earning nothing: a return of exactly zero in every period. Not a "
        "Treasury-bill or money-market series."
    ),
    return_type=BenchmarkReturnType.TOTAL_RETURN,
    periodicity="daily",
    source="portable:default",
)

_DEFAULT_REASON: Final = (
    "No benchmark has been assigned to this portfolio, so its returns are set beside "
    "cash at 0%, the default (ADR 0030). A return above it is the portfolio's own "
    "return, not a return over a market."
)


@dataclass(frozen=True, slots=True)
class Assignment:
    """One benchmark, from one date until the next assignment."""

    #: ``None`` for the default: it applies from the first day.
    effective_from: date | None
    benchmark: Benchmark
    #: The disclosure: why this benchmark, or what changed and why.
    disclosure: str
    change_type: str | None = None

    @property
    def is_default(self) -> bool:
        return self.effective_from is None


def history(repos: Repositories) -> list[Assignment]:
    """The default, then every assignment in date order, each disclosing its change.

    Raises:
        ValidationError: two assignments take effect on the same date, so the
            benchmark on that date is ambiguous.
        GipsRefusalError: an assigned benchmark is price-only (``PORT-GIPS-G01``).
    """
    found = [Assignment(None, CASH_ZERO, _DEFAULT_REASON)]
    for effective_from, benchmark, change_type, reason in repos.benchmarks.assignments():
        if benchmark.return_type is BenchmarkReturnType.PRICE_ONLY:
            raise GipsRefusalError(
                f"{benchmark.name}, assigned from {effective_from.isoformat()}, is a "
                "price-only series",
                requirement="PORT-GIPS-G01",
                code=E_GIPS_PRICE_ONLY_BENCHMARK,
                remedy=(
                    "A price-only index understates its benchmark by roughly the "
                    "dividend yield every year. Assign its total-return series."
                ),
                benchmark=benchmark.name,
            )
        previous = found[-1]
        if previous.effective_from == effective_from:
            raise ValidationError(
                f"two benchmarks are assigned from {effective_from.isoformat()}: "
                f"{previous.benchmark.name} and {benchmark.name}",
                remedy="One date takes one benchmark. Reverse the assignment that is wrong.",
                effective_from=effective_from.isoformat(),
            )
        found.append(
            Assignment(
                effective_from,
                benchmark,
                f"Benchmark changed on {effective_from.isoformat()} from "
                f"{previous.benchmark.name} to {benchmark.name} ({change_type}): {reason}",
                change_type,
            )
        )
    return found


def in_effect(repos: Repositories, on: date) -> Assignment:
    """The benchmark *on* falls under: the latest assignment on or before it."""
    current, *later = history(repos)
    for assignment in later:
        assert assignment.effective_from is not None
        if assignment.effective_from <= on:
            current = assignment
    return current
