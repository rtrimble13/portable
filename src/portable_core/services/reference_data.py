"""Reference data a person corrects: an account's description, an instrument's.

`pt account update` and `pt instrument update` (issue 38). Neither touches the
ledger, and neither changes a figure already derived. Each writes only columns
that nothing computing a figure reads. The other columns stay as they were
written, for a reason this module states when asked to change one:

- **An account's name** is part of every imported row's identity (ADR 0033).
  Renamed, the next incremental import would not recognise the rows the
  custodian already sent, and would append them a second time. Nothing would
  refuse it.
- **An account's default relief method** relieves every sale that was recorded
  without a method, each time replay runs. Changing it would re-relieve those
  sales and change gains already reported.
- **An account's cash treatment** is read by valuation for every snapshot
  (`PORT-GIPS-A07`). **Whether it allows fractional shares** is read by replay
  at every spinoff and conversion. Neither has an effective date that could
  confine a change to later days.

An instrument's identifiers can be corrected, because `pt instrument sync`
lists their differences from the security master for a person to act on. They
are also how a statement or an import finds the instrument (`InstrumentRepository
.resolve` falls back to CUSIP, ISIN and FIGI), so one already held by another
instrument is refused: resolution would become ambiguous.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final, NoReturn

from portable_core.domain.models import Account, Instrument
from portable_core.errors import UsageError, ValidationError
from portable_core.errors.kinds import E_RESTATES_HISTORY
from portable_core.persistence.repositories import Repositories

__all__ = [
    "ACCOUNT_FIELDS",
    "INSTRUMENT_FIELDS",
    "refuse_account_change",
    "refuse_free_text_class",
    "update_account",
    "update_instrument",
]

#: What `pt account update` writes. Description, read by nothing that computes.
ACCOUNT_FIELDS: Final = ("custodian", "account_alias", "note")

#: What `pt instrument update` writes. The symbol is changed by
#: `pt instrument correct-symbol` or a rename (ADR 0034), never here, and the
#: type not at all: it decides how the instrument is valued.
INSTRUMENT_FIELDS: Final = (
    "name",
    "exchange",
    "cusip",
    "isin",
    "figi",
    "sector",
    "industry",
    "country",
)

_IDENTIFIERS: Final = ("cusip", "isin", "figi")

#: Each account column `pt account update` refuses, with what it would break.
_ACCOUNT_REFUSALS: Final[dict[str, tuple[str | None, str, str]]] = {
    "name": (
        None,
        "an account's name is part of the identity of every row imported into it "
        "(ADR 0033): renamed, the next `pt import broker --incremental` would not "
        "recognise the rows the custodian already sent, and would append them again",
        "Keep the name. The alias (`--alias`) and the note are free to change.",
    ),
    "default_relief_method": (
        E_RESTATES_HISTORY,
        "replay relieves every sale recorded without a method by the account's "
        "default, so changing it would re-relieve those sales and change gains "
        "already reported",
        "Name the method on the sale instead: `--method` on the trade, or "
        "`pt trade correct TXN --method` for one already recorded.",
    ),
    "cash_treatment": (
        E_RESTATES_HISTORY,
        "valuation reads it for every snapshot of the account, so changing it would "
        "change every return already measured (PORT-GIPS-A07)",
        "It needs an effective date, as a tax rate has (ADR 0025), and has none yet.",
    ),
    "allows_fractional": (
        E_RESTATES_HISTORY,
        "replay reads it at every spinoff and conversion, where it decides whether a "
        "fraction is held or paid out as cash in lieu, so changing it would restate "
        "those lots",
        "It needs an effective date, as a tax rate has (ADR 0025), and has none yet.",
    ),
}


def refuse_free_text_class(symbol: str) -> NoReturn:
    """Raise for `--asset-class` on `pt instrument add` or `update` (ADR 0048).

    A class is declared, effective-dated, with weights, by `pt allocation
    classify`, and drift and `pt holdings --by asset-class` read nothing else.
    The free-text column is kept for files written before schema 13, whose
    migration seeded the classification from it, and nothing writes it now:
    two places to say what a holding is would be two answers.
    """
    raise UsageError(
        f"{symbol}'s asset class is not set here",
        remedy=(
            f"pt allocation classify {symbol} CLASS --effective-from DATE, or "
            "CLASS=WEIGHT ... for a split (ADR 0048). `pt allocation class list` "
            "shows the classes."
        ),
        symbol=symbol,
    )


def refuse_account_change(account: Account, field: str) -> None:
    """Raise for a column `pt account update` will not write, saying why."""
    code, why, remedy = _ACCOUNT_REFUSALS[field]
    message = f"{field} of account {account.name!r} is not changed here: {why}"
    if code is None:
        raise ValidationError(message, remedy=remedy, field=field)
    raise ValidationError(message, code=code, remedy=remedy, field=field)


def update_account(
    repos: Repositories, account: Account, changes: Mapping[str, str | None]
) -> dict[str, dict[str, str | None]]:
    """Write *changes* to *account*'s descriptive columns. Returns what changed.

    A value of ``""`` clears the column, so an empty value is stored as null
    and never as an empty string that would read differently from one.
    """
    return _update(
        current={field: getattr(account, field) for field in ACCOUNT_FIELDS},
        changes=changes,
        allowed=ACCOUNT_FIELDS,
        write=lambda fields: repos.accounts.update(account.account_id, **fields),
    )


def update_instrument(
    repos: Repositories, instrument: Instrument, changes: Mapping[str, str | None]
) -> dict[str, dict[str, str | None]]:
    """Write *changes* to *instrument*'s descriptive columns and identifiers."""
    for field in _IDENTIFIERS:
        value = _cleared(changes.get(field))
        if value is None:
            continue
        holder = repos.instruments.holding_identifier(field, value)
        if holder is not None and holder.instrument_id != instrument.instrument_id:
            raise ValidationError(
                f"{field} {value} already identifies {holder.symbol}",
                remedy=(
                    "One identifier names one instrument, or a statement or an import "
                    f"that names it could resolve to either. Correct {holder.symbol}'s "
                    "first, if it is the wrong one."
                ),
                field=field,
                held_by=holder.symbol,
            )
    return _update(
        current={field: getattr(instrument, field) for field in INSTRUMENT_FIELDS},
        changes=changes,
        allowed=INSTRUMENT_FIELDS,
        write=lambda fields: repos.instruments.update(instrument.instrument_id, **fields),
    )


def _cleared(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _update(
    *,
    current: Mapping[str, str | None],
    changes: Mapping[str, str | None],
    allowed: tuple[str, ...],
    write: Callable[[dict[str, str | None]], None],
) -> dict[str, dict[str, str | None]]:
    unknown = sorted(set(changes) - set(allowed))
    if unknown:  # pragma: no cover -- the commands pass only allowed fields
        raise ValidationError(f"not an updatable field: {', '.join(unknown)}")
    given = {field: _cleared(value) for field, value in changes.items() if value is not None}
    if not given:
        raise ValidationError(
            "nothing to update",
            remedy="Name at least one field to change. An empty value clears it.",
        )
    changed = {
        field: {"was": current[field], "now": value}
        for field, value in sorted(given.items())
        if current[field] != value
    }
    if changed:
        write({field: entry["now"] for field, entry in changed.items()})
    return changed
