#!/usr/bin/env python3
"""Rehearse `bmk` on real securities from fafnir: is an index right, judged from outside?

It builds indexes through the `bmk` CLI, which reads fafnir through
portable's own provider (`providers/fafnir.py`, ADR 0032), and sets each
result beside a reference computed here from `duk` directly:

* **Cap weights.** AAPL, MSFT, XOM and JPM, cap-weighted and rebalanced
  quarterly from 2024-01-02. At each rebalance, each target weight must equal
  that day's `duk mc` cap over their sum, to the last digit.
* **Price return.** Over each quarter between rebalances, the price level's
  change must equal the units held times `duk ph --exact` closes, to the last
  digit.
* **Total return.** AAPL alone and XOM alone, never rebalanced. The
  total-return level must equal the raw closes and `duk ca` dividends
  compounded by hand, reinvested at the ex-date close as `bmk` does
  (ADR 0050, answer 4): `(close + dividend) / previous close`.
* **fafnir's adjusted series.** `duk ph --adj` compounds
  `close / (previous close - dividend)` instead. Recomputed on that convention
  from the same closes and dividends, it must agree with `--adj` within the
  rounding of `--adj` to the cent. That is what shows a difference between a
  `bmk` total return and `--adj` to be the convention, not a missing dividend.
* **Refusals.** A cap-weighted index holding an ETF (SPY), or a secondary
  share class (GOOG), must be refused by name: `duk mc` has no cap for either.

Why the reference reads `duk` itself: `CLAUDE.md` keeps every use of fafnir
inside `providers/fafnir.py`, and that path is part of what is under test. A
reference read through the same code would check it against itself. Nothing
in `portable` gains a dependency on `duk` from this script.

What it cannot check: both sides read fafnir's closes, dividends and caps, so
wrong vendor data would be wrong in both. It tests `bmk`'s mechanics, not the
warehouse.

Run it where `duk` reaches the warehouse:

    python scripts/rehearse_index.py --work DIR

It prints one line per check and exits 1 if any fails. It prints agreement,
not prices: nothing it writes holds a vendor's figure.
"""

from __future__ import annotations

import argparse
import decimal
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

D = Decimal
CONTEXT = decimal.Context(prec=34, rounding=ROUND_HALF_EVEN)
#: Units are set to 34 significant digits, so a level can differ from the
#: reference in its last few digits; anything more is a defect.
EXACT = D("1e-25")
BASE, THROUGH = date(2024, 1, 2), date(2025, 6, 30)
CAP_MEMBERS = ("AAPL", "MSFT", "XOM", "JPM")
ALONE = ("AAPL", "XOM")
REFUSED = (("AAPL", "SPY"), ("AAPL", "GOOG"))
REPO = Path(__file__).resolve().parents[1]


# ── fafnir, read directly ────────────────────────────────────────────────────


def duk(*argv: str) -> Any:
    done = subprocess.run(
        ["duk", "-S", "db", *argv, "--json"], capture_output=True, encoding="utf-8", check=False
    )
    if done.returncode:
        raise SystemExit(f"duk {' '.join(argv)} failed: {done.stderr.strip()}")
    return json.loads(done.stdout, parse_float=D)


def security_id(symbol: str) -> int:
    periods = duk("th", f"ticker:{symbol}")
    covering = [
        p for p in periods
        if p["valid_from"] <= BASE.isoformat()
        and (p["valid_to"] is None or p["valid_to"] >= BASE.isoformat())
    ]  # fmt: skip
    if len(covering) != 1:
        raise SystemExit(f"{symbol} names {len(covering)} securities on {BASE}")
    return int(covering[0]["security_id"])


def closes(sid: int) -> dict[str, Decimal]:
    rows = duk("ph", f"sid:{sid}", "--start-date", BASE.isoformat(),
               "--end-date", THROUGH.isoformat(), "--close", "--exact")  # fmt: skip
    return {r["date"]: D(r["close"]) for r in rows}


def adjusted(sid: int) -> dict[str, Decimal]:
    rows = duk("ph", f"sid:{sid}", "--start-date", BASE.isoformat(),
               "--end-date", THROUGH.isoformat(), "--close", "--adj")  # fmt: skip
    return {r["date"][:10]: D(str(r["close"])) for r in rows}


def dividends(sid: int) -> dict[str, Decimal]:
    rows = duk("ca", f"sid:{sid}", "--start-date", BASE.isoformat(),
               "--end-date", THROUGH.isoformat(), "--exact")  # fmt: skip
    return {
        r["ex_date"]: D(r["dividend_amount"]) for r in rows if r["action_type"] == "dividend"
    }


def cap(sid: int, on: str) -> Decimal:
    (row,) = duk("mc", f"sid:{sid}", "-s", on, "-e", on, "--exact")
    return D(row["market_cap"])


# ── bmk ──────────────────────────────────────────────────────────────────────


@dataclass
class Bmk:
    """`bmk` from this checkout, reading fafnir, in a working directory."""

    work: Path

    def __call__(self, index: Path, *args: str, expect: int = 0) -> dict[str, Any]:
        env = dict(os.environ) | {
            "PYTHONPATH": str(REPO / "src"),
            "PORTABLE_SOURCE": "fafnir",
            "PORTABLE_BMK": str(index),
            "HOME": str(self.work),
            "USERPROFILE": str(self.work),
        }
        done = subprocess.run(
            [sys.executable, "-m", "portable_bmk", "--format", "json", *args],
            capture_output=True, encoding="utf-8", check=False, cwd=self.work, env=env,
        )  # fmt: skip
        if done.returncode != expect:
            raise SystemExit(
                f"bmk {' '.join(args)} exited {done.returncode}, not {expect}:\n"
                f"{done.stdout}{done.stderr}"
            )
        payload: dict[str, Any] = json.loads(done.stdout)
        return payload

    def index(self, name: str, symbols: tuple[str, ...], weighting: str, rule: str,
              members: list[str], *, expect_build: int = 0) -> Path:  # fmt: skip
        path = self.work / f"{name}.bmk"
        path.unlink(missing_ok=True)
        self(path, "init", str(path), "--name", name, "--description", "rehearsal",
             "--base-date", BASE.isoformat())  # fmt: skip
        for symbol in symbols:
            self(path, "--as-of", BASE.isoformat(), "security", "add", symbol)
        self(path, "methodology", "set", "--weighting", weighting, "--rebalance", rule,
             "--effective-from", BASE.isoformat())  # fmt: skip
        self(path, "reconstitute", *members, "--effective-date", BASE.isoformat())
        self(path, "refresh", "--through", THROUGH.isoformat())
        self(path, "schedule", "accept")
        self.built = self(path, "build", expect=expect_build)
        return path


# ── the checks ───────────────────────────────────────────────────────────────


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


def cap_index(bmk: Bmk) -> list[Check]:
    path = bmk.index("cap4", CAP_MEMBERS, "cap", "quarterly", list(CAP_MEMBERS))
    sids = {s: security_id(s) for s in CAP_MEMBERS}
    found: list[Check] = []
    with closing(sqlite3.connect(path)) as con, decimal.localcontext(CONTEXT):
        local = {
            s: con.execute(
                "SELECT security_id FROM security WHERE symbol = ?", (s,)
            ).fetchone()[0]
            for s in CAP_MEMBERS
        }
        rebalances = [r[0] for r in con.execute(
            "SELECT DISTINCT rebalance_date FROM rebalance_weight ORDER BY 1")]  # fmt: skip
        worst = D(0)
        for on in rebalances:
            caps = {s: cap(sids[s], on) for s in CAP_MEMBERS}
            total = sum(caps.values(), D(0))
            for s in CAP_MEMBERS:
                stored = D(con.execute(
                    "SELECT target_weight FROM rebalance_weight WHERE rebalance_date = ? "
                    "AND security_id = ?", (on, local[s])).fetchone()[0])  # fmt: skip
                worst = max(worst, abs(stored - caps[s] / total))
        found.append(Check(f"cap weights at {len(rebalances)} rebalances", worst <= EXACT,
                           f"largest difference {worst:.1E}"))  # fmt: skip

        raw = {s: closes(sids[s]) for s in CAP_MEMBERS}
        level = dict(con.execute("SELECT level_date, price_level FROM level"))
        days = sorted(level)
        worst = D(0)
        quarters = 0
        for start, end in zip(rebalances, [*rebalances[1:], days[-1]], strict=True):
            after = days[days.index(start) + 1]
            units = {
                s: D(con.execute(
                    "SELECT units FROM holding WHERE security_id = ? AND from_date <= ? "
                    "AND to_date >= ?", (local[s], after, after)).fetchone()[0])
                for s in CAP_MEMBERS
            }  # fmt: skip
            reference = sum((units[s] * raw[s][end] for s in CAP_MEMBERS), D(0)) / sum(
                (units[s] * raw[s][start] for s in CAP_MEMBERS), D(0)
            )
            built = D(level[end]) / D(level[start])
            worst = max(worst, abs(built - reference))
            quarters += 1
        found.append(Check(f"price return over {quarters} quarters", worst <= EXACT,
                           f"largest difference {worst:.1E}"))  # fmt: skip
    return found


def alone(bmk: Bmk, symbol: str) -> list[Check]:
    path = bmk.index(f"{symbol.lower()}_alone", (symbol,), "custom", "never", [f"{symbol}=1"])
    sid = security_id(symbol)
    raw, paid, adj = closes(sid), dividends(sid), adjusted(sid)
    days = sorted(raw)
    with closing(sqlite3.connect(path)) as con, decimal.localcontext(CONTEXT):
        tr = dict(con.execute("SELECT level_date, total_return_level FROM level"))
        built = D(tr[days[-1]]) / D(tr[days[0]])
        forward = backward = D(1)
        for a, b in pairwise(days):
            forward *= (raw[b] + paid.get(b, D(0))) / raw[a]
            backward *= raw[b] / (raw[a] - paid.get(b, D(0)))
        against_adj = backward - adj[days[-1]] / adj[days[0]]
        slack = D("0.005") / adj[days[0]] + D("0.005") / adj[days[-1]]
        gap = (built - adj[days[-1]] / adj[days[0]]) * 10000
    return [
        Check(f"{symbol} total return, {len(paid)} dividends", abs(built - forward) <= EXACT,
              f"difference {built - forward:.1E}"),
        Check(f"{symbol} duk --adj is the backward convention", abs(against_adj) <= slack,
              f"{against_adj * 10000:+.2f} bp against +/-{slack * 10000:.2f} bp of rounding; "
              f"bmk differs from --adj by {gap:+.2f} bp"),
    ]  # fmt: skip


def refused(bmk: Bmk, pair: tuple[str, str]) -> Check:
    name = "_".join(pair).lower()
    bmk.index(name, pair, "cap", "quarterly", list(pair), expect_build=5)
    error = bmk.built["error"]
    ok = error["code"] == "PT-E-INDEX-CAP-MISSING" and pair[1] in error["message"]
    return Check(f"cap index holding {pair[1]} is refused", ok, error["code"])


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--work", type=Path, required=True,
                        help="Where the indexes are built. Not the repository.")  # fmt: skip
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    bmk = Bmk(args.work)

    checks = cap_index(bmk)
    for symbol in ALONE:
        checks += alone(bmk, symbol)
    checks += [refused(bmk, pair) for pair in REFUSED]

    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"{'pass' if c.passed else 'FAIL'}  {c.name:<{width}}  {c.detail}")
    failed = [c for c in checks if not c.passed]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
