"""Policy allocation and drift. ADR 0048.

**Am I where I meant to be?** For each asset class: the target, the band, the
market value and the actual weight, and whether it is outside its band.

- **The value is what `pt mark` gives** (§4): `services/holdings.py`, which
  values through the snapshot's own functions. Its total is securities, plus
  cash, less the margin loan, plus accrued income.
  - A bond's accrued interest goes with the bond's class.
  - A declared dividend goes to `cash`: money owed, not exposure.
  - Operating cash is left out, as the snapshot leaves it out.
- **One target, for the portfolio** (answer 1). Each account is shown by its
  own mix and by its contribution to each class's portfolio weight. The
  contributions sum to the portfolio weight, so an internal transfer moves
  contributions and no portfolio weight.
- **Bands are absolute** (answer 2). A weight exactly on the edge of its band
  is inside.
- **A holding classified across classes** contributes its value to each by
  its stated weight (answer 3). Weights sum to exactly 1, in ``Decimal``.
- **A holding that cannot be valued makes every weight unknown**, so drift is
  refused (answer 4), unless `--estimate MAX_SHARE` carries its price as
  `pert` does (ADR 0044), within that share of the total, marked.
- **Unclassified holdings are named, never bucketed.** They count in the
  total, so every class weight is a share of the whole, and the report is
  incomplete while any is held. So is it while a class held has no target.
- **A missing policy is an error, not a zero** (PT-E-ALLOCATION-NO-POLICY).

Read-only. No trade is proposed: that is issue 36.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Final

from portable_core.decimals import money_context, quantize_money
from portable_core.domain.allocation import (
    CASH_CLASS,
    AllocationPolicy,
    Classification,
    ClassShare,
    Target,
)
from portable_core.domain.models import Instrument
from portable_core.errors import DataUnavailableError, PortableError, ValidationError
from portable_core.errors.kinds import (
    E_ALLOCATION_NO_POLICY,
    E_ALLOCATION_UNKNOWN_CLASS,
    E_ALLOCATION_UNPRICED,
    E_ALLOCATION_WEIGHTS,
    E_COUPON_UNRECORDED,
    E_PRICE_MISSING,
    E_PRICE_STALE,
)
from portable_core.persistence.repositories import Repositories
from portable_core.services import estimates
from portable_core.services import holdings as holdings_service
from portable_core.services.holdings import HoldingLine, LineKind
from portable_core.services.replay import effective_ledger

__all__ = [
    "AccountMix",
    "Carried",
    "ClassDrift",
    "DriftReport",
    "Unclassified",
    "add_class",
    "classes_of",
    "classification_on",
    "classify",
    "drift",
    "policy_on",
    "set_policy",
]

ONE: Final = Decimal(1)
ZERO: Final = Decimal(0)

#: The states of a class against its band.
INSIDE: Final = "inside"
ABOVE: Final = "above"
BELOW: Final = "below"
#: Held, and named in no target of the policy in force.
NO_TARGET: Final = "no_target"


# ── writing ──────────────────────────────────────────────────────────────────


def add_class(repos: Repositories, name: str, description: str) -> int:
    """Add one of the owner's classes. Its name is fixed once added."""
    name = name.strip()
    if not name:
        raise ValidationError("an asset class needs a name")
    if name in repos.allocation.class_ids():
        raise ValidationError(
            f"there is already an asset class {name!r}",
            remedy="`pt allocation class list` shows them.",
            asset_class=name,
        )
    return repos.allocation.add_class(name, description.strip())


def _known(repos: Repositories, names: Sequence[str]) -> None:
    known = repos.allocation.class_ids()
    unknown = sorted(set(names) - set(known))
    if unknown:
        raise ValidationError(
            f"no asset class {', '.join(repr(n) for n in unknown)}",
            code=E_ALLOCATION_UNKNOWN_CLASS,
            remedy=(
                "Add it first with `pt allocation class add NAME --description TEXT`. "
                "A class is declared, never inferred."
            ),
            unknown=unknown,
            known=sorted(known),
        )
    repeated = sorted({n for n in names if list(names).count(n) > 1})
    if repeated:
        raise ValidationError(
            f"asset class {', '.join(repr(n) for n in repeated)} named twice",
            code=E_ALLOCATION_WEIGHTS,
            repeated=repeated,
        )


def _whole(parts: Sequence[Decimal], *, what: str, context: dict[str, object]) -> None:
    """Refuse weights that do not sum to exactly 1."""
    total = sum(parts, ZERO)
    if total != ONE:
        raise ValidationError(
            f"{what} sum to {total}, not exactly 1",
            code=E_ALLOCATION_WEIGHTS,
            remedy="State every share, so they add up to 1: 0.6 and 0.4, not 0.6 alone.",
            total=str(total),
            **context,
        )


def classify(
    repos: Repositories,
    instrument: Instrument,
    effective_from: date,
    shares: Sequence[tuple[str, Decimal]],
    *,
    corrects: bool = False,
    note: str | None = None,
) -> int:
    """Classify *instrument* from *effective_from*, by weights that sum to 1.

    Effective-dated and never edited: a second classification for the same
    date is refused, and one entered wrongly is replaced on its date with
    *corrects* (ADR 0025 decision 5).
    """
    names = [name for name, _ in shares]
    _known(repos, names)
    for name, weight in shares:
        if not ZERO < weight <= ONE:
            raise ValidationError(
                f"{instrument.symbol}'s share in {name!r} is {weight}: a share is more "
                "than 0 and at most 1",
                code=E_ALLOCATION_WEIGHTS,
                symbol=instrument.symbol,
                asset_class=name,
                weight=str(weight),
            )
    _whole(
        [weight for _, weight in shares],
        what=f"{instrument.symbol}'s shares",
        context={"symbol": instrument.symbol},
    )
    same_day = [
        c
        for c in repos.allocation.classifications(instrument_id=instrument.instrument_id)
        if c.effective_from == effective_from
    ]
    corrected = _same_date(
        same_day,
        corrects=corrects,
        what=f"{instrument.symbol}'s classification",
        on=effective_from,
    )
    return repos.allocation.add_classification(
        Classification(
            classification_id=0,
            instrument_id=instrument.instrument_id,
            effective_from=effective_from,
            shares=tuple(ClassShare(name, weight) for name, weight in shares),
            note=note,
            corrects_classification_id=(same_day[0].classification_id if corrected else None),
        )
    )


def set_policy(
    repos: Repositories,
    effective_from: date,
    targets: Sequence[tuple[str, Decimal, Decimal]],
    *,
    corrects: bool = False,
    note: str | None = None,
) -> int:
    """The portfolio's target and band per class, from *effective_from*.

    Targets sum to exactly 1, cash included. A class may have a target of
    zero. Effective-dated and never edited, as :func:`classify`.
    """
    _known(repos, [name for name, _, _ in targets])
    for name, target, band in targets:
        for label, value in (("target", target), ("band", band)):
            if not ZERO <= value <= ONE:
                raise ValidationError(
                    f"{name!r} has a {label} of {value}: it is a fraction from 0 to 1",
                    code=E_ALLOCATION_WEIGHTS,
                    asset_class=name,
                    **{label: str(value)},
                )
    _whole([target for _, target, _ in targets], what="the targets", context={})
    same_day = [p for p in repos.allocation.policies() if p.effective_from == effective_from]
    corrected = _same_date(
        same_day, corrects=corrects, what="an allocation policy", on=effective_from
    )
    return repos.allocation.add_policy(
        AllocationPolicy(
            policy_id=0,
            effective_from=effective_from,
            targets=tuple(Target(name, target, band) for name, target, band in targets),
            note=note,
            corrects_policy_id=same_day[0].policy_id if corrected else None,
        )
    )


def _same_date(same_day: Sequence[object], *, corrects: bool, what: str, on: date) -> bool:
    """ADR 0025 decisions 4 and 5: one row per date, and --corrects replaces it."""
    if corrects and not same_day:
        raise ValidationError(
            f"{what} has no row on {on.isoformat()} to correct",
            remedy="Without --corrects, it is recorded from that date.",
            effective_from=on.isoformat(),
        )
    if same_day and not corrects:
        raise ValidationError(
            f"{what} already has a row on {on.isoformat()}",
            remedy=(
                "Rows are effective-dated and never edited. If that one was entered "
                "wrongly, pass --corrects to record this one in its place on the same "
                "date; the wrong one is kept as history (ADR 0025). A row for a later "
                "date replaces it from that date on."
            ),
            effective_from=on.isoformat(),
        )
    return corrects


# ── reading ──────────────────────────────────────────────────────────────────


def classification_on(
    repos: Repositories, instrument: Instrument, on: date
) -> Classification | None:
    """The classification of *instrument* in force on *on*, held to its sum."""
    found = repos.allocation.classification_in_force(instrument.instrument_id, on)
    if found is not None:
        _whole(
            [share.weight for share in found.shares],
            what=f"{instrument.symbol}'s stored shares",
            context={"symbol": instrument.symbol},
        )
    return found


def policy_on(repos: Repositories, on: date) -> AllocationPolicy:
    """The allocation policy in force on *on*. A missing one is an error, not a zero."""
    policy = repos.allocation.policy_in_force(on)
    if policy is None:
        raise ValidationError(
            f"no allocation policy is in force on {on.isoformat()}",
            code=E_ALLOCATION_NO_POLICY,
            remedy=(
                "Set one with `pt allocation target set CLASS=TARGET:BAND ... "
                "--effective-from DATE`. Drift is judged against a stated target, never "
                "against a zero."
            ),
            as_of=on.isoformat(),
        )
    _whole([t.target for t in policy.targets], what="the stored targets", context={})
    return policy


# ── drift ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ClassDrift:
    """One class, at portfolio level."""

    asset_class: str
    value: Decimal
    weight: Decimal
    #: None for a class held that the policy names no target for.
    target: Decimal | None
    band: Decimal | None
    difference: Decimal | None
    #: inside, above, below, or no_target.
    status: str


@dataclass(frozen=True, slots=True)
class AccountMix:
    """One account: its own mix, and what it contributes to each portfolio weight."""

    account: str
    total: Decimal
    #: By class: (value, the account's own weight, contribution to the
    #: portfolio weight). The contributions sum, over accounts, to the
    #: portfolio weight.
    classes: tuple[tuple[str, Decimal, Decimal | None, Decimal], ...]
    unclassified: Decimal


@dataclass(frozen=True, slots=True)
class Unclassified:
    """A holding no classification covers on the date: named, never bucketed."""

    account: str
    symbol: str
    value: Decimal
    #: Its share of the total, which it is counted in.
    weight: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Carried:
    """A holding valued at a carried price under --estimate (ADR 0044)."""

    account: str
    symbol: str
    quantity: Decimal
    price: Decimal
    price_date: date
    age_days: int
    value: Decimal


@dataclass(frozen=True, slots=True)
class DriftReport:
    """Drift against the policy in force on one date."""

    as_of: date
    policy: AllocationPolicy
    total: Decimal
    classes: tuple[ClassDrift, ...]
    accounts: tuple[AccountMix, ...]
    unclassified: tuple[Unclassified, ...]
    #: The classification read for each instrument held: (symbol, effective_from).
    classified: tuple[tuple[str, date], ...]
    excluded_cash: tuple[tuple[str, Decimal], ...]
    carried: tuple[Carried, ...]

    @property
    def unpoliced(self) -> tuple[str, ...]:
        return tuple(c.asset_class for c in self.classes if c.status == NO_TARGET)

    @property
    def is_complete(self) -> bool:
        return not self.unclassified and not self.unpoliced

    @property
    def is_estimated(self) -> bool:
        return bool(self.carried)

    @property
    def outside(self) -> tuple[ClassDrift, ...]:
        return tuple(c for c in self.classes if c.status in (ABOVE, BELOW))


def drift(
    repos: Repositories,
    as_of: date,
    *,
    staleness_tolerance_days: int,
    estimate_max_share: Decimal | None = None,
) -> tuple[DriftReport, tuple[str, ...]]:
    """Drift on *as_of*, against the policy in force that day.

    Returns the report and the replay's warnings.

    Raises:
        ValidationError: no policy in force (PT-E-ALLOCATION-NO-POLICY), or
            stored weights that do not sum to 1 (PT-E-ALLOCATION-WEIGHTS).
        DataUnavailableError: a holding cannot be valued, and --estimate does
            not cover it (PT-E-ALLOCATION-UNPRICED).
    """
    policy = policy_on(repos, as_of)
    accounts = repos.accounts.all()
    view, warnings = holdings_service.holdings_on(
        repos, accounts, as_of, staleness_tolerance_days=staleness_tolerance_days
    )
    lines, carried = _valued(repos, view, as_of, estimate_max_share)

    # One context for every product and quotient (ADR 0005).
    with money_context():
        by_class: dict[str, Decimal] = {}
        by_account: dict[str, dict[str, Decimal]] = {}
        unclassified: list[Unclassified] = []
        classified: dict[str, date] = {}
        totals: dict[str, Decimal] = {}
        for line, value in lines:
            account = line.account.name
            totals[account] = totals.get(account, ZERO) + value
            shares = classes_of(repos, line, as_of)
            if shares is None:
                symbol = line.instrument.symbol if line.instrument else "CASH"
                unclassified.append(Unclassified(account, symbol, value))
                continue
            if line.instrument is not None and not _is_money(line):
                found = repos.allocation.classification_in_force(
                    line.instrument.instrument_id, as_of
                )
                assert found is not None  # classes_of just read it
                classified[line.instrument.symbol] = found.effective_from
            for share in shares:
                part = value * share.weight
                by_class[share.asset_class] = by_class.get(share.asset_class, ZERO) + part
                mix = by_account.setdefault(account, {})
                mix[share.asset_class] = mix.get(share.asset_class, ZERO) + part

        total = sum(totals.values(), ZERO)
        if total == 0:
            raise ValidationError(
                f"nothing is held on {as_of.isoformat()} to weigh against the policy",
                code=E_ALLOCATION_WEIGHTS,
                as_of=as_of.isoformat(),
            )
        unclassified = [replace(u, weight=u.value / total) for u in unclassified]

        targets = {t.asset_class: t for t in policy.targets}
        names = sorted(set(targets) | set(by_class), key=lambda n: (n != CASH_CLASS, n))
        classes = tuple(
            _class_drift(n, by_class.get(n, ZERO), total, targets.get(n)) for n in names
        )
        mixes = tuple(
            AccountMix(
                account=account,
                total=totals[account],
                classes=tuple(
                    (
                        name,
                        value,
                        value / totals[account] if totals[account] else None,
                        value / total,
                    )
                    for name, value in sorted(
                        by_account.get(account, {}).items(),
                        key=lambda kv: (kv[0] != CASH_CLASS, kv[0]),
                    )
                ),
                unclassified=sum((u.value for u in unclassified if u.account == account), ZERO),
            )
            for account in sorted(totals)
        )
    return (
        DriftReport(
            as_of=as_of,
            policy=policy,
            total=total,
            classes=classes,
            accounts=mixes,
            unclassified=tuple(sorted(unclassified, key=lambda u: (u.account, u.symbol))),
            classified=tuple(sorted(classified.items())),
            excluded_cash=view.excluded_cash,
            carried=carried,
        ),
        warnings,
    )


def _class_drift(
    name: str, value: Decimal, total: Decimal, target: Target | None
) -> ClassDrift:
    weight = value / total
    if target is None:
        return ClassDrift(name, value, weight, None, None, None, NO_TARGET)
    difference = weight - target.target
    if abs(difference) <= target.band:
        status = INSIDE
    else:
        status = ABOVE if difference > 0 else BELOW
    return ClassDrift(name, value, weight, target.target, target.band, difference, status)


def _is_money(line: HoldingLine) -> bool:
    return line.kind in (LineKind.CASH, LineKind.MARGIN, LineKind.ACCRUED_DIVIDEND)


def classes_of(
    repos: Repositories, line: HoldingLine, on: date
) -> tuple[ClassShare, ...] | None:
    """The class or classes a holdings line's value belongs to on *on* (§4).

    Cash, a margin loan and a declared dividend are money held or owed, so
    `cash`, whatever pays them. A position, and a bond's accrued interest, are
    in the instrument's own class or classes. None for an instrument no
    classification covers on the date: unclassified, never guessed.
    """
    if _is_money(line):
        return (ClassShare(CASH_CLASS, ONE),)
    if line.instrument is None:
        return None
    found = classification_on(repos, line.instrument, on)
    return None if found is None else found.shares


def _valued(
    repos: Repositories,
    view: holdings_service.HoldingsView,
    on: date,
    estimate_max_share: Decimal | None,
) -> tuple[list[tuple[HoldingLine, Decimal]], tuple[Carried, ...]]:
    """Every line in the total with its value, carrying prices if allowed."""
    valued = [
        (line, line.market_value)
        for line in view.lines
        if line.in_total and line.market_value is not None
    ]
    missing = [line for line in view.lines if line.in_total and line.market_value is None]
    if not missing:
        return valued, ()
    carriable = {E_PRICE_MISSING, E_PRICE_STALE}
    if estimate_max_share is None or any(gap.code not in carriable for gap in view.unvalued):
        raise _unpriced(view, estimate_max_share)

    carried: list[Carried] = []
    ledger = estimates.Ledger(effective_ledger(repos.transactions.in_ledger_order()))
    accounts = sorted({line.account.account_id for line in missing})
    book = estimates.HoldingsBook(repos, accounts)
    book.load([on])
    entitled: set[date] = set()
    for account_id in accounts:
        entitled |= estimates.entitlement_days(
            repos, ledger, account_id, book.on(account_id, on), on
        )
    book.load(entitled)
    for line in missing:
        instrument = line.instrument
        assert instrument is not None and line.quantity is not None
        try:
            known, _, carry = estimates.carried_price(
                repos,
                ledger,
                book,
                account_id=line.account.account_id,
                instrument=instrument,
                on=on,
            )
        except PortableError as cannot:
            raise _unpriced(view, estimate_max_share, cause=cannot) from None
        with money_context():
            worth = quantize_money(instrument.value_of(line.quantity, carry))
        carried.append(
            Carried(
                account=line.account.name,
                symbol=instrument.symbol,
                quantity=line.quantity,
                price=carry,
                price_date=known.on,
                age_days=(on - known.on).days,
                value=worth,
            )
        )
        valued.append((replace(line, market_value=worth), worth))

    total = sum((value for _, value in valued), ZERO)
    share = sum((c.value for c in carried), ZERO) / total if total else ONE
    if abs(share) > estimate_max_share:
        raise DataUnavailableError(
            f"carried prices would value {share:.2%} of the portfolio, more than the "
            f"{estimate_max_share:.2%} --estimate allows",
            code=E_ALLOCATION_UNPRICED,
            remedy="Price the holdings, or allow a larger share if you mean to.",
            carried_share=str(share),
            max_share=str(estimate_max_share),
            symbols=sorted({c.symbol for c in carried}),
        )
    return valued, tuple(carried)


def _unpriced(
    view: holdings_service.HoldingsView,
    estimate_max_share: Decimal | None,
    *,
    cause: PortableError | None = None,
) -> DataUnavailableError:
    """ADR 0048, answer 4: an unvalued holding leaves every weight unknown."""
    named = [
        f"{gap.symbol} in {gap.account}"
        + (f" (newest price {gap.price_date.isoformat()})" if gap.price_date else "")
        for gap in view.unvalued
    ]
    accrual = [g for g in view.unvalued if g.code not in {E_PRICE_MISSING, E_PRICE_STALE}]
    if cause is not None:
        why = f": {cause.message}"
    elif accrual:
        why = (
            ", and a carried price cannot stand in for accrued interest"
            if estimate_max_share is not None
            else ""
        )
    else:
        why = ""
    return DataUnavailableError(
        f"cannot weigh the portfolio: {', '.join(named)} cannot be valued, so every "
        f"weight is unknown{why}",
        code=E_ALLOCATION_UNPRICED,
        remedy=(
            "Price them (`pt price set` or `pt price load`)"
            + (
                ", record the coupon (`pt income coupon`)"
                if any(g.code == E_COUPON_UNRECORDED for g in view.unvalued)
                else ""
            )
            + ", or, for a price, pass --estimate MAX_SHARE to value it at a carried "
            "price, marked (ADR 0044)."
        ),
        unvalued=[
            {"account": g.account, "symbol": g.symbol, "code": g.code} for g in view.unvalued
        ],
    )
