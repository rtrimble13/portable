"""A window of one walk is the walk of that window alone. ADR 0045 §1.

`pert twr` computes a period by walking its days once (`walk`) and taking the
walk's one window (`window`). Multi-period reporting takes many windows from
one long walk, and each row it prints must be exactly what `pert twr` gives
over that row's dates: the same value, or the same refusal, code for code.

These tests hold the engine to that on a portfolio built to reach every
refusal a window can meet. They compare each window of one long walk against
`time_weighted_return` over the same dates, which walks those dates alone.
The comparison covers the repr of the whole result (every sub-period,
disclosure and idle day) and the structured form of every refusal, which is
what `--format json` renders.
"""

from __future__ import annotations

import shutil
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest

from portable_core.clock import FixedClock
from portable_core.domain.returns import TimeWeightedReturn
from portable_core.errors import PortableError, ValidationError
from portable_core.errors.kinds import (
    E_FEE_EMBEDDED_IN_CASH,
    E_GIPS_VALUATION_GAP,
    E_RETURN_BLOCKED,
    E_RETURN_BREAK,
    E_RETURN_NOTHING_HELD,
    E_RETURN_UNPRICED_TRADE,
    E_SNAPSHOT_MISSING,
    E_SNAPSHOT_STALE,
)
from portable_core.persistence.connection import open_portfolio, transaction
from portable_core.persistence.repositories import Repositories
from portable_core.services import price_acceptance
from portable_core.services.returns import time_weighted_return, walk, window
from tests.integration.conftest import ROOT, CliRunner, build_template, copy_template

pytestmark = pytest.mark.integration

THROUGH = date(2024, 2, 29)
FIRST = date(2024, 1, 1)

#: Starts and ends chosen to put a window's ends on usable and unusable days,
#: before and after the second account opens, inside the empty stretch, and
#: across each refusal below.
STARTS: list[date | None] = [
    None,
    *(
        date(2024, month, day)
        for month, day in [
            (1, 1), (1, 3), (1, 5), (1, 10), (1, 14), (1, 16), (1, 20), (1, 23),
            (1, 29), (2, 1), (2, 6), (2, 8), (2, 9), (2, 13), (2, 15),
        ]
    ),
]  # fmt: skip
ENDS = [
    date(2024, month, day)
    for month, day in [
        (1, 1), (1, 4), (1, 9), (1, 13), (1, 15), (1, 19), (1, 21), (1, 25), (1, 31),
        (2, 5), (2, 7), (2, 8), (2, 12), (2, 14), (2, 16), (2, 20), (2, 22),
    ]
]  # fmt: skip
WINDOWS = [(start, end) for start in STARTS for end in ENDS if start is None or start <= end]

MEASURED = "measured"

#: What the portfolio below was built to reach, at each level. A fixture that
#: stopped reaching one would let the comparison pass on less than it claims.
REACHED = {
    None: {
        MEASURED,
        ValidationError.code,
        E_SNAPSHOT_MISSING,
        E_SNAPSHOT_STALE,
        E_RETURN_BLOCKED,
        E_RETURN_UNPRICED_TRADE,
        E_FEE_EMBEDDED_IN_CASH,
        E_GIPS_VALUATION_GAP,
    },
    "C": {
        MEASURED,
        ValidationError.code,
        E_SNAPSHOT_MISSING,
        E_RETURN_BREAK,
        E_RETURN_NOTHING_HELD,
    },
}
REACHED["B"] = REACHED[None]

_XYZ = """
2024-01-03 100 | 2024-01-04 101 | 2024-01-05 99  | 2024-01-08 102 | 2024-01-09 103
2024-01-19 104 | 2024-01-22 105 | 2024-01-23 104 | 2024-01-24 106 | 2024-02-05 107
2024-02-06 108 | 2024-02-07 109 | 2024-02-08 108 | 2024-02-09 110 | 2024-02-12 111
2024-02-13 112 | 2024-02-14 111 | 2024-02-15 113 | 2024-02-16 114 | 2024-02-19 115
2024-02-20 116
"""


def _build_mixed(run_pt: CliRunner, portfolio: Path) -> None:
    """Account B, funded 500,000 on 01-02 by the base template, and account C.

    What each window can meet:

    * **Unpriced days.** XYZ goes unpriced 01-10 to 01-18 and 01-25 to 02-04.
      The default tolerance is five days, so 01-15 to 01-18 and 01-30 to 02-04
      are incomplete. The first gap is bridged unless a large flow is inside
      it, and the 100,000 deposit on 01-17 is one. The second spans a month
      end. Both refuse (PT-E-GIPS-VALUATION-GAP).
    * **A second account** opens 01-20. A portfolio window ending before then
      has one account.
    * **An empty stretch.** C is emptied 01-22 and re-funded 02-12. Its track
      record breaks there (ADR 0045 §4): a window across the gap is refused,
      one inside it holds nothing, and one that starts or ends inside it is
      moved to the record. The portfolio's does not break, since B holds
      value throughout.
    * **A cash fee classed as a fund expense** on 02-12 (PORT-GIPS-D05), and
      **a cash trade in an instrument with no market price** on 02-14 (ADR
      0040 §12).
    * **Snapshots valued only through 02-20**, so a window ending later is
      refused for the missing days.
    * **A price changed after valuing** on 02-07, so the snapshot that
      recorded the old one no longer holds (ADR 0024).

    The test adds an unresolved statement line on 02-16, which blocks B from
    that day (ADR 0031): `pt reconcile` is the only command that records one,
    and it needs a custodian's files.
    """
    port = str(portfolio)
    run_pt("--port", port, "policy", "set", "--effective-from", "2024-01-01",
           "--large-flow-pct", "0.10")  # fmt: skip
    run_pt("--port", port, "instrument", "add", "XYZ", "--type", "equity")
    run_pt("--port", port, "instrument", "add", "CVR", "--type", "equity", "--no-market-price")
    run_pt("--port", port, "buy", "XYZ", "--qty", "1000", "--price", "100", "--date",
           "2024-01-03", "-a", "B")  # fmt: skip
    for entry in _XYZ.replace("\n", "|").split("|"):
        if entry.strip():
            on, px = entry.split()
            run_pt("--port", port, "price", "set", "XYZ", "--price", px, "--date", on,
                   "--valuation-level", "1")  # fmt: skip
    run_pt("--port", port, "cash", "deposit", "-a", "B", "--amount", "100000",
           "--date", "2024-01-17")  # fmt: skip
    run_pt("--port", port, "account", "add", "--name", "C", "--type", "taxable",
           "--opened", "2024-01-20", "--relief-method", "fifo")  # fmt: skip
    for verb, amount, on in (
        ("deposit", "50000", "2024-01-20"),
        ("withdraw", "50000", "2024-01-22"),
        ("deposit", "20000", "2024-02-12"),
    ):
        run_pt("--port", port, "cash", verb, "-a", "C", "--amount", amount, "--date", on)
    run_pt("--port", port, "cash", "fee", "-a", "B", "--amount", "50", "--fee-class",
           "embedded_fund_fee", "-d", "2024-02-12")  # fmt: skip
    run_pt("--port", port, "buy", "CVR", "--qty", "10", "--price", "1", "--date",
           "2024-02-14", "-a", "B")  # fmt: skip
    run_pt("--port", port, "value", "--from", "2024-01-02", "--to", "2024-02-20")
    run_pt("--port", port, "price", "set", "XYZ", "--price", "150", "--date", "2024-02-07",
           "--valuation-level", "1")  # fmt: skip


@pytest.fixture(scope="module")
def mixed_template(tmp_path_factory: pytest.TempPathFactory, portfolio_template: Path) -> Path:
    return build_template(tmp_path_factory, _build_mixed, start_from=portfolio_template)


@pytest.fixture
def mixed(mixed_template: Path, tmp_path: Path) -> Iterator[Repositories]:
    """The mixed portfolio, with B's statement line on 02-16 left unresolved."""
    con = open_portfolio(copy_template(mixed_template, tmp_path / "mixed.port"))
    repos = Repositories(con, clock=FixedClock(datetime(2024, 3, 1, tzinfo=UTC)))
    with transaction(con):
        # 1,000 XYZ stated at 1.00: a break no price on file explains.
        price_acceptance.record(
            repos,
            repos.accounts.resolve("B"),
            repos.instruments.resolve("XYZ"),
            date(2024, 2, 16),
            D("1000"),
            D("1.00"),
            source="test",
        )
    yield repos
    con.close()


def _outcome(
    compute: Callable[..., TimeWeightedReturn], **arguments: Any
) -> tuple[str, object, TimeWeightedReturn | None]:
    """What a caller sees: the whole result, or the refusal as JSON renders it."""
    try:
        result = compute(**arguments)
    except PortableError as refusal:
        return refusal.code, (type(refusal).__name__, refusal.to_dict()), None
    return MEASURED, repr(result), result


@pytest.mark.gips
@pytest.mark.parametrize("level", [None, "B", "C"], ids=["portfolio", "B", "C"])
def test_port_gips_b04_a_window_of_a_walk_is_the_walk_of_the_window(
    mixed: Repositories, level: str | None
) -> None:
    """Every window of one walk from 01-01 to 02-29, against `pert twr` over it."""
    account = mixed.accounts.resolve(level) if level is not None else None
    walked = walk(mixed, first=FIRST, through=THROUGH, account=account, staleness_days=5)

    reached: Counter[str] = Counter()
    moved = 0
    for start, end in WINDOWS:
        code, seen, result = _outcome(window, walked=walked, start=start, end=end)
        alone_code, alone, _ = _outcome(
            time_weighted_return,
            repos=mixed,
            start=start,
            end=end,
            account=account,
            staleness_days=5,
        )
        assert (code, seen) == (alone_code, alone), (level, start, end)
        reached[code] += 1
        if result is not None and (result.nothing_held_before or result.nothing_held_after):
            moved += 1

    assert set(reached) == REACHED[level], reached
    # Only C ever holds nothing: a window starting or ending in its empty
    # stretch is moved to the record, and no other level's ever is.
    assert (moved > 0) == (level == "C"), moved


@pytest.mark.gips
def test_port_gips_b04_windows_of_the_sample_are_its_walks(tmp_path: Path) -> None:
    """The same, on the committed sample: five years, three accounts, Modified Dietz."""
    copy = tmp_path / "sample.port"
    shutil.copyfile(ROOT / "examples" / "sample.port", copy)
    con = open_portfolio(copy)
    try:
        repos = Repositories(con)
        walked = walk(
            repos, first=date(2021, 1, 1), through=date(2025, 12, 31), staleness_days=5
        )
        for start, end in [
            (None, date(2025, 12, 31)),
            (date(2024, 1, 1), date(2024, 12, 31)),
            (date(2023, 4, 1), date(2023, 6, 30)),
            (date(2022, 2, 10), date(2022, 3, 15)),
            (date(2025, 6, 1), date(2025, 6, 20)),
        ]:
            seen = _outcome(window, walked=walked, start=start, end=end)
            alone = _outcome(
                time_weighted_return, repos=repos, start=start, end=end, staleness_days=5
            )
            assert seen[:2] == alone[:2], (start, end)
    finally:
        con.close()


def test_a_window_outside_its_walk_is_a_mistake_in_the_caller(mixed: Repositories) -> None:
    """Not a refusal of the portfolio: the walk never read the days asked for."""
    walked = walk(mixed, first=date(2024, 1, 10), through=date(2024, 1, 31), staleness_days=5)
    with pytest.raises(ValueError, match="before the walk's first day"):
        window(walked, start=date(2024, 1, 5), end=date(2024, 1, 20))
    with pytest.raises(ValueError, match="after the walk's"):
        window(walked, start=date(2024, 1, 10), end=date(2024, 2, 5))
