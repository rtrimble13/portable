"""Corrections to the local security master.

The security master is reference data, not ledger: an instrument's symbol is
how a price, a statement line and a crosswalk find it, and nothing in the
ledger refers to it by symbol. So a symbol recorded in error is corrected in
place, with the correction written to the change log -- unlike a rename, which
is an event in the security's life and is recorded as one (`pt ca
symbol-change`). ADR 0034.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from portable_core.domain.models import Instrument
from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_INSTRUMENT_AMBIGUOUS
from portable_core.persistence.repositories import Repositories

__all__ = ["SymbolCorrection", "correct_symbol"]


@dataclass(frozen=True, slots=True)
class SymbolCorrection:
    """What a correction changed: one instrument's symbol, over these periods."""

    instrument_id: int
    old: str
    new: str
    #: ``(valid_from, valid_to)`` of each corrected period; ``valid_to`` is
    #: inclusive and ``None`` while current.
    periods: tuple[tuple[date, date | None], ...]


def correct_symbol(
    repos: Repositories,
    instrument: Instrument,
    new_symbol: str,
    *,
    old_symbol: str,
    reason: str,
) -> SymbolCorrection:
    """Say that every period in which *instrument* carried *old_symbol* carried *new_symbol*.

    ADR 0034. Refused rather than guessed where the correction could make
    something wrong:

    - **no reason**: the change log is the record of why the master changed,
      and a correction nobody can explain later is the one that gets undone;
    - **another instrument carries the new symbol over an overlapping
      period**: resolution by that symbol would then answer with two
      instruments, and a quantity would be put against whichever came first;
    - **the instrument has stored prices**: each reached it by the symbol
      being corrected -- a provider asked under that ticker, or a file naming
      it -- so each may be another security's price. Correct first, then
      price.

    Raises:
        ValidationError: as above, or when *old_symbol* is not a symbol the
            instrument has carried, or the two symbols are the same.
    """
    old = old_symbol.strip().upper()
    new = new_symbol.strip().upper()
    if not reason.strip():
        raise ValidationError(
            "a symbol correction needs a --reason",
            remedy=(
                "Say what shows the symbol was wrong -- the custodian's statement "
                "naming the other ticker, say -- so the change log can answer why."
            ),
        )
    if not new or new == old:
        raise ValidationError(
            f"{old} would be corrected to itself",
            remedy="Name the symbol the instrument actually carried with --to.",
        )

    periods = tuple(
        (valid_from, valid_to)
        for symbol, valid_from, valid_to in repos.instruments.symbol_history(
            instrument.instrument_id
        )
        if symbol == old
    )
    if not periods:
        raise ValidationError(
            f"{instrument.symbol} (instrument {instrument.instrument_id}) has never "
            f"carried the symbol {old}",
            remedy="Name the instrument by the symbol that is wrong.",
            instrument_id=instrument.instrument_id,
        )

    clashes = sorted(
        {
            other
            for other, valid_from, valid_to in repos.instruments.periods_named(new)
            if other != instrument.instrument_id
            and any(_overlap((valid_from, valid_to), period) for period in periods)
        }
    )
    if clashes:
        raise ValidationError(
            f"{new} already names instrument(s) {clashes} over a period {old} is "
            f"corrected for; {new} would then resolve to two instruments",
            code=E_INSTRUMENT_AMBIGUOUS,
            remedy=(
                "Two instruments for one security is a duplicate in the master, which "
                "a symbol correction cannot merge. Settle which instrument is the "
                "security before correcting either."
            ),
            matches=[instrument.instrument_id, *clashes],
        )

    count, first, last = repos.prices.span(instrument.instrument_id)
    if count:
        assert first is not None and last is not None
        raise ValidationError(
            f"{old} has {count} stored price(s), {first.isoformat()} to "
            f"{last.isoformat()}; each was found under the symbol being corrected and "
            f"may be another security's",
            remedy=(
                "Correct a symbol before pricing the instrument. Prices already "
                "stored cannot yet be removed by any command (ADR 0034)."
            ),
            symbol=old,
            prices=count,
        )

    repos.instruments.correct_symbol(instrument.instrument_id, old, new)
    repos.meta.record_change(
        f"instrument:{instrument.instrument_id}:symbol",
        old,
        new,
        min(valid_from for valid_from, _ in periods),
        reason.strip(),
    )
    return SymbolCorrection(
        instrument_id=instrument.instrument_id, old=old, new=new, periods=periods
    )


def _overlap(a: tuple[date, date | None], b: tuple[date, date | None]) -> bool:
    """Two inclusive periods share a day; ``None`` is open-ended."""
    a_end = a[1] or date.max
    b_end = b[1] or date.max
    return a[0] <= b_end and b[0] <= a_end
