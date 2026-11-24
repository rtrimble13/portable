"""Lot-level sale candidates: each open lot as if sold alone today. ADR 0047.

For every open long lot in a taxable account, the sale `pt sell --lots
@key:qty` would record on ``as_of`` if it sold that lot and nothing else:
the proceeds at the newest price the staleness rule allows, the gain against
the lot's basis, the holding period, the estimated tax, and for a loss, the
replacements already made that would wash it.

Nothing here computes anything the sale path does not. Each row is the sale
path's own arithmetic run on a disposition that is never stored:

* the price, by :meth:`ValuationEngine.price_status` and
  :meth:`Instrument.value_of` (``PORT-GIPS-A09``, ADR 0041);
* the disposition, by :meth:`LotEngine.select` and :meth:`LotEngine.realize`,
  under specific identification, as a ``--lots`` sale is;
* the replacements, by :func:`wash_sales.plan_matches`, the one statement of
  which shares a loss would wash, and the disallowed part of the loss by
  :func:`wash_sales.disallowed_part` (ADR 0037);
* the estimate, by :meth:`TaxEngine.estimate` (ADR 0011).

The rows are alternatives, not parts of one sale: two loss lots can each be
washed by the same ten recent shares, and sold together only ten wash. So
there are no totals. A figure that cannot be stated -- no price, a stale
price, a basis that is not a basis, no rate -- is null with its reason, never
a zero (invariants 9 and 10). Nothing is written and nothing is proposed.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from portable_core.decimals import money_context, quantize_money
from portable_core.domain.dates import days_between, holding_period, long_term_from
from portable_core.domain.enums import BasisSource, HoldingPeriod, ReliefMethod, WashSaleStatus
from portable_core.domain.models import (
    Account,
    Instrument,
    Lot,
    LotDisposition,
    Price,
    RealizedGain,
    TaxRateSchedule,
)
from portable_core.errors import PortableError
from portable_core.errors.kinds import E_TAX_NO_RATE_SCHEDULE
from portable_core.persistence.repositories import Repositories
from portable_core.services.lots import LotEngine
from portable_core.services.replay import state_on
from portable_core.services.tax import APPROXIMATE_SOURCES, TaxEngine
from portable_core.services.valuation import PriceStatus, ValuationEngine
from portable_core.services.wash_sales import WINDOW, disallowed_part, plan_matches

__all__ = [
    "AveragedHolding",
    "Candidate",
    "CandidateReport",
    "CandidateSort",
    "Replacement",
    "Unstated",
    "UnstatedReason",
    "WashCheck",
    "sale_candidates",
]

ZERO = Decimal("0.00")

#: Tax per dollar raised is a ratio, not money: six places is finer than any
#: rate a schedule holds.
PER_DOLLAR: Final = Decimal("0.000001")


class CandidateSort(enum.StrEnum):
    """``--sort`` (ADR 0047 §7). Unknown figures sort last, ties by acquisition."""

    #: Oldest first, as `pt lot list` reads. The default.
    ACQUIRED = "acquired"
    #: The most tax saved per dollar raised first.
    TAX_PER_DOLLAR = "tax-per-dollar"
    DAYS_TO_LONG_TERM = "days-to-long-term"
    #: The largest loss first.
    GAIN = "gain"


class UnstatedReason(enum.StrEnum):
    """Why a figure on a row is null rather than a number."""

    #: No price on or before the date.
    PRICE_MISSING = "price_missing"
    #: The newest price is older than the staleness tolerance (PORT-GIPS-A09).
    PRICE_STALE = "price_stale"
    #: The instrument says nothing quotes it (ADR 0028). Its zero is a
    #: valuation convention, not a price anyone would pay.
    NO_MARKET_PRICE = "no_market_price"
    #: Seeded at cutover with no derivable basis: a value, not a cost (ADR
    #: 0017 §2b). A gain against it is the change since an arbitrary date.
    BASIS_UNAVAILABLE = "basis_unavailable"
    #: The account has no tax rate schedule in force on the date.
    NO_RATE_SCHEDULE = "no_rate_schedule"


@dataclass(frozen=True, slots=True)
class Unstated:
    """A reason a row's figure is null, with the error code where one was raised."""

    reason: UnstatedReason
    message: str
    code: str | None = None


@dataclass(frozen=True, slots=True)
class Replacement:
    """Shares already acquired that a loss on this lot, sold today, would pair with."""

    lot_key: str
    account: str
    acquired: date
    quantity: Decimal
    #: The part of the loss these shares would disallow, to the cent.
    disallowed: Decimal
    #: DEFERRED into a taxable replacement's basis; PERMANENT for one in a
    #: sheltered account, which loses the loss for good (Rev. Rul. 2008-5);
    #: MANUAL for shares since disposed of, reported and never applied.
    status: WashSaleStatus


@dataclass(frozen=True, slots=True)
class WashCheck:
    """ADR 0047 §5, for a lot that would be sold at a loss."""

    replacements: tuple[Replacement, ...]
    #: The first sale date no replacement already made falls within 30 days
    #: before, provided nothing more is bought. None when there is none.
    window_clears: date | None
    #: Buying the instrument, or one declared identical, in any account
    #: through this date would wash the loss -- a dividend reinvestment too.
    buying_washes_through: date

    @property
    def washed_quantity(self) -> Decimal:
        return sum((r.quantity for r in self.replacements), Decimal(0))

    def disallowed(self, status: WashSaleStatus) -> Decimal:
        return sum((r.disallowed for r in self.replacements if r.status is status), ZERO)

    @property
    def applied(self) -> Decimal:
        """What the sale would record as disallowed: deferred and permanent alike."""
        return self.disallowed(WashSaleStatus.DEFERRED) + self.disallowed(
            WashSaleStatus.PERMANENT
        )


@dataclass(frozen=True, slots=True)
class Candidate:
    """One open lot, sold alone, all of it, on the report's date."""

    lot: Lot
    account: Account
    instrument: Instrument
    sale_date: date
    #: What `pt sell --lots` takes to make this sale (ADR 0036).
    designation: str
    holding_period: HoldingPeriod
    days_held: int
    long_term_from: date
    #: Zero once long-term.
    days_to_long_term: int
    price: Price | None
    price_status: PriceStatus | None
    #: Null, with a reason, when no price can be used.
    proceeds: Decimal | None
    #: Null when proceeds are, or the basis is unavailable.
    gain: Decimal | None
    #: A basis from a custodian, a reconstruction or an estimate, not this
    #: portfolio's own arithmetic (ADR 0017 §3).
    is_approximate: bool
    #: For a loss; None for a gain or when the gain is unknown.
    wash: WashCheck | None
    #: The gain the estimate is taken on: the loss less what a wash disallows.
    allowed_gain: Decimal | None
    #: Null with a reason when the gain is, or no rate schedule is in force.
    tax: RealizedGain | None
    tax_per_dollar: Decimal | None
    #: For a short-term lot: the estimate sold today less the estimate sold on
    #: its first long-term day, at today's price and gain before any wash, at
    #: the rate in force each day. Positive means waiting saves tax.
    waiting_saves: Decimal | None
    unstated: tuple[Unstated, ...]

    @property
    def estimated_tax(self) -> Decimal | None:
        return self.tax.estimated_tax if self.tax is not None else None

    @property
    def price_age_days(self) -> int | None:
        if self.price is None:
            return None
        return days_between(self.price.price_date, self.sale_date)


@dataclass(frozen=True, slots=True)
class AveragedHolding:
    """An instrument in an account sold under average cost: no lot can be chosen (§6)."""

    account: str
    symbol: str
    lots: int
    quantity: Decimal
    reason: str


@dataclass(frozen=True, slots=True)
class CandidateReport:
    """Every candidate on one date, and what the report does not list."""

    as_of: date
    staleness_tolerance_days: int
    rows: tuple[Candidate, ...]
    averaged: tuple[AveragedHolding, ...]
    #: Open short lots: closing one is a purchase, not a sale.
    short_lots: int
    #: Open long lots in sheltered accounts, which owe no tax on a sale. Their
    #: purchases still count as replacements (§5).
    sheltered_lots: int


def sale_candidates(
    repos: Repositories,
    *,
    as_of: date,
    staleness_tolerance_days: int,
    account: Account | None = None,
    instrument: Instrument | None = None,
    losses_only: bool = False,
    sort: CandidateSort = CandidateSort.ACQUIRED,
) -> tuple[CandidateReport, tuple[str, ...]]:
    """Every open long lot in a taxable account, sold alone on *as_of*.

    Read from derived state as it stood at the end of *as_of* (ADR 0021), so
    a lot sold since is listed and one bought since is not. Returns the
    report and the replay's warnings, which the caller shows.
    """
    engine = ValuationEngine(staleness_tolerance_days=staleness_tolerance_days)

    def read() -> CandidateReport:
        rows: list[Candidate] = []
        averaged: list[AveragedHolding] = []
        short_lots = 0
        sheltered_lots = 0
        accounts = [account] if account is not None else repos.accounts.all()
        instruments = [instrument] if instrument is not None else repos.instruments.all()
        for target in accounts:
            for held in instruments:
                lots = [
                    lot
                    for lot in repos.lots.open_lots(
                        target.account_id, held.instrument_id, as_of=as_of
                    )
                    if lot.is_open and lot.remaining_quantity > 0
                ]
                short_lots += sum(1 for lot in lots if lot.is_short)
                longs = [lot for lot in lots if not lot.is_short]
                if not longs:
                    continue
                if not target.is_taxable:
                    sheltered_lots += len(longs)
                    continue
                reason = _averaged(repos, target, held)
                if reason is not None:
                    averaged.append(
                        AveragedHolding(
                            account=target.name,
                            symbol=held.symbol,
                            lots=len(longs),
                            quantity=sum((lot.remaining_quantity for lot in longs), Decimal(0)),
                            reason=reason,
                        )
                    )
                    continue
                price = repos.prices.newest_on_or_before(held.instrument_id, as_of)
                status = engine.price_status(price, as_of)
                for lot in longs:
                    row = _candidate(repos, target, held, lot, as_of, price, status)
                    if losses_only and (row.gain is None or row.gain >= 0):
                        continue
                    rows.append(row)
        rows.sort(key=_ORDER[sort])
        return CandidateReport(
            as_of=as_of,
            staleness_tolerance_days=staleness_tolerance_days,
            rows=tuple(rows),
            averaged=tuple(sorted(averaged, key=lambda a: (a.account, a.symbol))),
            short_lots=short_lots,
            sheltered_lots=sheltered_lots,
        )

    walked = state_on(repos, as_of, read)
    return walked.results[0], walked.warnings


def _averaged(repos: Repositories, account: Account, instrument: Instrument) -> str | None:
    """Why no lot of *instrument* in *account* can be chosen, or None (ADR 0047 §6).

    Average cost and specific identification cannot be mixed for one
    instrument, in any account (`LotEngine.check_method_consistency`), so a
    `--lots` sale of it would be refused, and a lot's own basis no longer
    exists under the election.
    """
    if ReliefMethod.AVERAGE.value in repos.lots.methods_used_for(instrument.instrument_id):
        return "sold under average cost before: a lot cannot be designated"
    if account.default_relief_method is ReliefMethod.AVERAGE:
        return "the account's default relief method is average cost"
    return None


def _candidate(
    repos: Repositories,
    account: Account,
    instrument: Instrument,
    lot: Lot,
    as_of: date,
    price: Price | None,
    status: PriceStatus,
) -> Candidate:
    """The lot sold alone, all of it, on *as_of*: the sale path's arithmetic, unstored."""
    quantity = lot.remaining_quantity
    period = holding_period(lot.holding_period_start, as_of)
    first_long = long_term_from(lot.holding_period_start)
    unstated: list[Unstated] = []

    proceeds: Decimal | None = None
    if not instrument.has_market_price:
        unstated.append(
            Unstated(
                UnstatedReason.NO_MARKET_PRICE,
                f"{instrument.symbol} has no market price; it is valued at zero by "
                "convention (ADR 0028), which is not what a sale would raise",
            )
        )
    elif price is None or status is PriceStatus.MISSING:
        unstated.append(
            Unstated(
                UnstatedReason.PRICE_MISSING,
                f"no price for {instrument.symbol} on or before {as_of.isoformat()}",
            )
        )
    elif status is PriceStatus.STALE:
        unstated.append(
            Unstated(
                UnstatedReason.PRICE_STALE,
                f"the newest price for {instrument.symbol} is "
                f"{days_between(price.price_date, as_of)} days old "
                f"({price.price_date.isoformat()}), beyond the staleness tolerance",
            )
        )
    else:
        with money_context():
            # What the sale path takes as gross proceeds for a sale at this
            # price with no amount stated (ReplayEngine, a closing trade).
            proceeds = abs(quantize_money(instrument.value_of(quantity, price.price)))

    if lot.basis_source is BasisSource.UNAVAILABLE:
        unstated.append(
            Unstated(
                UnstatedReason.BASIS_UNAVAILABLE,
                "the lot was seeded at cutover with no derivable basis; its value "
                "is not a cost, so no gain can be stated (ADR 0017 §2b)",
            )
        )

    disposition: LotDisposition | None = None
    if proceeds is not None and lot.basis_source is not BasisSource.UNAVAILABLE:
        lots = LotEngine()
        plan = lots.select(
            [lot], quantity, ReliefMethod.SPEC, as_of, selection={lot.lot_id: quantity}
        )
        (disposition,) = lots.realize(
            plan,
            txn_id=0,
            account_id=account.account_id,
            instrument_id=instrument.instrument_id,
            disposition_date=as_of,
            gross_proceeds=proceeds,
        )

    wash: WashCheck | None = None
    if disposition is not None and disposition.realized_gain < 0:
        wash = _wash_check(repos, lot, disposition, as_of)
        disposition = replace(disposition, wash_sale_deferred=wash.applied or None)

    tax: RealizedGain | None = None
    waiting: Decimal | None = None
    if disposition is not None:
        estimator = TaxEngine()
        schedules = repos.accounts.rate_schedules(account.account_id)
        try:
            tax = estimator.estimate(disposition, account, schedules)
            if period is HoldingPeriod.SHORT:
                waiting = _waiting(
                    estimator, disposition, account, schedules, lot.holding_period_start
                )
        except PortableError as missing:
            if missing.code != E_TAX_NO_RATE_SCHEDULE:
                raise
            unstated.append(
                Unstated(UnstatedReason.NO_RATE_SCHEDULE, missing.message, missing.code)
            )

    per_dollar: Decimal | None = None
    if tax is not None and tax.estimated_tax is not None and proceeds:
        with money_context():
            per_dollar = (tax.estimated_tax / proceeds).quantize(PER_DOLLAR)

    return Candidate(
        lot=lot,
        account=account,
        instrument=instrument,
        sale_date=as_of,
        designation=f"@{lot.lot_key}:{quantity}",
        holding_period=period,
        days_held=days_between(lot.holding_period_start, as_of),
        long_term_from=first_long,
        days_to_long_term=max(0, days_between(as_of, first_long)),
        price=price,
        price_status=status,
        proceeds=proceeds,
        gain=disposition.realized_gain if disposition is not None else None,
        is_approximate=lot.basis_source in APPROXIMATE_SOURCES
        and lot.basis_source is not BasisSource.UNAVAILABLE,
        wash=wash,
        allowed_gain=disposition.allowed_gain if disposition is not None else None,
        tax=tax,
        tax_per_dollar=per_dollar,
        waiting_saves=waiting,
        unstated=tuple(unstated),
    )


def _wash_check(
    repos: Repositories, sold: Lot, disposition: LotDisposition, as_of: date
) -> WashCheck:
    """What the matcher would pair this loss with, were the sale applied now (§5).

    The plan is :func:`plan_matches`'s, the one the matcher applies, and each
    part of the loss is :func:`disallowed_part`'s, cumulative to the cent as
    the matcher takes it. The sold lot is never its own replacement
    (:func:`wash_sales.replaces`), so nothing it sells needs setting aside.
    """
    plan = plan_matches(
        repos,
        sold=sold,
        instrument_id=disposition.instrument_id,
        sale_date=as_of,
        quantity=disposition.quantity,
        sold_here={},
    )
    loss = -disposition.realized_gain
    matched = Decimal(0)
    found: list[Replacement] = []
    for match in plan:
        part = disallowed_part(loss, disposition.quantity, matched, match.quantity)
        matched += match.quantity
        where = repos.accounts.get(match.lot.account_id)
        assert where is not None
        if match.gone:
            status = WashSaleStatus.MANUAL
        elif where.is_taxable:
            status = WashSaleStatus.DEFERRED
        else:
            status = WashSaleStatus.PERMANENT
        found.append(
            Replacement(
                lot_key=match.lot.lot_key,
                account=where.name,
                acquired=match.lot.open_date,
                quantity=match.quantity,
                disallowed=part,
                status=status,
            )
        )
    clears = max(r.acquired for r in found) + WINDOW + timedelta(days=1) if found else None
    return WashCheck(
        replacements=tuple(found),
        window_clears=clears,
        buying_washes_through=as_of + WINDOW,
    )


def _waiting(
    estimator: TaxEngine,
    disposition: LotDisposition,
    account: Account,
    schedules: list[TaxRateSchedule],
    start: date,
) -> Decimal | None:
    """Today's estimate less the estimate on the first long-term day (§4).

    Both on today's gain before any wash sale: whether a sale on the later day
    is washed depends on purchases not yet made. The same proceeds, the
    holding period and the rate in force on each day.
    """
    first_long = long_term_from(start)
    unwashed = replace(disposition, wash_sale_deferred=None)
    later = replace(
        unwashed,
        disposition_date=first_long,
        holding_period=holding_period(start, first_long),
        days_held=days_between(start, first_long),
    )
    today = estimator.estimate(unwashed, account, schedules).estimated_tax
    then = estimator.estimate(later, account, schedules).estimated_tax
    if today is None or then is None:
        return None
    with money_context():
        return today - then


def _acquired(row: Candidate) -> tuple[object, ...]:
    """Oldest first, then by account, symbol and lineage: a total order."""
    return (row.lot.open_date, row.account.name, row.instrument.symbol, row.lot.lot_key)


_ORDER: Final = {
    CandidateSort.ACQUIRED: _acquired,
    CandidateSort.TAX_PER_DOLLAR: lambda row: (
        row.tax_per_dollar is None,
        row.tax_per_dollar if row.tax_per_dollar is not None else ZERO,
        *_acquired(row),
    ),
    CandidateSort.DAYS_TO_LONG_TERM: lambda row: (row.days_to_long_term, *_acquired(row)),
    CandidateSort.GAIN: lambda row: (
        row.gain is None,
        row.gain if row.gain is not None else ZERO,
        *_acquired(row),
    ),
}
