#!/usr/bin/env python3
"""Generate `examples/sample.bmk`: a cap-weighted index of five fictional stocks.

Like `examples/sample.port`, it is generated, not hand-made, and two runs write
the same bytes: every stamp is read from one fixed clock and the version it
claims is pinned (ADR 0035, ADR 0049 §5). Its inputs are written as files and
read back through `FileProvider`, the services `bmk` itself calls, so the
sample is what the commands produce.

The index, from 2025-01-02 at 1,000, cap-weighted, rebalanced quarterly:

* ALFA, BRAV, CHAR and DELT from the base date; CHAR floats 80%.
* BRAV splits 2-for-1, ex 2025-03-03.
* ALFA and CHAR pay quarterly; the others pay nothing.
* On 2025-04-01 DELT leaves and ECHO joins: a reconstitution.

Prices move in a straight line in sessions with a fixed wobble. There is no
randomness: a reader can recompute any close from :func:`close`.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import portable_core.schema.bmk as bmk_schema  # noqa: E402
from portable_core.clock import FixedClock  # noqa: E402
from portable_core.decimals import money_context  # noqa: E402
from portable_core.domain.index import (  # noqa: E402
    IndexMeta,
    Member,
    RebalanceRule,
    SecurityType,
    SyntheticSecurity,
    Weighting,
)
from portable_core.persistence.connection import open_index, transaction  # noqa: E402
from portable_core.persistence.index_repositories import IndexRepositories  # noqa: E402
from portable_core.providers import FileProvider  # noqa: E402
from portable_core.services import (  # noqa: E402
    index_construction,
    index_definition,
    index_inputs,
)

D = Decimal
STAMP = datetime(2026, 11, 27, tzinfo=UTC)
CLOCK = FixedClock(STAMP)
#: Not a release, so nobody reads it as one (ADR 0049 §5).
STAMPED_VERSION = "0.0.0+sample"

BASE = date(2025, 1, 2)
LAST = date(2025, 6, 30)
#: NYSE's closures in the span, by name: a calendar is listed, not inferred.
HOLIDAYS = {
    date(2025, 1, 9): "national day of mourning",
    date(2025, 1, 20): "Martin Luther King Jr. Day",
    date(2025, 2, 17): "Washington's Birthday",
    date(2025, 4, 18): "Good Friday",
    date(2025, 5, 26): "Memorial Day",
    date(2025, 6, 19): "Juneteenth",
}
SPLIT = date(2025, 3, 3)
RECONSTITUTION = date(2025, 4, 1)

#: symbol: (first close, close-to-close drift per session, shares outstanding).
STOCKS = {
    "ALFA": (D("50.00"), D("0.04"), D("1000000000")),
    "BRAV": (D("120.00"), D("0.10"), D("500000000")),
    "CHAR": (D("30.00"), D("-0.01"), D("2000000000")),
    "DELT": (D("80.00"), D("0.02"), D("300000000")),
    "ECHO": (D("25.00"), D("0.03"), D("400000000")),
}
DIVIDENDS = {
    "ALFA": [(date(2024, 11, 7), D("0.50")), (date(2025, 2, 6), D("0.50")),
             (date(2025, 5, 8), D("0.52"))],
    "CHAR": [(date(2024, 12, 12), D("0.30")), (date(2025, 3, 13), D("0.30")),
             (date(2025, 6, 12), D("0.30"))],
}  # fmt: skip


def sessions() -> list[date]:
    days, on = [], BASE
    while on <= LAST:
        if on.weekday() < 5 and on not in HOLIDAYS:
            days.append(on)
        on += timedelta(days=1)
    return days


def close(symbol: str, index: int, on: date) -> Decimal:
    """The close of *symbol* on the *index*-th session: a line and a wobble."""
    start, drift, _ = STOCKS[symbol]
    wobble = D((index * 7 + len(symbol) * 3) % 9 - 4) * D("0.07")
    with money_context():
        price = start + drift * index + wobble
        if symbol == "BRAV" and on >= SPLIT:
            price = price / 2
        return price.quantize(D("0.01"))


def shares(symbol: str, on: date) -> Decimal:
    count = STOCKS[symbol][2]
    return count * 2 if symbol == "BRAV" and on >= SPLIT else count


def write_inputs(folder: Path) -> FileProvider:
    days = sessions()
    (folder / "calendar.csv").write_text(
        "exchange,date\n" + "".join(f"XNYS,{d.isoformat()}\n" for d in days), encoding="utf-8"
    )
    lines = ["symbol,date,price"]
    for symbol in STOCKS:
        lines += [f"{symbol},{d.isoformat()},{close(symbol, i, d)}" for i, d in enumerate(days)]
    (folder / "prices.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    actions = ["symbol,action_type,ex_date,split_numerator,split_denominator,cash_amount"]
    actions.append(f"BRAV,split,{SPLIT.isoformat()},2,1,")
    for symbol, paid in DIVIDENDS.items():
        actions += [f"{symbol},cash_dividend,{ex.isoformat()},,,{paid_}" for ex, paid_ in paid]
    (folder / "actions.csv").write_text("\n".join(actions) + "\n", encoding="utf-8")
    caps = ["symbol,date,market_cap,close_raw,shares_outstanding,share_factor,source"]
    for i, d in enumerate(days):
        for symbol in STOCKS:
            price, count = close(symbol, i, d), shares(symbol, d)
            caps.append(f"{symbol},{d.isoformat()},{price * count},{price},{count},1,example")
    (folder / "caps.csv").write_text("\n".join(caps) + "\n", encoding="utf-8")
    return FileProvider(
        price_file=folder / "prices.csv",
        action_file=folder / "actions.csv",
        calendar_file=folder / "calendar.csv",
        cap_file=folder / "caps.csv",
    )


def build(path: Path, *, force: bool = False) -> dict[str, int]:
    if path.exists():
        if not force:
            raise SystemExit(f"{path} exists. Pass --force to regenerate.")
        path.unlink()
    for suffix in ("-wal", "-shm"):
        extra = path.with_name(path.name + suffix)
        if extra.exists():
            extra.unlink()

    con = open_index(path, must_exist=False)
    con.execute("PRAGMA secure_delete = ON")
    bmk_schema.initialise(con, clock=CLOCK, applied_by=f"portable {STAMPED_VERSION}")
    repos = IndexRepositories(con, clock=CLOCK)
    with tempfile.TemporaryDirectory() as scratch:
        provider = write_inputs(Path(scratch))
        with transaction(con):
            index_definition.create(
                repos,
                IndexMeta(
                    index_name="Example cap-weighted five, quarterly",
                    description=(
                        "A generated demonstration index: five fictional stocks, "
                        "cap-weighted, rebalanced quarterly, with a split, quarterly "
                        "dividends, a free float and a reconstitution."
                    ),
                    base_date=BASE,
                    base_level=D(1000),
                    currency="USD",
                    calendar="XNYS",
                ),
                version=STAMPED_VERSION,
            )
            ids = {}
            for symbol in STOCKS:
                ids[symbol] = index_definition.add_security(
                    repos,
                    SyntheticSecurity(
                        security_id=0,
                        symbol=symbol,
                        security_type=SecurityType.EQUITY,
                        source="example",
                        name=f"{symbol.title()} Corp.",
                        issuer_key=f"EXAMPLE-{symbol}",
                    ),
                )
                repos.add_symbol(ids[symbol], symbol, date.min, None, "example")
            index_definition.set_methodology(
                repos, BASE, Weighting.CAP, RebalanceRule.QUARTERLY, note="The example's."
            )
            char = Member(ids["CHAR"], free_float=D("0.8"), free_float_source="example")
            first = [Member(ids[s]) for s in ("ALFA", "BRAV", "DELT")]
            index_definition.reconstitute(repos, BASE, [*first, char])
            later = [Member(ids[s]) for s in ("ALFA", "BRAV", "ECHO")]
            index_definition.reconstitute(
                repos, RECONSTITUTION, [*later, char], note="DELT leaves, ECHO joins."
            )
            index_inputs.refresh(repos, provider, through=LAST)
            index_definition.accept_proposals(
                repos, index_definition.propose_schedules(repos), source="file"
            )
            built_by = f"portable {STAMPED_VERSION}"
            index_construction.build(repos, through=None, built_by=built_by)
    counts = {
        "sessions": len(repos.trading_days()),
        "securities": len(repos.securities()),
        "levels": len(repos.levels()),
        "reinvestments": len(repos.reinvestments()),
        "market_caps": len(repos.market_caps()),
    }
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "examples" / "sample.bmk")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    counts = build(args.out, force=args.force)
    print(f"wrote {args.out}")
    for key in sorted(counts):
        print(f"  {key}: {counts[key]}")
    print(f"  written by SQLite {sqlite3.sqlite_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
