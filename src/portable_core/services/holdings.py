"""What the portfolio holds on a date, valued as a snapshot values it.

`pt holdings` lists every holding, cash and accrued income line by line, and
its total is the market value `pt mark` gives for the same accounts that day:

    securities + cash - margin loan + accrued income

Each term is valued here by the function the snapshot uses, not again:

* a security by :meth:`ValuationEngine.price_holding`, which refuses a missing
  or stale price (``PORT-GIPS-A09``), or at zero, at level 5, when nothing
  quotes it (ADR 0028);
* accrued interest and declared dividends by
  :func:`snapshots.accrued_income`, part by part (``PORT-GIPS-A06``,
  ADR 0041);
* cash as the snapshot counts it: in the total unless the account holds
  operating cash, which is shown and left out (``PORT-GIPS-A07``).

**A total over part of the portfolio is not a total.** A line that cannot be
valued -- a missing or stale price, interest that cannot be accrued -- leaves
the total and every weight unknown: a weight over the rest would overstate
every other holding and still sum to 100%. So both are null, and what could be
valued is reported beside them under its own name (invariant 10).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from portable_core.decimals import money_context, quantize_money
from portable_core.domain.enums import CashTreatment
from portable_core.domain.models import Account, Instrument, Price
from portable_core.errors import DataUnavailableError
from portable_core.persistence.repositories import Repositories
from portable_core.services import snapshots
from portable_core.services.replay import state_on
from portable_core.services.valuation import Holding, PriceStatus, ValuationEngine

__all__ = ["HoldingLine", "HoldingsView", "LineKind", "Unvalued", "holdings_on"]

ZERO = Decimal("0.00")


class LineKind(enum.StrEnum):
    """What a line of holdings is."""

    LEG = "leg"
    CASH = "cash"
    #: Only for an account holding operating cash, whose cash is left out of
    #: the total while its margin loan is not.
    MARGIN = "margin_loan"
    ACCRUED_INTEREST = "accrued_interest"
    ACCRUED_DIVIDEND = "accrued_dividend"


@dataclass(frozen=True, slots=True)
class HoldingLine:
    """One line of what an account holds."""

    account: Account
    kind: LineKind
    #: The instrument held, or that the income accrues on. None for cash.
    instrument: Instrument | None
    position_id: int | None
    role: str
    quantity: Decimal | None
    price: Price | None
    #: Null when the line cannot be valued. Never zero for that reason.
    market_value: Decimal | None
    #: Null for accrued income, which has no basis.
    cost_basis: Decimal | None
    #: Whether the line counts in the total: False only for operating cash.
    in_total: bool = True

    @property
    def unrealized(self) -> Decimal | None:
        if self.market_value is None or self.cost_basis is None:
            return None
        with money_context():
            return quantize_money(self.market_value - self.cost_basis)


@dataclass(frozen=True, slots=True)
class Unvalued:
    """A line that cannot be valued, and why: the reason there is no total."""

    account: str
    symbol: str
    #: The error the valuation raised: PT-E-PRICE-MISSING, PT-E-PRICE-STALE,
    #: PT-E-ACCRUAL-UNAVAILABLE or PT-E-COUPON-UNRECORDED.
    code: str
    message: str
    #: For a stale price, its date and age.
    price_date: date | None = None
    days_old: int | None = None


@dataclass(frozen=True, slots=True)
class HoldingsView:
    """Every line held on a date, and the total when there is one."""

    lines: tuple[HoldingLine, ...]
    unvalued: tuple[Unvalued, ...]
    #: Operating cash shown and left out of the total, by account name.
    excluded_cash: tuple[tuple[str, Decimal], ...]

    @property
    def is_complete(self) -> bool:
        return not self.unvalued

    @property
    def valued_market_value(self) -> Decimal:
        """The sum of the lines that could be valued: the total, when complete."""
        with money_context():
            return quantize_money(
                sum(
                    (
                        line.market_value
                        for line in self.lines
                        if line.in_total and line.market_value is not None
                    ),
                    ZERO,
                )
            )

    @property
    def total_market_value(self) -> Decimal | None:
        """What `pt mark` values the accounts at, or None when a line is unvalued."""
        return self.valued_market_value if self.is_complete else None

    def weight(self, market_value: Decimal | None, *, in_total: bool = True) -> Decimal | None:
        """*market_value* as a share of the total, or None when either is unknown."""
        total = self.total_market_value
        if market_value is None or not in_total or total is None or total == 0:
            return None
        return market_value / total


def holdings_on(
    repos: Repositories,
    accounts: list[Account],
    as_of: date,
    *,
    staleness_tolerance_days: int,
) -> tuple[HoldingsView, tuple[str, ...]]:
    """What *accounts* held at the end of *as_of*, valued as a snapshot values it.

    Positions, lots and cash are derived state as it stood at the end of
    *as_of* (ADR 0021). Declared distributions and recorded coupons are read
    from the ledger as it now stands, as :func:`snapshots.value_range` reads
    them: a distribution is dated its pay-date, after the day that accrues it.
    Returns the view and the replay's warnings.
    """
    engine = ValuationEngine(staleness_tolerance_days=staleness_tolerance_days)
    declared = snapshots.declared_by_account(repos)
    coupons = snapshots.coupons_by_account(repos)

    def read() -> HoldingsView:
        lines: list[HoldingLine] = []
        unvalued: list[Unvalued] = []
        excluded: list[tuple[str, Decimal]] = []
        for target in accounts:
            for position in repos.positions.all(account_id=target.account_id, open_only=True):
                for leg in position.legs:
                    if leg.quantity == 0:
                        continue
                    instrument = repos.instruments.get(leg.instrument_id)
                    if instrument is None:
                        continue
                    holding = Holding(instrument, leg.signed_quantity)
                    price: Price | None = None
                    value: Decimal | None
                    if not instrument.has_market_price:
                        value = engine.without_market_price(holding, as_of).market_value
                    else:
                        price = repos.prices.newest_on_or_before(
                            instrument.instrument_id, as_of
                        )
                        try:
                            value = engine.price_holding(holding, price, as_of).market_value
                        except DataUnavailableError as gap:
                            value = None
                            stale = engine.price_status(price, as_of) is PriceStatus.STALE
                            unvalued.append(
                                Unvalued(
                                    account=target.name,
                                    symbol=instrument.symbol,
                                    code=gap.code,
                                    message=gap.message,
                                    price_date=price.price_date if price and stale else None,
                                    days_old=(
                                        (as_of - price.price_date).days
                                        if price and stale
                                        else None
                                    ),
                                )
                            )
                    basis = sum(
                        (lot.adjusted_cost_basis for lot in repos.lots.by_leg(leg.leg_id)), ZERO
                    )
                    # ADR 0038: a short leg's lots hold what the sale received,
                    # shown negative beside its negative value.
                    lines.append(
                        HoldingLine(
                            account=target,
                            kind=LineKind.LEG,
                            instrument=instrument,
                            position_id=position.position_id,
                            role=str(leg.role),
                            quantity=leg.signed_quantity,
                            price=price,
                            market_value=value,
                            cost_basis=ZERO - basis if leg.sign < 0 else basis,
                        )
                    )

            income = snapshots.accrued_income(
                snapshots.held(repos, target.account_id),
                as_of,
                engine=engine,
                declared=declared.get(target.account_id, ()),
                coupons=coupons.get(target.account_id, {}),
            )
            lines.extend(_accrued_lines(repos, target, income))
            for unaccrued in income.unavailable:
                unvalued.append(
                    Unvalued(
                        target.name,
                        unaccrued.instrument.symbol,
                        unaccrued.code,
                        unaccrued.message,
                    )
                )
                lines.append(
                    HoldingLine(
                        account=target,
                        kind=LineKind.ACCRUED_INTEREST,
                        instrument=unaccrued.instrument,
                        position_id=None,
                        role=str(LineKind.ACCRUED_INTEREST),
                        quantity=None,
                        price=None,
                        market_value=None,
                        cost_basis=None,
                    )
                )

            cash, margin = repos.valuations.cash(target.account_id, currency=target.currency)
            lines.extend(_cash_lines(target, cash, margin, excluded))
        return HoldingsView(
            lines=tuple(lines), unvalued=tuple(unvalued), excluded_cash=tuple(excluded)
        )

    walked = state_on(repos, as_of, read)
    return walked.results[0], walked.warnings


def _accrued_lines(
    repos: Repositories, account: Account, income: snapshots.AccruedIncome
) -> list[HoldingLine]:
    """One line per instrument and kind of income accrued on it, in order of first part."""
    totals: dict[tuple[int | None, snapshots.AccrualKind], Decimal] = {}
    with money_context():
        for part in income.parts:
            key = (part.instrument_id, part.kind)
            totals[key] = totals.get(key, ZERO) + part.amount
    lines: list[HoldingLine] = []
    for (instrument_id, kind), amount in totals.items():
        if amount == 0:
            continue  # a bond on its coupon date has accrued nothing
        instrument = repos.instruments.get(instrument_id) if instrument_id is not None else None
        line_kind = (
            LineKind.ACCRUED_INTEREST
            if kind is snapshots.AccrualKind.INTEREST
            else LineKind.ACCRUED_DIVIDEND
        )
        lines.append(
            HoldingLine(
                account=account,
                kind=line_kind,
                instrument=instrument,
                position_id=None,
                role=str(line_kind),
                quantity=None,
                price=None,
                market_value=amount,
                cost_basis=None,
            )
        )
    return lines


def _cash_lines(
    account: Account,
    cash: Decimal,
    margin: Decimal,
    excluded: list[tuple[str, Decimal]],
) -> list[HoldingLine]:
    """Cash as the snapshot counts it (``PORT-GIPS-A07``, ``D04``)."""

    def line(kind: LineKind, amount: Decimal, *, in_total: bool) -> HoldingLine:
        with money_context():
            amount = quantize_money(amount)
        return HoldingLine(
            account=account,
            kind=kind,
            instrument=None,
            position_id=None,
            role=str(kind),
            quantity=None,
            price=None,
            market_value=amount,
            cost_basis=amount,
            in_total=in_total,
        )

    if account.cash_treatment is CashTreatment.INVESTED:
        return [line(LineKind.CASH, cash - margin, in_total=True)] if cash or margin else []
    # Operating cash is held, and shown, but it is not in the value a return
    # is measured on; the margin loan still is, since leverage is never
    # grossed up (PORT-GIPS-D04).
    lines: list[HoldingLine] = []
    if cash:
        excluded.append((account.name, cash))
        lines.append(line(LineKind.CASH, cash, in_total=False))
    if margin:
        lines.append(line(LineKind.MARGIN, ZERO - margin, in_total=True))
    return lines
