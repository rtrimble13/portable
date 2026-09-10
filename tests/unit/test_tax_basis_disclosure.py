"""Disclosing where a reported gain's basis came from. ADR 0017 §3 and §2b.

Two obligations, and they are different:

**Mark what is approximate.** A report in which every basis is `portable`'s own
arithmetic and one in which a sixth of it was reconstructed must not look
identical, because the reader's next action differs. The provenance travels
with the figure.

**Do not print a gain that is not one.** A lot with `basis_source =
'unavailable'` was seeded at cutover market value so cash conservation would
close and the position engine had a lot to relieve. The difference between that
seed and the proceeds is the change since an arbitrary date wearing the units of
a gain. Summing it into a tax figure is the silently-wrong-number failure with
a plausible magnitude and the right units — so those dispositions are excluded
from every total, listed separately with proceeds only, and the year is marked
incomplete.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portable_core.domain.enums import BasisSource, HoldingPeriod
from portable_core.domain.models import RealizedGain
from portable_core.services.tax import TaxEngine

pytestmark = pytest.mark.unit

YEAR = 2025


def _gain(
    source: BasisSource,
    *,
    gain: str = "1000.00",
    basis: str = "4000.00",
    proceeds: str = "5000.00",
    tax: str | None = "150.00",
    disposition_id: int = 1,
    period: HoldingPeriod = HoldingPeriod.LONG,
) -> RealizedGain:
    return RealizedGain(
        disposition_id=disposition_id,
        account_id=1,
        instrument_id=1,
        tax_year=YEAR,
        disposition_date=date(YEAR, 6, 1),
        holding_period=period,
        proceeds=Decimal(proceeds),
        cost_basis=Decimal(basis),
        gain=Decimal(gain),
        is_taxable=True,
        estimated_tax=Decimal(tax) if tax is not None else None,
        basis_source=source,
    )


# ── exclusion ────────────────────────────────────────────────────────────────


def test_an_unavailable_disposition_is_in_no_total() -> None:
    """The heart of it. Its stored gain is arithmetically real and is not a gain."""
    summary = TaxEngine().summarise(
        [
            _gain(BasisSource.DERIVED, gain="300.00", basis="700.00", disposition_id=1),
            _gain(
                BasisSource.UNAVAILABLE,
                gain="9999.00",
                basis="1.00",
                proceeds="10000.00",
                tax="1500.00",
                disposition_id=2,
            ),
        ],
        YEAR,
    )
    assert summary.total_gain == Decimal("300.00")
    assert summary.total_tax == Decimal("150.00")
    assert summary.cost_basis == Decimal("700.00")
    assert summary.proceeds == Decimal("5000.00")
    assert summary.disposition_count == 1


def test_the_excluded_disposition_is_reported_with_proceeds_only() -> None:
    """Proceeds are exact — they come from the sale. Basis and gain are absent
    rather than zero, because a zero would be read as a figure."""
    summary = TaxEngine().summarise(
        [_gain(BasisSource.UNAVAILABLE, proceeds="10000.00", disposition_id=7)], YEAR
    )
    (item,) = summary.unreportable
    assert item.disposition_id == 7
    assert item.proceeds == Decimal("10000.00")
    assert not hasattr(item, "gain")
    assert not hasattr(item, "cost_basis")


def test_the_year_is_marked_incomplete() -> None:
    """The treatment `valuation_snapshot.is_complete` already gives a snapshot
    built from a position that could not be priced."""
    engine = TaxEngine()
    assert engine.summarise([_gain(BasisSource.DERIVED)], YEAR).is_complete is True
    assert engine.summarise([_gain(BasisSource.UNAVAILABLE)], YEAR).is_complete is False


def test_an_excluded_disposition_does_not_reduce_a_loss_either() -> None:
    """Netting works both ways, and so does the error.

    An excluded disposition carrying a stored loss would flatter the totals just
    as one carrying a stored gain inflates them.
    """
    summary = TaxEngine().summarise(
        [
            _gain(BasisSource.DERIVED, gain="-500.00", tax="0.00", disposition_id=1),
            _gain(BasisSource.UNAVAILABLE, gain="-4000.00", disposition_id=2),
        ],
        YEAR,
    )
    assert summary.total_gain == Decimal("-500.00")


# ── marking what is approximate ──────────────────────────────────────────────


def test_every_rung_present_is_reported_with_what_rests_on_it() -> None:
    summary = TaxEngine().summarise(
        [
            _gain(BasisSource.DERIVED, gain="100.00", basis="900.00", disposition_id=1),
            _gain(BasisSource.RECONSTRUCTED, gain="200.00", basis="800.00", disposition_id=2),
            _gain(BasisSource.ESTIMATED, gain="300.00", basis="700.00", disposition_id=3),
        ],
        YEAR,
    )
    by_source = {p.basis_source: p for p in summary.basis_provenance}
    assert set(by_source) == {
        BasisSource.DERIVED,
        BasisSource.RECONSTRUCTED,
        BasisSource.ESTIMATED,
    }
    assert by_source[BasisSource.RECONSTRUCTED].cost_basis == Decimal("800.00")
    assert by_source[BasisSource.RECONSTRUCTED].gain == Decimal("200.00")
    assert by_source[BasisSource.DERIVED].is_exact
    assert not by_source[BasisSource.ESTIMATED].is_exact


def test_the_rungs_are_in_ladder_order_not_by_size() -> None:
    """So two adjacent years read side by side, and `derived` leads."""
    summary = TaxEngine().summarise(
        [
            _gain(BasisSource.ESTIMATED, basis="9000.00", disposition_id=1),
            _gain(BasisSource.DERIVED, basis="1.00", disposition_id=2),
        ],
        YEAR,
    )
    assert [p.basis_source for p in summary.basis_provenance] == [
        BasisSource.DERIVED,
        BasisSource.ESTIMATED,
    ]


def test_the_approximate_share_is_measured_on_basis() -> None:
    """Not on gain, deliberately: the basis is the approximate input, and a
    proportion of a signed total near zero misleads more than it informs."""
    summary = TaxEngine().summarise(
        [
            _gain(BasisSource.DERIVED, basis="750.00", disposition_id=1),
            _gain(BasisSource.RECONSTRUCTED, basis="250.00", disposition_id=2),
        ],
        YEAR,
    )
    assert summary.approximate_basis == Decimal("250.00")
    assert summary.approximate_basis_share == Decimal("0.25")


def test_a_custodian_asserted_basis_counts_as_approximate() -> None:
    """It is exact as a figure and it is not portable's own arithmetic.

    The reader is being told which numbers rest on somebody else's assertion,
    and a custodian's lot report is one.
    """
    summary = TaxEngine().summarise(
        [_gain(BasisSource.CUSTODIAN_ASSERTED, basis="1000.00")], YEAR
    )
    assert summary.approximate_basis == Decimal("1000.00")
    assert summary.approximate_basis_share == Decimal("1")


def test_an_all_derived_year_reports_a_zero_share_not_a_null() -> None:
    """Zero says every basis was exact. Null would say nothing was reported."""
    summary = TaxEngine().summarise([_gain(BasisSource.DERIVED)], YEAR)
    assert summary.approximate_basis_share == Decimal("0")
    assert summary.approximate_basis == Decimal("0.00")


def test_an_empty_year_reports_null_rather_than_zero() -> None:
    """The distinction `CLAUDE.md` insists on, one level up: blank and zero must
    never mean the same thing."""
    summary = TaxEngine().summarise([], YEAR)
    assert summary.approximate_basis_share is None
    assert summary.basis_provenance == ()
    assert summary.is_complete is True


def test_excluded_dispositions_are_not_in_the_provenance_breakdown() -> None:
    """The breakdown describes the reported totals. Putting the excluded rows in
    it would let them be summed back by a consumer adding up the rungs."""
    summary = TaxEngine().summarise(
        [
            _gain(BasisSource.DERIVED, basis="900.00", disposition_id=1),
            _gain(BasisSource.UNAVAILABLE, basis="9000.00", disposition_id=2),
        ],
        YEAR,
    )
    assert [p.basis_source for p in summary.basis_provenance] == [BasisSource.DERIVED]
    assert sum(p.cost_basis for p in summary.basis_provenance) == summary.cost_basis


def test_short_and_long_stay_apart_through_the_exclusion() -> None:
    """The exclusion must not disturb the split it sits inside."""
    summary = TaxEngine().summarise(
        [
            _gain(
                BasisSource.DERIVED,
                gain="100.00",
                tax="35.00",
                period=HoldingPeriod.SHORT,
                disposition_id=1,
            ),
            _gain(
                BasisSource.DERIVED,
                gain="200.00",
                tax="30.00",
                period=HoldingPeriod.LONG,
                disposition_id=2,
            ),
            _gain(
                BasisSource.UNAVAILABLE,
                gain="5000.00",
                period=HoldingPeriod.SHORT,
                disposition_id=3,
            ),
        ],
        YEAR,
    )
    assert summary.short_term_gain == Decimal("100.00")
    assert summary.long_term_gain == Decimal("200.00")
    assert summary.short_term_tax == Decimal("35.00")
    assert summary.long_term_tax == Decimal("30.00")


def test_the_wash_sale_statement_is_untouched_by_any_of_this() -> None:
    """Two separate incompleteness claims; neither may mask the other."""
    summary = TaxEngine().summarise([_gain(BasisSource.UNAVAILABLE)], YEAR)
    assert summary.excludes_wash_sales is True
    assert summary.disclaimer
