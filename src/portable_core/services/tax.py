"""The tax engine: estimated liability on realized gains.

ADR 0011 draws the line this module lives on, and the line is the point:

* **Exact** -- realized gain, holding period, basis adjustments, lot selection.
  Every input is a ledger fact.
* **Estimated** -- the liability itself, computed as gain times the account's
  effective-dated rate for the holding period at disposition. Deliberately
  naive, and labelled.
* **Refused** -- a missing rate schedule, and anything wash-sale-dependent.

The estimate does **not** model bracket progressivity, the capital-loss
limitation or its carryforward, the $3,000 ordinary offset, qualified-dividend
rate stacking, AMT, state treatment of federal gains, or the taxpayer's other
income. It cannot: `portable` does not know any of that. It is useful for
comparing two dispositions and useless as a filing figure, and every output
says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from portable_core.decimals import money_context, quantize_money
from portable_core.disclaimer import TAX_DISCLAIMER
from portable_core.domain.enums import AccountType, HoldingPeriod
from portable_core.domain.models import (
    Account,
    LotDisposition,
    RealizedGain,
    TaxRateSchedule,
)
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_TAX_NO_RATE_SCHEDULE

__all__ = ["TAX_DISCLAIMER", "TaxEngine", "TaxSummary"]

ZERO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class TaxSummary:
    """Realized gains for a period, split the way a Schedule D is."""

    tax_year: int
    short_term_gain: Decimal
    long_term_gain: Decimal
    short_term_tax: Decimal
    long_term_tax: Decimal
    proceeds: Decimal
    cost_basis: Decimal
    disposition_count: int
    #: Accounts whose gains are not taxable, listed so the reader knows the
    #: zero is "inapplicable" rather than "happens to be zero".
    non_taxable_accounts: tuple[str, ...] = ()
    #: Always present. Not suppressible. ADR 0011.
    disclaimer: str = TAX_DISCLAIMER
    #: True while wash-sale detection is unimplemented (v0.2).
    excludes_wash_sales: bool = True

    @property
    def total_gain(self) -> Decimal:
        return self.short_term_gain + self.long_term_gain

    @property
    def total_tax(self) -> Decimal:
        return self.short_term_tax + self.long_term_tax


class TaxEngine:
    """Applies effective-dated rate schedules to realized gains."""

    def rate_in_force(
        self,
        schedules: list[TaxRateSchedule],
        on: date,
        *,
        account_name: str,
    ) -> TaxRateSchedule:
        """The schedule effective on *on*: the latest one not after that date.

        Effective-dating is what stops a rate change next year from
        retroactively restating what last year's sale cost.

        Raises:
            ValidationError: when no schedule is in force. **A missing rate is
                an error, not a zero** -- the same rule PORT-GIPS-B03 applies
                to flow policy, for the same reason: a defaulted zero produces
                a plausible number that is wrong.
        """
        applicable = [s for s in schedules if s.effective_from <= on]
        if not applicable:
            raise ValidationError(
                f"no tax rate schedule in force for account {account_name!r} "
                f"on {on.isoformat()}",
                code=E_TAX_NO_RATE_SCHEDULE,
                remedy=(
                    "Set one with `pt account tax-rates set --account "
                    f"{account_name} --short RATE --long RATE --effective-from DATE`. "
                    "portable will not assume a zero rate."
                ),
                account=account_name,
                date=on.isoformat(),
                known_schedules=[s.effective_from.isoformat() for s in schedules],
            )
        return max(applicable, key=lambda s: s.effective_from)

    def estimate(
        self,
        disposition: LotDisposition,
        account: Account,
        schedules: list[TaxRateSchedule],
    ) -> RealizedGain:
        """Estimate the liability on one disposition.

        In a tax-deferred or tax-exempt account there is no current liability,
        and the result records ``is_taxable=False`` so a reader can tell
        "inapplicable" from "happens to be zero" -- which is `CLAUDE.md`'s
        explicit-null rule applied to tax.
        """
        taxable = account.account_type is AccountType.TAXABLE

        if not taxable:
            return RealizedGain(
                disposition_id=disposition.disposition_id,
                account_id=disposition.account_id,
                instrument_id=disposition.instrument_id,
                tax_year=disposition.disposition_date.year,
                disposition_date=disposition.disposition_date,
                holding_period=disposition.holding_period,
                proceeds=disposition.proceeds,
                cost_basis=disposition.cost_basis_relieved,
                gain=disposition.realized_gain,
                is_taxable=False,
            )

        schedule = self.rate_in_force(
            schedules, disposition.disposition_date, account_name=account.name
        )
        federal = (
            schedule.short_term_federal
            if disposition.holding_period is HoldingPeriod.SHORT
            else schedule.long_term_federal
        )

        with money_context():
            # A loss produces a negative "tax", which is the value of the
            # deduction *at this rate*. It is not a refund, and the summary
            # does not net it against a real liability without saying so --
            # `portable` cannot see the capital-loss limitation or any
            # carryforward (ADR 0011).
            rate = federal + schedule.state + schedule.niit
            estimated = quantize_money(disposition.realized_gain * rate)

        return RealizedGain(
            disposition_id=disposition.disposition_id,
            account_id=disposition.account_id,
            instrument_id=disposition.instrument_id,
            tax_year=disposition.disposition_date.year,
            disposition_date=disposition.disposition_date,
            holding_period=disposition.holding_period,
            proceeds=disposition.proceeds,
            cost_basis=disposition.cost_basis_relieved,
            gain=disposition.realized_gain,
            is_taxable=True,
            rate_id=schedule.rate_id,
            federal_rate=federal,
            state_rate=schedule.state,
            niit_rate=schedule.niit,
            estimated_tax=estimated,
        )

    def summarise(
        self,
        gains: list[RealizedGain],
        tax_year: int,
        *,
        non_taxable_accounts: tuple[str, ...] = (),
    ) -> TaxSummary:
        """Aggregate realized gains into a Schedule-D-shaped summary.

        Short and long are kept apart throughout and are never netted into one
        figure, because they are taxed at different rates and the netting rules
        between them are exactly the part this engine does not model.
        """
        in_year = [g for g in gains if g.tax_year == tax_year]

        with money_context():
            short_gain = sum(
                (g.gain for g in in_year if g.holding_period is HoldingPeriod.SHORT), ZERO
            )
            long_gain = sum(
                (g.gain for g in in_year if g.holding_period is HoldingPeriod.LONG), ZERO
            )
            short_tax = sum(
                (
                    g.estimated_tax or ZERO
                    for g in in_year
                    if g.holding_period is HoldingPeriod.SHORT
                ),
                ZERO,
            )
            long_tax = sum(
                (
                    g.estimated_tax or ZERO
                    for g in in_year
                    if g.holding_period is HoldingPeriod.LONG
                ),
                ZERO,
            )
            proceeds = sum((g.proceeds for g in in_year), ZERO)
            basis = sum((g.cost_basis for g in in_year), ZERO)

        return TaxSummary(
            tax_year=tax_year,
            short_term_gain=short_gain,
            long_term_gain=long_gain,
            short_term_tax=short_tax,
            long_term_tax=long_tax,
            proceeds=proceeds,
            cost_basis=basis,
            disposition_count=len(in_year),
            non_taxable_accounts=non_taxable_accounts,
        )

    @staticmethod
    def net_of_tax(gain: Decimal, estimated_tax: Decimal | None) -> Decimal:
        """Gain less its estimated liability.

        Returns the gain unchanged when there is no estimate, rather than
        treating a missing estimate as zero tax -- those are different
        statements and only one of them is true.
        """
        if estimated_tax is None:
            return gain
        with money_context():
            return quantize_money(gain - estimated_tax)
