"""Naming a position: a label recorded in the ledger (ADR 0027).

`position.label` is derived state (ADR 0010), so a label written there would be
gone after the next rebuild, as a grouping was before ADR 0023. A label is a
``position_label`` row in the ledger instead, naming its position by the
transaction that opened it, which a rebuild does not renumber. Replay applies
it (:meth:`ReplayEngine._label`); this module writes the row and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal

from portable_core.domain.enums import TransactionSource, TransactionType
from portable_core.domain.models import Transaction
from portable_core.errors import ValidationError
from portable_core.persistence.repositories import Repositories
from portable_core.services.replay import ReplayEngine, state_on

__all__ = ["Label", "LabelResult", "record_label"]


@dataclass(frozen=True, slots=True)
class Label:
    """Give the position *opened_by* opened the name *text*, from *on*."""

    #: The transaction that opened the position. Never a ``position_id``.
    opened_by: int
    on: date
    #: ``None`` clears the label.
    text: str | None
    note: str | None = None
    external_ref: str | None = None


@dataclass(frozen=True, slots=True)
class LabelResult:
    transaction: Transaction
    #: True when the row was back-dated and derived state was rebuilt
    #: (ADR 0016).
    rebuilt: bool


def record_label(repos: Repositories, label: Label) -> LabelResult:
    """Append the row and derive its effect. The caller holds the transaction.

    Checked against the state **as of its own date** (ADR 0021): a label dated
    before its position was opened is a ledger row replay cannot apply, and
    `pt validate` would report it for ever, because the ledger is not edited.

    Raises:
        ValidationError: an empty label, or no such position on that date.
    """
    text = None if label.text is None else label.text.strip()
    if text == "":
        raise ValidationError(
            "a label cannot be empty",
            remedy="Pass the text to name the position, or --clear to remove its label.",
        )

    found = state_on(repos, label.on, lambda: _account_of(repos, label.opened_by))
    account_id = found.results[0]
    if account_id is None:
        raise ValidationError(
            f"no position opened by transaction {label.opened_by} existed on "
            f"{label.on.isoformat()}",
            remedy="Check the date: a label names a position that exists on it.",
            date=label.on.isoformat(),
        )

    row = Transaction(
        txn_id=0,
        account_id=account_id,
        trade_date=label.on,
        seq=repos.transactions.next_seq(label.on),
        txn_type=TransactionType.POSITION_LABEL,
        net_cash_effect=Decimal("0.00"),
        related_txn_id=label.opened_by,
        label=text,
        note=label.note,
        external_ref=label.external_ref,
        source=TransactionSource.MANUAL,
        created_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    txn_id = repos.transactions.append(row)
    stored = replace(row, txn_id=txn_id)
    rebuilt = ReplayEngine(repos).apply_or_rebuild(stored)
    return LabelResult(transaction=stored, rebuilt=rebuilt)


def _account_of(repos: Repositories, opened_by: int) -> int | None:
    position = repos.positions.opened_by(opened_by)
    return None if position is None else position.account_id
