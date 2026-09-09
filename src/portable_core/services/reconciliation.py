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
from decimal import Decimal

from portable_core.domain.models import Account
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_USAGE
from portable_core.persistence.repositories import Repositories

__all__ = [
    "ExternalHolding",
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
    ) -> ReconciliationResult:
        """Compare *external* against what *accounts* currently hold.

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
            lines.extend(self._positions(account, rows, tolerance))
            lines.append(self._cash(account, rows, overrides.get(account.name), tolerance))
        return ReconciliationResult(lines=tuple(lines), tolerance=tolerance)

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
    ) -> list[ReconciliationLine]:
        ours = self._held(account)
        theirs: dict[str, Decimal] = {}
        for row in rows:
            if row.is_cash_equivalent:
                continue
            # Resolution goes through the same path a trade uses, so a CUSIP or
            # an ISIN on the statement finds the instrument a ticker would.
            found = self.repos.instruments.find(row.identifier)
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
