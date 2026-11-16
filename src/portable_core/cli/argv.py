"""Accept a CLI's global flags after the subcommand as well as before it.

**Every** command supports the global flags (``CLAUDE.md``, "CLI shape"):
``--format``, ``--as-of``, ``-v/-vv``, ``--quiet``, ``--no-color`` and, for a
CLI that has them, ``--source``, ``--offline``, ``--dry-run`` and ``--yes``.
Click parses group-level options only *before* the subcommand, so
``pt holdings --format json`` -- which is what a person types, and what an agent
generates -- fails with "No such option".

Declaring all of them on every command would work and would be one place per
command for one to be forgotten. Instead each CLI's entry point normalises
``argv`` once, moving its recognised global flags ahead of the subcommand, so
both orders behave identically. It lives here because every CLI needs it and
no CLI imports another.

The rule is deliberately conservative, and the reason is in
:func:`normalise_argv`.
"""

from __future__ import annotations

from collections.abc import Set

__all__ = ["normalise_argv"]


def normalise_argv(argv: list[str], *, flags: Set[str], value_flags: Set[str]) -> list[str]:
    """Move global flags ahead of the subcommand.

    *flags* are the CLI's global flags that take no value, *value_flags* those
    that consume the following argument as their value.

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
            if not arg.startswith("-") and previous not in value_flags:
                seen_subcommand = True
            previous = arg
            index += 1
            continue

        movable = arg in flags or arg in value_flags
        preceded_by_unknown_option = (
            previous.startswith("-") and previous not in flags and previous not in value_flags
        )

        if movable and not preceded_by_unknown_option:
            leading.insert(_insertion_point(leading), arg)
            if arg in value_flags and index + 1 < len(argv):
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
