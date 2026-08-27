"""The tax engine: what it computes exactly, estimates, and refuses. ADR 0011."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portable_core.domain.enums import AccountType, HoldingPeriod, ReliefMethod
from portable_core.domain.models import Account, LotDisposition, TaxRateSchedule
from portable_core.errors import ValidationError
from portable_core.services.tax import TaxEngine

pytestmark = pytest.mark.unit

D = Decimal
ENGINE = TaxEngine()


def account(
    account_type: AccountType = AccountType.TAXABLE, name: str = "Brokerage"
) -> Account:
    return Account(
        account_id=1, name=name, account_type=account_type, opened_date=date(2020, 1, 1)
    )


def schedule(
    effective_from: date = date(2024, 1, 1),
    *,
    rate_id: int = 1,
    short: str = "0.37",
    long: str = "0.20",
    state: str = "0.05",
    niit: str = "0.038",
) -> TaxRateSchedule:
    return TaxRateSchedule(
        rate_id=rate_id,
        account_id=1,
        effective_from=effective_from,
        short_term_federal=D(short),
        long_term_federal=D(long),
        state=D(state),
        niit=D(niit),
    )


def disposition(
    *,
    gain: str = "5000.00",
    period: HoldingPeriod = HoldingPeriod.LONG,
    on: date = date(2025, 6, 30),
    disposition_id: int = 1,
) -> LotDisposition:
    return LotDisposition(
        disposition_id=disposition_id,
        lot_id=1,
        txn_id=1,
        account_id=1,
        instrument_id=1,
        disposition_date=on,
        quantity=D("100"),
        proceeds=D("15000.00"),
        cost_basis_relieved=D("15000.00") - D(gain),
        realized_gain=D(gain),
        holding_period=period,
        days_held=500,
        relief_method=ReliefMethod.FIFO,
    )


# ── effective-dated rates ────────────────────────────────────────────────────


def test_the_schedule_in_force_is_the_latest_one_not_after_the_date() -> None:
    """Effective-dating stops a rate change restating what a past sale cost."""
    schedules = [
        schedule(date(2022, 1, 1), rate_id=1, short="0.32"),
        schedule(date(2024, 1, 1), rate_id=2, short="0.37"),
        schedule(date(2026, 1, 1), rate_id=3, short="0.39"),
    ]
    for on, expected in [
        (date(2023, 6, 1), 1),
        (date(2024, 1, 1), 2),
        (date(2025, 12, 31), 2),
        (date(2026, 1, 1), 3),
    ]:
        assert ENGINE.rate_in_force(schedules, on, account_name="A").rate_id == expected


def test_a_missing_rate_schedule_is_an_error_not_a_zero() -> None:
    """The same rule PORT-GIPS-B03 applies to flow policy, for the same reason.

    A defaulted zero rate produces a plausible number that is wrong, and
    nothing about the output would say so.
    """
    with pytest.raises(ValidationError) as exc:
        ENGINE.rate_in_force(
            [schedule(date(2026, 1, 1))], date(2025, 1, 1), account_name="Brokerage"
        )
    assert exc.value.code == "PT-E-TAX-NO-RATE-SCHEDULE"
    assert exc.value.exit_code == 4
    assert "will not assume a zero rate" in (exc.value.remedy or "")
    assert exc.value.context["known_schedules"] == ["2026-01-01"]


def test_the_effective_rate_is_explainable_from_its_components() -> None:
    """Not a magic number: federal + state + NIIT, each stored separately."""
    s = schedule()
    assert s.effective_rate(HoldingPeriod.SHORT) == D("0.458")
    assert s.effective_rate(HoldingPeriod.LONG) == D("0.288")


# ── estimation ───────────────────────────────────────────────────────────────


def test_the_estimate_carries_its_components_and_the_schedule_it_used() -> None:
    result = ENGINE.estimate(disposition(), account(), [schedule()])

    assert result.is_taxable is True
    assert result.estimated_tax == D("1440.00")  # 5000 x 0.288
    assert result.federal_rate == D("0.20")
    assert result.state_rate == D("0.05")
    assert result.niit_rate == D("0.038")
    assert result.rate_id == 1, "the schedule used is recorded, so it can be traced"


def test_short_and_long_gains_are_estimated_at_different_rates() -> None:
    short = ENGINE.estimate(disposition(period=HoldingPeriod.SHORT), account(), [schedule()])
    long = ENGINE.estimate(disposition(period=HoldingPeriod.LONG), account(), [schedule()])
    assert short.estimated_tax == D("2290.00")  # 5000 x 0.458
    assert long.estimated_tax == D("1440.00")
    assert short.estimated_tax > long.estimated_tax


@pytest.mark.parametrize("account_type", [AccountType.TAX_DEFERRED, AccountType.TAX_EXEMPT])
def test_a_sheltered_account_reports_inapplicable_rather_than_zero(
    account_type: AccountType,
) -> None:
    """CLAUDE.md's explicit-null rule, applied to tax.

    `is_taxable=False` with `estimated_tax=None` says "inapplicable". A zero
    would say "we computed it and it came to nothing", which is a different and
    false statement -- and it would be indistinguishable from a genuine
    zero-rate result.
    """
    result = ENGINE.estimate(disposition(), account(account_type), [])
    assert result.is_taxable is False
    assert result.estimated_tax is None
    assert result.gain == D("5000.00"), "the gain itself is still exact and reported"


def test_a_sheltered_account_needs_no_rate_schedule() -> None:
    """It does not consult one, so a missing schedule is not an error there."""
    ENGINE.estimate(disposition(), account(AccountType.TAX_DEFERRED), [])


def test_a_realized_loss_produces_a_negative_estimate() -> None:
    """The value of the deduction at this rate -- not a refund, and not netted.

    portable cannot see the capital-loss limitation, the $3,000 ordinary
    offset, or any carryforward, so it reports the arithmetic and says in the
    disclaimer that it does not model the rules that would bound it.
    """
    result = ENGINE.estimate(disposition(gain="-2000.00"), account(), [schedule()])
    assert result.gain == D("-2000.00")
    assert result.estimated_tax == D("-576.00")


# ── summary ──────────────────────────────────────────────────────────────────


def test_short_and_long_are_never_netted_into_one_figure() -> None:
    """They are taxed differently, and the netting rules between them are
    exactly the part this engine does not model."""
    gains = [
        ENGINE.estimate(
            disposition(gain="5000.00", period=HoldingPeriod.LONG, disposition_id=1),
            account(),
            [schedule()],
        ),
        ENGINE.estimate(
            disposition(gain="-1000.00", period=HoldingPeriod.SHORT, disposition_id=2),
            account(),
            [schedule()],
        ),
    ]
    summary = ENGINE.summarise(gains, 2025)

    assert summary.long_term_gain == D("5000.00")
    assert summary.short_term_gain == D("-1000.00")
    assert summary.long_term_tax == D("1440.00")
    assert summary.short_term_tax == D("-458.00")
    assert summary.disposition_count == 2


def test_the_summary_only_covers_the_requested_year() -> None:
    gains = [
        ENGINE.estimate(
            disposition(on=date(2024, 6, 1), disposition_id=1), account(), [schedule()]
        ),
        ENGINE.estimate(
            disposition(on=date(2025, 6, 1), disposition_id=2), account(), [schedule()]
        ),
    ]
    assert ENGINE.summarise(gains, 2025).disposition_count == 1


def test_the_wash_sale_and_not_tax_advice_statements_are_not_suppressible() -> None:
    """ADR 0011.

    A tax report that quietly omits wash sales is the silently-wrong-number
    failure mode by definition: the 30-day window spans every account the
    taxpayer has, including IRAs.
    """
    summary = ENGINE.summarise([], 2025)
    assert summary.excludes_wash_sales is True
    assert "wash sale" in summary.disclaimer.lower()
    assert "not tax advice" in summary.disclaimer.lower()
    assert "1099-B" in summary.disclaimer


def test_net_of_tax_distinguishes_no_estimate_from_zero_tax() -> None:
    assert ENGINE.net_of_tax(D("5000.00"), D("1440.00")) == D("3560.00")
    assert ENGINE.net_of_tax(D("5000.00"), None) == D("5000.00")
    assert ENGINE.net_of_tax(D("5000.00"), D("0.00")) == D("5000.00")
