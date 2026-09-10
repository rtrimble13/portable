"""Pairing the two legs of one internal transfer. ADR 0014, generalised by ADR 0018.

A custodian that reports both sides of a journal has reported one event twice:
once in the paying account, once in the receiving one. `portable` records a
transfer **once**, as a single row with a counter account (ADR 0007), because
recorded twice it is two external flows at portfolio level and the track
record is rewritten with money that never left.

So the adapter pairs them. Two rows under a pairing rule, on the same trade
date, for the same magnitude, in opposite directions and different accounts,
are one movement: the outbound leg becomes the ``transfer`` and the inbound leg
is carried as a skipped row naming it, so the batch shows both and the ledger
gets one. Where the note names the other account, only that account will do --
two IRAs funded with the same amount on the same day are otherwise
indistinguishable, and a guess is what this module refuses.

A leg with no counterpart is either a movement to an account outside the
portfolio -- a withdrawal or a deposit, which the rule may declare as the
fallback -- or a hole in the history. The two are told apart by the note: a leg
naming an account that *is* in the export but has no matching row is a hole,
and refuses, whatever fallback the rule declares.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from portable_core.domain.enums import TransactionType
from portable_core.domain.import_records import MappedTransaction, TransactionRecord
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_IMPORT_SOURCE_INVALID
from portable_core.importers.activity import ActivityRule, PairSpec

__all__ = ["Leg", "pair_legs"]

ZERO: Final = Decimal("0")


@dataclass(frozen=True, slots=True)
class Leg:
    """One row under a pairing rule, before it is matched."""

    index: int
    record: TransactionRecord
    rule: ActivityRule

    @property
    def spec(self) -> PairSpec:
        assert self.rule.pair is not None
        return self.rule.pair

    @property
    def outbound(self) -> bool:
        return self.record.amount < ZERO

    @property
    def magnitude(self) -> Decimal:
        return abs(self.record.amount)

    @property
    def named(self) -> str | None:
        return self.spec.named_counterpart(self.record.note)


def pair_legs(legs: Sequence[Leg], accounts: frozenset[str]) -> dict[int, MappedTransaction]:
    """Resolve every leg to what it becomes, keyed by row index.

    ``accounts`` is every account the export mentions. A named counterpart in
    that set with no matching leg is a hole in the history and refuses; one
    outside it is a movement across the portfolio boundary and takes the
    rule's declared fallback.
    """
    known = {_key(a) for a in accounts}
    for leg in legs:
        if leg.record.amount == ZERO:
            raise _refuse(
                f"row {leg.index}: {leg.rule.match!r} moves no cash, so it cannot be "
                f"a leg of a transfer. Check the `cash` convention on the rule",
                row=leg.index,
            )

    outbound = [leg for leg in legs if leg.outbound]
    inbound = [leg for leg in legs if not leg.outbound]
    unmatched = {leg.index: leg for leg in inbound}
    resolved: dict[int, MappedTransaction] = {}

    for out in outbound:
        candidates = [
            leg
            for leg in unmatched.values()
            if leg.record.trade_date == out.record.trade_date
            and leg.magnitude == out.magnitude
            and _key(leg.record.account) != _key(out.record.account)
            and (out.named is None or _key(out.named) == _key(leg.record.account))
            and (leg.named is None or _key(leg.named) == _key(out.record.account))
        ]
        if len(candidates) > 1:
            raise _refuse(
                f"row {out.index}: {out.rule.match!r} out of {out.record.account} for "
                f"{out.magnitude} on {out.record.trade_date.isoformat()} could pair with "
                f"rows {', '.join(str(c.index) for c in candidates)} ("
                + ", ".join(c.record.account for c in candidates)
                + "). Declare a `counterpart` pattern on the rule so the note "
                "decides, or the import would have to guess which account received it",
                row=out.index,
                candidates=[c.index for c in candidates],
            )
        if len(candidates) == 1:
            into = candidates.pop()
            del unmatched[into.index]
            resolved[out.index] = MappedTransaction(
                record=out.record,
                rule=f"{out.rule.label} (paired with row {into.index})",
                txn_type=TransactionType.TRANSFER,
                counter_account=into.record.account,
            )
            resolved[into.index] = MappedTransaction(
                record=into.record,
                rule=f"{into.rule.label} (paired with row {out.index})",
                reason=(
                    f"the receiving leg of the transfer on row {out.index}; recorded "
                    f"once, from {out.record.account} (ADR 0014)"
                ),
            )
            continue
        resolved[out.index] = _unpaired(out, known, out.spec.unpaired_out, "receiving")

    for into in unmatched.values():
        resolved[into.index] = _unpaired(into, known, into.spec.unpaired_in, "paying")

    return resolved


def _unpaired(
    leg: Leg, known: set[str], fallback: TransactionType | None, other: str
) -> MappedTransaction:
    """What a leg with no counterpart becomes, or the refusal saying why not."""
    named = leg.named
    where = (
        f"row {leg.index}: {leg.rule.match!r} {'out of' if leg.outbound else 'into'} "
        f"{leg.record.account} for {leg.magnitude} on {leg.record.trade_date.isoformat()}"
    )
    if named is not None and _key(named) in known:
        raise _refuse(
            f"{where} names {named!r} as the {other} account, which is in this export, "
            f"but no matching leg was found there on that date for that amount. The "
            f"history is missing a row, or the amounts differ; nothing here guesses "
            f"which",
            row=leg.index,
            counterpart=named,
        )
    if fallback is None:
        raise _refuse(
            f"{where} has no counterpart"
            + (f" ({named!r} is not an account in this export)" if named else "")
            + f". Declare `unpaired_{'out' if leg.outbound else 'in'}` on the rule if a "
            f"leg with no counterpart is a movement across the portfolio boundary, "
            f"or add the missing leg to the history",
            row=leg.index,
            counterpart=named,
        )
    return MappedTransaction(
        record=leg.record,
        rule=f"{leg.rule.label} (unpaired → {fallback.value})",
        txn_type=fallback,
    )


def _key(text: str) -> str:
    return " ".join(text.split()).casefold()


def _refuse(message: str, **context: Any) -> ValidationError:
    return ValidationError(message, code=E_IMPORT_SOURCE_INVALID, **context)
