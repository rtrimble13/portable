"""Global flags must work after the subcommand as well as before it.

The bootstrap requires every command to support --format, --as-of and the rest
(§7.2), and click only parses group-level options before the subcommand. This
is the shim, and these are the cases that keep it from changing the meaning of
anything else.
"""

from __future__ import annotations

import pytest

from portable_pt.argv import normalise_argv

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # The case that motivated this: what a person actually types.
        (["holdings", "--format", "json"], ["--format", "json", "holdings"]),
        (
            ["--port", "p.port", "holdings", "--format", "json"],
            ["--port", "p.port", "--format", "json", "holdings"],
        ),
        # Sub-groups: the flag hops the whole path, not just the last word.
        (
            ["lot", "list", "AAPL", "--as-of", "2025-06-30"],
            ["--as-of", "2025-06-30", "lot", "list", "AAPL"],
        ),
        # Value-less flags.
        (["rebuild", "--dry-run"], ["--dry-run", "rebuild"]),
        (
            ["sell", "AAPL", "--qty", "10", "-v", "-v"],
            ["-v", "-v", "sell", "AAPL", "--qty", "10"],
        ),
        # Already correct: unchanged.
        (["--format", "json", "holdings"], ["--format", "json", "holdings"]),
        # Command options stay put.
        (
            ["buy", "AAPL", "--qty", "100", "--price", "185.64"],
            ["buy", "AAPL", "--qty", "100", "--price", "185.64"],
        ),
        # Mixed.
        (
            ["buy", "AAPL", "--qty", "100", "--format", "json", "--price", "1"],
            ["--format", "json", "buy", "AAPL", "--qty", "100", "--price", "1"],
        ),
        ([], []),
        (["--help"], ["--help"]),
    ],
)
def test_global_flags_move_ahead_of_the_subcommand(
    given: list[str], expected: list[str]
) -> None:
    assert normalise_argv(given) == expected


def test_the_equals_form_is_left_alone() -> None:
    """click already handles `--format=json` at group level, so leave it."""
    assert normalise_argv(["holdings", "--format=json"]) == [
        "holdings",
        "--format=json",
    ]


def test_nothing_after_the_end_of_options_marker_moves() -> None:
    """`--` is the conventional end of options and must stay inert."""
    assert normalise_argv(["query", "--", "--as-of", "x"]) == [
        "query",
        "--",
        "--as-of",
        "x",
    ]


def test_a_value_that_looks_like_a_global_flag_is_not_hijacked() -> None:
    """`--note --as-of` means a note whose text is "--as-of".

    Bizarre, but legal, and reinterpreting it would silently change what the
    user recorded on a ledger entry. The conservative rule leaves it alone.
    """
    assert normalise_argv(["buy", "AAPL", "--note", "--as-of"]) == [
        "buy",
        "AAPL",
        "--note",
        "--as-of",
    ]


def test_the_flag_still_moves_when_it_follows_a_known_global_flag() -> None:
    """Two globals in a row are both globals, not an option and its value."""
    assert normalise_argv(["holdings", "--dry-run", "--format", "json"]) == [
        "--dry-run",
        "--format",
        "json",
        "holdings",
    ]
