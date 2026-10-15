"""Recording a change of intent: one leg into another position (ADR 0023).

A position is the unit of trader intent (ADR 0009). Regrouping used to edit
derived state directly, so the next rebuild -- any back-dated import, any
reversal -- undid it. It is now a ``position_regroup`` row in the ledger,
which replay applies like any other, so a grouping is as durable as a trade.

This module writes the row and nothing else; what the row *does* is
:meth:`ReplayEngine._regroup`, the one place it is derived.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal

from portable_core.domain.enums import (
    PositionStatus,
    StrategyType,
    TransactionSource,
    TransactionType,
)
from portable_core.domain.models import Transaction
from portable_core.errors import ValidationError
from portable_core.persistence.repositories import Repositories
from portable_core.services.replay import ReplayEngine, state_on

__all__ = ["Regroup", "RegroupResult", "record_regroup"]


@dataclass(frozen=True, slots=True)
class Regroup:
    """Move *account_id*'s leg in *instrument_id*, on *on*."""

    account_id: int
    instrument_id: int
    on: date
    #: The transaction that opened the target position, or ``None`` for a new
    #: position of its own. Never a ``position_id``: a rebuild renumbers those.
    into_opened_by: int | None
    strategy: StrategyType | None = None
    note: str | None = None
    external_ref: str | None = None


@dataclass(frozen=True, slots=True)
class RegroupResult:
    transaction: Transaction
    #: True when the row was back-dated and derived state was rebuilt
    #: (ADR 0016).
    rebuilt: bool


def record_regroup(repos: Repositories, regroup: Regroup) -> RegroupResult:
    """Append the row and derive its effect. The caller holds the transaction.

    Checked against the state **as of its own date** (ADR 0021) before it is
    written: a regroup dated before the leg existed, or into a position not
    yet opened or already closed, would be a ledger row replay cannot apply --
    which `pt validate` would then report for ever, because the ledger cannot
    be edited.

    Raises:
        ValidationError: nothing to move, or nowhere to move it, on that date.
    """
    problem = state_on(repos, regroup.on, lambda: _problem(repos, regroup)).results[0]
    if problem is not None:
        raise ValidationError(
            problem,
            remedy="Check the date: a grouping applies to what was held on it.",
            date=regroup.on.isoformat(),
        )

    row = Transaction(
        txn_id=0,
        account_id=regroup.account_id,
        trade_date=regroup.on,
        seq=repos.transactions.next_seq(regroup.on),
        txn_type=TransactionType.POSITION_REGROUP,
        net_cash_effect=Decimal("0.00"),
        instrument_id=regroup.instrument_id,
        related_txn_id=regroup.into_opened_by,
        strategy_type=regroup.strategy,
        note=regroup.note,
        external_ref=regroup.external_ref,
        source=TransactionSource.MANUAL,
        created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    txn_id = repos.transactions.append(row)
    stored = replace(row, txn_id=txn_id)
    rebuilt = ReplayEngine(repos).apply_or_rebuild(stored)
    return RegroupResult(transaction=stored, rebuilt=rebuilt)


def _problem(repos: Repositories, regroup: Regroup) -> str | None:
    on = regroup.on.isoformat()
    if repos.positions.leg_for(regroup.account_id, regroup.instrument_id) is None:
        instrument = repos.instruments.get(regroup.instrument_id)
        symbol = instrument.symbol if instrument else str(regroup.instrument_id)
        return f"the account held no {symbol} on {on}, so there is no leg to move"
    if regroup.into_opened_by is not None:
        target = repos.positions.opened_by(regroup.into_opened_by)
        if target is None or target.status is not PositionStatus.OPEN:
            return f"the position to move into was not open on {on}"
        if target.account_id != regroup.account_id:
            return "a position holds legs in one account only"
    return None
