"""`instruments.toml` -- the name-to-symbol crosswalk. ADR 0012, ADR 0018 §2.

Most custodians put a security *description* on a transaction row and a symbol
nowhere. ``INSTRUMENT_SYMBOL`` is then withheld, and ADR 0018 says what that
costs: *a name-to-symbol crosswalk is required and any unmapped name is a
refusal*. This module is that crosswalk.

It is data, reviewed as data, for the same reason the activity map is. A name
that resolves to the wrong symbol is a position in the wrong instrument, which
reconciles against nothing and is caught -- but a name that resolves to a
*plausible* wrong symbol, one share class for another, reconciles at the
quantity level and is wrong at every other. So there is no fuzzy matching, no
prefix matching, and no default: a name is in the file or the import stops.

Matching folds case and collapses whitespace, as the activity map does, and
nothing cleverer. Two spellings of one security are two entries.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from portable_core.errors import ValidationError
from portable_core.errors.kinds import E_IMPORT_SOURCE_INVALID, E_INSTRUMENT_UNMAPPED

__all__ = ["Crosswalk", "CrosswalkEntry", "load_crosswalk"]


@dataclass(frozen=True, slots=True)
class CrosswalkEntry:
    """One name the custodian writes, and the symbol it means."""

    name: str
    symbol: str
    note: str | None = None


@dataclass(frozen=True, slots=True)
class Crosswalk:
    """Every name the custodian writes for a security, and no others."""

    entries: tuple[CrosswalkEntry, ...]
    path: Path | None = None

    def resolve(self, name: str, *, row: int | None = None) -> str:
        """The symbol for one name, or a refusal naming the row and the name."""
        key = _key(name)
        for entry in self.entries:
            if _key(entry.name) == key:
                return entry.symbol
        where = f" (row {row})" if row is not None else ""
        raise ValidationError(
            f"no symbol for instrument name {name!r}{where}. Add it to "
            f"{self.path.name if self.path else 'the crosswalk'}, or the import "
            f"would have to guess which security this is",
            code=E_INSTRUMENT_UNMAPPED,
            name=name,
            row=row,
            mapped=sorted(entry.name for entry in self.entries),
            path=str(self.path) if self.path else None,
        )

    def __len__(self) -> int:
        return len(self.entries)


def _key(name: str) -> str:
    return " ".join(name.split()).casefold()


def load_crosswalk(path: Path) -> Crosswalk:
    """Parse and fully validate a crosswalk. Every refusal is at load."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(
            f"no crosswalk at {path}", code=E_IMPORT_SOURCE_INVALID, path=str(path)
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(
            f"{path} is not valid TOML: {exc}",
            code=E_IMPORT_SOURCE_INVALID,
            path=str(path),
        ) from exc

    entries_raw = raw.get("instrument")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise _invalid(path, "expected at least one [[instrument]] table")

    entries: list[CrosswalkEntry] = []
    seen: dict[str, str] = {}
    for position, entry in enumerate(entries_raw):
        where = f"[[instrument]] #{position + 1}"
        if not isinstance(entry, dict):
            raise _invalid(path, f"{where} is not a table")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise _invalid(path, f"{where} needs a non-empty `name`")
        symbol = entry.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise _invalid(path, f"instrument {name!r} needs a non-empty `symbol`")
        unknown = sorted(set(entry) - {"name", "symbol", "note"})
        if unknown:
            raise _invalid(
                path,
                f"instrument {name!r} has unknown key(s) {', '.join(unknown)}. "
                f"Known: name, symbol, note",
            )
        key = _key(name)
        if key in seen:
            raise _invalid(
                path,
                f"instrument name {name!r} is mapped twice (also as {seen[key]!r}). "
                f"Two entries for one name would make the result depend on file "
                f"order",
            )
        seen[key] = name
        note = entry.get("note")
        entries.append(
            CrosswalkEntry(
                name=name.strip(),
                symbol=symbol.strip(),
                note=note if isinstance(note, str) else None,
            )
        )
    return Crosswalk(entries=tuple(entries), path=path)


def _invalid(path: Path, message: str) -> ValidationError:
    return ValidationError(
        f"{path.name}: {message}", code=E_IMPORT_SOURCE_INVALID, path=str(path)
    )
