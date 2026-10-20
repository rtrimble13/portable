"""Comparing the book against a custodian's position statement.

The acceptance criterion for an import (`docs/broker-import.md` §9). Parser
tests establish that an adapter does what it claims; only reconciliation
establishes that what it claims is right.

Three things this has to get right, each of which the previous implementation
got wrong by omission:

**Per account.** Summing every account into one namespace makes two accounts
holding the same fund reconcile as a total, so an overstatement in one can be
cancelled by an understatement in the other and the line still balances.

**Cash.** A quantity-only reconciliation passes on a sign error, a dropped
cash row, and a double-counted transfer -- every failure that leaves the share
counts right and the money wrong. Cash is the only check that catches those,
which is why `docs/broker-import.md` calls it a prerequisite rather than a
refinement.

**Cash equivalents are cash.** A custodian reports its sweep vehicles as
positions; `portable` holds their value as cash (ADR 0013). The statement's
sweep lines are therefore folded into its cash figure before comparison. This
is the one place that decision has to be undone, and doing it here is what
keeps it out of the ledger.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from portable_core.domain.enums import BasisSource
from portable_core.domain.import_records import ClosedLotRecord
from portable_core.domain.models import Account, Instrument, LotDisposition
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_USAGE
from portable_core.persistence.repositories import Repositories
from portable_core.services import price_acceptance

__all__ = [
    "ExternalHolding",
    "RealizedTieLine",
    "RealizedTieResult",
    "ReconciliationLine",
    "ReconciliationResult",
    "ReconciliationService",
]

ZERO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class ExternalHolding:
    """One line as the custodian states it, before anything is resolved."""

    #: The custodian's account name. ``None`` where the statement covers one
    #: account and does not repeat it per row.
    account: str | None
    #: Whatever the statement identifies the holding by -- a ticker, a CUSIP, an
    #: ISIN. Resolution is `portable`'s problem, not the file's.
    identifier: str
    amount: Decimal
    #: True for the statement's cash line and for any sweep vehicle it reports
    #: as a position. Both are cash here (ADR 0013).
    is_cash_equivalent: bool = False
    #: The market value the statement gives a position, when it gives one. The
    #: price check compares it with portable's price (ADR 0031).
    market_value: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationLine:
    """One comparison. ``difference`` is ours less theirs."""

    account: str
    kind: str
    identifier: str
    ours: Decimal
    theirs: Decimal
    difference: Decimal
    is_break: bool


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    lines: tuple[ReconciliationLine, ...]
    tolerance: Decimal

    @property
    def breaks(self) -> tuple[ReconciliationLine, ...]:
        return tuple(line for line in self.lines if line.is_break)


@dataclass(frozen=True, slots=True)
class RealizedTieLine:
    """One sale -- an account, an instrument, a day -- ours against theirs.

    ``status`` says what the line is:

    - ``tied``: the gains agree within tolerance;
    - ``break``: they do not, or one side has a sale the other does not;
    - ``unreportable``: a lot the sale relieved carries `unavailable` basis
      (ADR 0017), so `portable` has no gain to compare. Shown, never a break
      -- the report already says this basis is not known;
    - ``portable_only``: a disposition the custodian's report does not
      carry and that realized nothing, such as a sweep redemption. Shown,
      not a break: a gain of zero is nothing to report.

    ``theirs_proceeds`` is ``None`` where the report states cost but not
    proceeds; the comparison is then on basis rather than gain, and the line
    says so through ``compared``.
    """

    account: str
    identifier: str
    disposed: date
    ours_proceeds: Decimal
    ours_basis: Decimal
    ours_gain: Decimal
    theirs_proceeds: Decimal | None
    theirs_basis: Decimal | None
    theirs_gain: Decimal | None
    #: ours less theirs, on whatever `compared` names.
    difference: Decimal | None
    compared: str
    status: str

    @property
    def is_break(self) -> bool:
        return self.status == "break"


@dataclass(frozen=True, slots=True)
class RealizedTieResult:
    lines: tuple[RealizedTieLine, ...]
    tolerance: Decimal

    @property
    def breaks(self) -> tuple[RealizedTieLine, ...]:
        return tuple(line for line in self.lines if line.is_break)


class ReconciliationService:
    """Compares held positions and cash against an external statement."""

    def __init__(self, repos: Repositories) -> None:
        self.repos = repos

    def reconcile(
        self,
        external: Sequence[ExternalHolding],
        accounts: Sequence[Account],
        *,
        tolerance: Decimal,
        cash_override: Mapping[str, Decimal] | None = None,
        on: date | None = None,
    ) -> ReconciliationResult:
        """Compare *external* against what *accounts* currently hold.

        Holdings are read from derived state as it stands, so a caller
        reconciling a past statement runs this inside
        :func:`~portable_core.services.replay.state_on` for its date, and
        passes that date as *on* so its tickers resolve as they stood then.

        Args:
            external: the statement's lines.
            accounts: the accounts to reconcile. Every external row must belong
                to one of them.
            tolerance: absolute, applied per line to both quantities and money.
            cash_override: the custodian's cash figure per account name, where
                it comes from a flag rather than from a line in the file.

        Raises:
            ValidationError: when a row cannot be attributed to an account, or
                when cash is stated twice for one account.
        """
        by_account = self._attribute(external, accounts)
        overrides = dict(cash_override or {})
        self._check_cash_not_stated_twice(by_account, overrides)

        lines: list[ReconciliationLine] = []
        for account in accounts:
            rows = by_account.get(account.name, [])
            lines.extend(self._positions(account, rows, tolerance, on))
            lines.append(self._cash(account, rows, overrides.get(account.name), tolerance))
        return ReconciliationResult(lines=tuple(lines), tolerance=tolerance)

    # ── prices, against the statement's market values (ADR 0031) ────────────

    def prices(
        self,
        external: Sequence[ExternalHolding],
        accounts: Sequence[Account],
        on: date,
        *,
        staleness_days: int,
    ) -> list[price_acceptance.PriceLine]:
        """Each priced statement line against portable's price for *on*.

        Lines for one instrument in one account are summed first, as the
        quantity comparison sums them. A line portable cannot place is left to
        the quantity comparison, which already reports it as a break.
        """
        lines: list[price_acceptance.PriceLine] = []
        for account, instrument, quantity, market_value in self._priced(external, accounts, on):
            lines.append(
                price_acceptance.check(
                    self.repos,
                    account,
                    instrument,
                    on,
                    quantity,
                    market_value,
                    staleness_days=staleness_days,
                    recorded=self.repos.statements.get(
                        account.account_id, on, instrument.instrument_id
                    ),
                )
            )
        return lines

    def record(
        self,
        external: Sequence[ExternalHolding],
        accounts: Sequence[Account],
        on: date,
        *,
        source: str,
    ) -> tuple[int, int]:
        """Store the statement's priced lines as anchors dated *on*.

        Returns how many lines were recorded and how many of them were new or
        changed.
        """
        recorded = changed = 0
        for account, instrument, quantity, market_value in self._priced(external, accounts, on):
            recorded += 1
            changed += price_acceptance.record(
                self.repos, account, instrument, on, quantity, market_value, source=source
            )
        return recorded, changed

    def _priced(
        self, external: Sequence[ExternalHolding], accounts: Sequence[Account], on: date
    ) -> list[tuple[Account, Instrument, Decimal, Decimal]]:
        by_account = self._attribute(external, accounts)
        found: list[tuple[Account, Instrument, Decimal, Decimal]] = []
        for account in accounts:
            totals: dict[int, tuple[Instrument, Decimal, Decimal]] = {}
            for row in by_account.get(account.name, []):
                if row.is_cash_equivalent or row.market_value is None:
                    continue
                instrument = self.repos.instruments.find(row.identifier, on=on)
                if instrument is None:
                    continue
                _, quantity, value = totals.get(
                    instrument.instrument_id, (instrument, ZERO, ZERO)
                )
                totals[instrument.instrument_id] = (
                    instrument,
                    quantity + row.amount,
                    value + row.market_value,
                )
            found.extend(
                (account, instrument, quantity, value)
                for instrument, quantity, value in sorted(
                    totals.values(), key=lambda t: t[0].symbol
                )
            )
        return found

    # ── realized gains, per sale ─────────────────────────────────────────────

    def tie_realized(
        self,
        closed_lots: Sequence[ClosedLotRecord],
        accounts: Sequence[Account],
        *,
        tolerance: Decimal,
    ) -> RealizedTieResult:
        """Tie every disposition to the custodian's own lot-level report.

        `docs/broker-import.md` §9, check 2, as a comparison per sale rather
        than per year: proceeds, basis and gain of what the ledger relieved
        against the report's lots for the same account, instrument and day.
        Per sale because a year that ties can hide two sales that are wrong
        by offsetting amounts, and because a break named by day is one a
        person can look up.

        Args:
            closed_lots: the report, as the adapter read it -- identifiers
                already resolved through the crosswalk.
            accounts: the accounts in scope. Report rows for other accounts
                are ignored, as `reconcile` ignores statement rows for them.
            tolerance: absolute, on the gain (or on basis where the report
                states no proceeds).
        """
        names = {account.name for account in accounts}
        theirs: dict[tuple[str, str, date], list[ClosedLotRecord]] = {}
        for lot in closed_lots:
            if lot.account in names:
                theirs.setdefault((lot.account, lot.identifier, lot.disposed), []).append(lot)

        ours: dict[tuple[str, str, date], list[LotDisposition]] = {}
        unavailable: set[tuple[str, str, date]] = set()
        for account in accounts:
            for disposition in self.repos.lots.dispositions(account_id=account.account_id):
                instrument = self.repos.instruments.get(disposition.instrument_id)
                if instrument is None:
                    continue
                key = (account.name, instrument.symbol, disposition.disposition_date)
                ours.setdefault(key, []).append(disposition)
                relieved = self.repos.lots.get(disposition.lot_id)
                if relieved is not None and relieved.basis_source is BasisSource.UNAVAILABLE:
                    unavailable.add(key)

        lines = [
            self._tie_line(
                key, ours.get(key, []), theirs.get(key), key in unavailable, tolerance
            )
            for key in sorted(set(ours) | set(theirs), key=lambda k: (k[0], k[2], k[1]))
        ]
        return RealizedTieResult(lines=tuple(lines), tolerance=tolerance)

    @staticmethod
    def _tie_line(
        key: tuple[str, str, date],
        mine: Sequence[LotDisposition],
        report: Sequence[ClosedLotRecord] | None,
        unavailable: bool,
        tolerance: Decimal,
    ) -> RealizedTieLine:
        account, identifier, disposed = key
        ours_proceeds = sum((d.proceeds for d in mine), ZERO)
        ours_basis = sum((d.cost_basis_relieved for d in mine), ZERO)
        ours_gain = sum((d.realized_gain for d in mine), ZERO)
        if report is None:
            status = "portable_only" if abs(ours_gain) <= tolerance else "break"
            return RealizedTieLine(
                account, identifier, disposed, ours_proceeds, ours_basis, ours_gain,
                None, None, None, None, "gain", status,
            )  # fmt: skip
        theirs_basis = sum((lot.cost_basis for lot in report), ZERO)
        stated_proceeds = [lot.proceeds for lot in report]
        theirs_proceeds: Decimal | None = None
        theirs_gain: Decimal | None = None
        if all(p is not None for p in stated_proceeds):
            proceeds_total = sum((p for p in stated_proceeds if p is not None), ZERO)
            theirs_proceeds = proceeds_total
            theirs_gain = proceeds_total - theirs_basis
        if not mine:
            return RealizedTieLine(
                account, identifier, disposed, ZERO, ZERO, ZERO,
                theirs_proceeds, theirs_basis, theirs_gain, None, "gain", "break",
            )  # fmt: skip
        if theirs_gain is not None:
            compared, difference = "gain", ours_gain - theirs_gain
        else:
            compared, difference = "basis", ours_basis - theirs_basis
        if unavailable:
            status = "unreportable"
        else:
            status = "tied" if abs(difference) <= tolerance else "break"
        return RealizedTieLine(
            account, identifier, disposed, ours_proceeds, ours_basis, ours_gain,
            theirs_proceeds, theirs_basis, theirs_gain, difference, compared, status,
        )  # fmt: skip

    # ── attribution ──────────────────────────────────────────────────────────

    def _attribute(
        self, external: Sequence[ExternalHolding], accounts: Sequence[Account]
    ) -> dict[str, list[ExternalHolding]]:
        """Assign every external row to an account, or refuse.

        A row with no account is only unambiguous when exactly one account is
        being reconciled. With more than one, guessing would put a holding in
        the wrong account and still balance the total -- which is precisely the
        failure per-account scoping exists to prevent.
        """
        known = {account.name: account for account in accounts}
        grouped: dict[str, list[ExternalHolding]] = {name: [] for name in known}

        for row in external:
            if row.account is None:
                if len(accounts) != 1:
                    raise ValidationError(
                        "the statement does not say which account each line belongs to, "
                        f"and {len(accounts)} accounts are being reconciled",
                        code=E_USAGE,
                        remedy=(
                            "Reconcile one account at a time with `--account`, or add an "
                            "`account` column to the file. Attributing a holding to the "
                            "wrong account can leave the portfolio total balancing while "
                            "both accounts are wrong."
                        ),
                        identifier=row.identifier,
                        accounts=sorted(known),
                    )
                grouped[accounts[0].name].append(row)
                continue

            if row.account not in known:
                raise ValidationError(
                    f"the statement names account {row.account!r}, which is not being "
                    "reconciled",
                    code=E_USAGE,
                    remedy=(
                        "Name it with `--account`, or remove its lines from the file. "
                        "Silently ignoring them would report a clean reconciliation "
                        "against a statement that was only partly read."
                    ),
                    account=row.account,
                    known=sorted(known),
                )
            grouped[row.account].append(row)
        return grouped

    @staticmethod
    def _check_cash_not_stated_twice(
        by_account: Mapping[str, Sequence[ExternalHolding]],
        overrides: Mapping[str, Decimal],
    ) -> None:
        for name, amount in overrides.items():
            if any(row.is_cash_equivalent for row in by_account.get(name, [])):
                raise ValidationError(
                    f"cash for {name} is stated both by `--cash` and by a line in the "
                    "statement",
                    code=E_USAGE,
                    remedy=(
                        "Use one or the other. Adding them would double the balance; "
                        "picking one silently would hide whichever is wrong."
                    ),
                    account=name,
                    flag_amount=str(amount),
                )

    # ── the two comparisons ──────────────────────────────────────────────────

    def _positions(
        self,
        account: Account,
        rows: Sequence[ExternalHolding],
        tolerance: Decimal,
        on: date | None = None,
    ) -> list[ReconciliationLine]:
        ours = self._held(account)
        theirs: dict[str, Decimal] = {}
        for row in rows:
            if row.is_cash_equivalent:
                continue
            # Resolution goes through the same path a trade uses, so a CUSIP or
            # an ISIN on the statement finds the instrument a ticker would.
            found = self.repos.instruments.find(row.identifier, on=on)
            key = found.symbol if found is not None else row.identifier.upper()
            theirs[key] = theirs.get(key, ZERO) + row.amount

        return [
            self._line(
                account.name,
                "position",
                key,
                ours.get(key, ZERO),
                theirs.get(key, ZERO),
                tolerance,
            )
            for key in sorted(set(ours) | set(theirs))
        ]

    def _held(self, account: Account) -> dict[str, Decimal]:
        """What this account holds now, by symbol.

        A closed leg carries a zero quantity, so open positions can be summed
        whole without filtering legs.
        """
        held: dict[str, Decimal] = {}
        for position in self.repos.positions.all(account_id=account.account_id, open_only=True):
            for leg in position.legs:
                instrument = self.repos.instruments.get(leg.instrument_id)
                if instrument is None:  # pragma: no cover -- foreign key prevents it
                    continue
                held[instrument.symbol] = held.get(instrument.symbol, ZERO) + leg.quantity
        return {symbol: quantity for symbol, quantity in held.items() if quantity != 0}

    def _cash(
        self,
        account: Account,
        rows: Sequence[ExternalHolding],
        override: Decimal | None,
        tolerance: Decimal,
    ) -> ReconciliationLine:
        """One cash line per account, always -- including when it is zero.

        Rendered even where the statement said nothing about cash, because a
        missing cash line is the difference between "they agree" and "nobody
        checked", and those must not look the same.
        """
        balance, margin_loan = self.repos.valuations.cash(
            account.account_id, currency=account.currency
        )
        # Net of any margin loan, which is how a statement presents it and how
        # `valuation_snapshot` composes market value.
        ours = balance - margin_loan
        theirs = (
            override
            if override is not None
            else sum((row.amount for row in rows if row.is_cash_equivalent), ZERO)
        )
        return self._line(account.name, "cash", "CASH", ours, theirs, tolerance)

    @staticmethod
    def _line(
        account: str,
        kind: str,
        identifier: str,
        ours: Decimal,
        theirs: Decimal,
        tolerance: Decimal,
    ) -> ReconciliationLine:
        difference = ours - theirs
        return ReconciliationLine(
            account=account,
            kind=kind,
            identifier=identifier,
            ours=ours,
            theirs=theirs,
            difference=difference,
            is_break=abs(difference) > tolerance,
        )
