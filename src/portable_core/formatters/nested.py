"""Nested data, for the human formats. Once, here.

A command's ``data`` and an error's ``context`` legitimately carry structures:
a list of per-rung provenance rows, what a wash-sale check does not detect, the
problems ``pt validate`` found. Printed with an f-string, each one reached a
person at a terminal as a Python ``repr`` -- brackets, quotes, and in the
colour path ``Decimal('6000.00')`` (issue 63). This module renders any such
value as indented key and value lines, for `table`, `markdown`, and an error on
stderr alike. `json` keeps the structure as it is.

The rules:

* **A mapping's keys are sorted at every depth**, so a human format lists them
  in the order `--format json` does (``sort_keys``), and two runs agree
  (`CLAUDE.md` invariant 6).
* **A list keeps its order**, which the command chose, one item to a line --
  unless every item is a single token, such as a date or a symbol. Those run
  together, comma-separated: a return over four years can list hundreds of
  stale-price days, and one to a line buries the rest of the report. An item
  holding a space or a comma is never run together, since it could not then
  be told from its neighbours.
* **Empty is not null.** An empty list or mapping reads ``(none)``; null reads
  ``—``. A check that found nothing and a figure that is missing must not look
  the same.
* **A scalar is its canonical text** (:func:`machine`): ``6000.00``, never
  ``Decimal('6000.00')``, and ``100``, never ``1E+2``.
* **A return goes through the return rules** (:func:`render_return`), so a
  return inside ``data`` is labelled with its method, basis and period, and is
  refused if annualized under a year, exactly as one in a column is
  (``PORT-GIPS-B07``, ``PORT-GIPS-H04``). Rendering it through :func:`machine`
  instead printed a bare 34-digit fraction.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Mapping
from typing import Any

from portable_core.formatters.model import ReturnValue
from portable_core.formatters.numbers import NULL_TEXT, machine, render_return

__all__ = ["EMPTY_TEXT", "markdown_lines", "text_lines"]

#: What an empty list or mapping renders as. Never the null marker.
EMPTY_TEXT = "(none)"

#: Indentation of a level of nesting, in the text formats.
_INDENT = "  "

#: A list item's marker, in the text formats.
_BULLET = "- "

#: How wide a run of tokens may grow on a line, in the text formats. Fixed, and
#: never read from the terminal, so the same input gives the same bytes
#: wherever it is rendered (`CLAUDE.md` invariant 6).
_RUN_WIDTH = 76


def _same(text: str) -> str:
    return text


def _entries(mapping: Mapping[Any, Any]) -> list[tuple[str, Any]]:
    return sorted(((str(key), value) for key, value in mapping.items()), key=lambda kv: kv[0])


def _leaf(value: object) -> str | None:
    """The one-line text of *value*, or None when it has children to list."""
    if value is None:
        return NULL_TEXT
    if isinstance(value, ReturnValue):
        return render_return(value)
    if isinstance(value, Mapping | list | tuple):
        return None if value else EMPTY_TEXT
    return str(machine(value))


def _tokens(value: object) -> list[str] | None:
    """The items of a non-empty list of single tokens, or None."""
    if not isinstance(value, list | tuple) or not value:
        return None
    tokens: list[str] = []
    for item in value:
        leaf = _leaf(item)
        if leaf is None or any(char.isspace() or char == "," for char in leaf):
            return None
        tokens.append(leaf)
    return tokens


# ── text: the table format, its colour path, and errors on stderr ───────────


def text_lines(
    data: Mapping[Any, Any],
    *,
    key: Callable[[str], str] = _same,
    text: Callable[[str], str] = _same,
) -> list[str]:
    """*data* as key and value lines, each child indented under its parent.

    Values align among the keys of one mapping. A key whose value has children
    stands alone on its line, with the children below it; a mapping inside a
    list starts on the list item's own line::

        basis_provenance
          - basis_source  derived
            gain          15250.68
        held              T-4.25-2030, VTI
        unreportable      (none)

    *key* and *text* wrap each key and each value as it is written -- the
    colour path passes Rich markup and escaping. Alignment is measured on the
    unwrapped text, so markup does not shift a column.
    """
    return _text_mapping(data, key, text)


def _text_mapping(
    mapping: Mapping[Any, Any], key: Callable[[str], str], text: Callable[[str], str]
) -> list[str]:
    entries = _entries(mapping)
    width = max((len(name) for name, _ in entries), default=0)
    lines: list[str] = []
    for name, value in entries:
        leaf = _leaf(value)
        tokens = _tokens(value)
        if tokens is not None:
            run = ", ".join(tokens)
            if len(run) <= _RUN_WIDTH:
                leaf = run
        if leaf is not None:
            lines.append(f"{key(name)}{' ' * (width - len(name))}  {text(leaf)}")
            continue
        lines.append(key(name))
        if tokens is not None:
            # No break inside a token: a date's hyphens are not break points.
            wrapped = textwrap.wrap(
                ", ".join(tokens),
                width=_RUN_WIDTH,
                break_long_words=False,
                break_on_hyphens=False,
            )
            lines.extend(_INDENT + text(line) for line in wrapped)
        else:
            lines.extend(_INDENT + line for line in _text_children(value, key, text))
    return lines


def _text_children(
    value: Any, key: Callable[[str], str], text: Callable[[str], str]
) -> list[str]:
    if isinstance(value, Mapping):
        return _text_mapping(value, key, text)
    lines: list[str] = []
    for item in value:
        leaf = _leaf(item)
        if leaf is not None:
            lines.append(_BULLET + text(leaf))
            continue
        first, *rest = _text_children(item, key, text)
        lines.append(_BULLET + first)
        lines.extend(" " * len(_BULLET) + line for line in rest)
    return lines


# ── markdown ─────────────────────────────────────────────────────────────────


def markdown_lines(data: Mapping[Any, Any]) -> list[str]:
    """*data* as a nested GitHub-flavoured list, keys in bold.

    A mapping inside a list is a numbered item holding the mapping's own list,
    so its keys stay together under it::

        - **basis_provenance**:
          1. - **basis_source**: derived
             - **gain**: 15250.68

    Each child is indented to its parent item's content, which is what makes it
    a sublist rather than a continuation of the parent's text.
    """
    return _markdown_mapping(data)


def _markdown_mapping(mapping: Mapping[Any, Any]) -> list[str]:
    lines: list[str] = []
    for name, value in _entries(mapping):
        tokens = _tokens(value)
        # A Markdown viewer wraps a long line itself, so a run of tokens is
        # never broken here.
        leaf = ", ".join(tokens) if tokens is not None else _leaf(value)
        if leaf is not None:
            lines.append(f"- **{name}**: {leaf}")
        else:
            lines.append(f"- **{name}**:")
            lines.extend(_INDENT + line for line in _markdown_children(value))
    return lines


def _markdown_children(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        return _markdown_mapping(value)
    lines: list[str] = []
    for number, item in enumerate(value, start=1):
        leaf = _leaf(item)
        if leaf is not None:
            lines.append(f"- {leaf}")
            continue
        marker = f"{number}. "
        first, *rest = _markdown_children(item)
        lines.append(marker + first)
        lines.extend(" " * len(marker) + line for line in rest)
    return lines
