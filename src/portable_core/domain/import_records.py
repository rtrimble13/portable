"""The canonical records every adapter emits, and nothing downstream looks past.

They live in `domain` rather than in `importers` for the reason ADR 0018 §5
gives them at all: the reconstruction, the batch builder and the reconciler
operate on these and have no knowledge of adapters. A service reaching into
`importers` for its input type would put the layering the wrong way round and
make the adapter, rather than the record, the thing everything depends on.
These are what `domain` is for -- frozen dataclasses, no I/O, no business rules.

ADR 0018 §5. The cutover reconstruction, the batch builder, the reconciler and
every refusal operate on these two types and have **no knowledge of
spreadsheets, custodians, or column names**. That is what makes the
reconstruction of ADR 0017 a general procedure rather than a description of one
adviser's data.

Two conventions are fixed here because leaving them to each adapter is how a
sign error gets in:

- ``TransactionRecord.amount`` is the **cash effect on the account**: positive
  is money in, negative is money out. Custodians differ wildly -- some sign the
  column, some state a magnitude and put the direction in the activity string,
  some use parentheses -- so the activity map declares the convention and the
  adapter normalises to this one. Note what this is *not*: it is what the
  statement says the cash did, not what `portable` concludes follows from the
  event. Deriving the consequences stays with the services (ADR 0012).
- ``TransactionRecord.quantity`` is the **signed change in units**: positive is
  units in, negative is units out. ``None`` where the event has no units, which
  is not the same as zero.

``activity`` stays the custodian's own string, unmapped. Mapping it is the
activity map's job, done once, in a file a person can review.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from portable_core.domain.enums import FeeClass, TransactionType

__all__ = ["HoldingRecord", "TransactionRecord"]


@dataclass(frozen=True, slots=True)
class HoldingRecord:
    """One line of a custodian's position statement.

    The optional fields are the capability-gated ones (ADR 0018 §2). Each is
    ``None`` rather than a zero or an empty string when the custodian did not
    supply it, so that "the custodian says the basis is nothing" and "the
    custodian did not say" cannot be confused -- the distinction decides
    whether a seeded lot is ``basis_source = 'derived'`` or ``'unavailable'``.
    """

    as_of: date
    account: str
    #: Symbol, CUSIP, ISIN, or a name to be crosswalked. Resolution is the
    #: importer's job; the adapter reports what the file said.
    identifier: str
    quantity: Decimal
    #: ADR 0013, generalised: membership of the account's declared
    #: cash-equivalent set, decided by `source.toml`, not by guessing at a
    #: ticker. A sweep vehicle is cash and must fold into the cash balance
    #: before reconciliation, or every account reconciles short by its sweep.
    is_cash_equivalent: bool
    market_value: Decimal | None = None
    #: ``ImportCapability.COST_BASIS``.
    cost_basis: Decimal | None = None
    #: ``ImportCapability.ACQUISITION_DATE``.
    acquired: date | None = None
    #: ``ImportCapability.LOT_DETAIL``.
    lot_id: str | None = None


@dataclass(frozen=True, slots=True)
class TransactionRecord:
    """One line of a custodian's activity export.

    ``source_row`` is kept verbatim so the batch file can carry it. A reviewer
    approving a batch is approving a mapping *from* something, and the something
    has to be visible next to the row it produced.
    """

    trade_date: date
    account: str
    #: The custodian's own activity string, unmapped and untranslated.
    activity: str
    identifier: str | None
    #: Signed change in units; ``None`` where the event has none.
    quantity: Decimal | None
    #: Signed cash effect: positive in, negative out.
    amount: Decimal
    source_row: Mapping[str, str]
    #: ``ImportCapability.TRANSACTION_ID``. Absent means ADR 0012 synthesises
    #: an ``external_ref`` from row content instead.
    external_id: str | None = None
    #: ``ImportCapability.SETTLEMENT_DATE``. Recorded, never used for
    #: recognition -- `portable` is trade-date accounting (invariant 7).
    settlement_date: date | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class MappedTransaction:
    """A custodian row with its activity resolved, and nothing else decided.

    The seam between the adapter and everything downstream. The adapter owns
    the activity map and so it owns this resolution; the batch builder owns
    what becomes a ledger row and must not need to know what a custodian calls
    things. Putting the resolved type here rather than letting the builder read
    the map is what keeps `services` from depending on `importers` — the
    dependency runs the other way, and a service reaching into an adapter for
    its input type would make the adapter the thing everything depends on.

    ``txn_type`` is ``None`` for a row the map deliberately keeps out of the
    ledger; ``reason`` then says why, and a skip without one is refused when
    the map loads.
    """

    record: TransactionRecord
    rule: str
    txn_type: TransactionType | None = None
    fee_class: FeeClass | None = None
    reason: str | None = None

    @property
    def is_skipped(self) -> bool:
        return self.txn_type is None
