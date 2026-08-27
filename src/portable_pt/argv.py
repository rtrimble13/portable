"""Accept global flags after the subcommand as well as before it.

The bootstrap requires that **every** command support ``--format``, ``--as-of``,
``--source``, ``--offline``, ``-v/-vv``, ``--quiet`` and ``--no-color`` (§7.2).
Click parses group-level options only *before* the subcommand, so
``pt holdings --format json`` -- which is what a person types, and what an agent
generates -- fails with "No such option".

Declaring all of them on all fifty commands would work and would be fifty
places for one to be forgotten. Instead this normalises ``argv`` once, moving
recognised global flags ahead of the subcommand, so both orders behave
identically.

The rule is deliberately conservative, and the reason is in
:func:`normalise_argv`.
"""

from __future__ import annotations

from typing import Final

__all__ = ["GLOBAL_FLAGS", "GLOBAL_VALUE_FLAGS", "normalise_argv"]

#: Global flags that take no value.
GLOBAL_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "--offline",
        "--dry-run",
        "--yes",
        "-y",
        "--quiet",
        "-q",
        "--no-color",
        "--log-json",
        "-v",
        "-vv",
        "-vvv",
        "--verbose",
    }
)

#: Global flags that consume the following arg as their value.
GLOBAL_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "--port",
        "--format",
        "-f",
        "--as-of",
        "--source",
        "-S",
    }
)


def normalise_argv(argv: list[str]) -> list[str]:
    """Move global flags ahead of the subcommand.

    ``pt holdings --format json`` becomes ``pt --format json holdings``.

    Three rules keep this from changing the meaning of anything else:

    1. **Only exact matches** for the flags above move. ``--format=json`` is
       left alone, because click already handles the ``=`` form at group level
       and because moving it is unnecessary.
    2. **Nothing after ``--`` moves.** That is the conventional end-of-options
       marker and must stay inert.
    3. **A flag whose preceding arg is itself an unrecognised option does not
       move.** ``--note --as-of`` means a note whose text is ``--as-of``, which
       is bizarre but legal; leaving it in place preserves that meaning rather
       than silently reinterpreting it. Anybody who genuinely wants such a
       value should write ``--note=--as-of``, which rule 1 already leaves
       untouched.
    """
    if not argv:
        return argv

    leading: list[str] = []
    rest: list[str] = []
    index = 0
    seen_subcommand = False
    previous = ""

    while index < len(argv):
        arg = argv[index]

        if arg == "--":
            rest.extend(argv[index:])
            break

        if not seen_subcommand:
            # Before the subcommand, everything already parses correctly.
            leading.append(arg)
            if not arg.startswith("-") and not _is_value_of(previous):
                seen_subcommand = True
            previous = arg
            index += 1
            continue

        movable = arg in GLOBAL_FLAGS or arg in GLOBAL_VALUE_FLAGS
        preceded_by_unknown_option = (
            previous.startswith("-")
            and previous not in GLOBAL_FLAGS
            and previous not in GLOBAL_VALUE_FLAGS
        )

        if movable and not preceded_by_unknown_option:
            leading.insert(_insertion_point(leading), arg)
            if arg in GLOBAL_VALUE_FLAGS and index + 1 < len(argv):
                leading.insert(_insertion_point(leading), argv[index + 1])
                index += 1
        else:
            rest.append(arg)

        previous = arg
        index += 1

    return [*leading, *rest]


def _insertion_point(leading: list[str]) -> int:
    """Just before the subcommand, which is the last non-flag arg collected."""
    for position in range(len(leading) - 1, -1, -1):
        if not leading[position].startswith("-"):
            return position
    return len(leading)


def _is_value_of(previous: str) -> bool:
    """Whether the current arg is the value of the previous option."""
    return previous in GLOBAL_VALUE_FLAGS
