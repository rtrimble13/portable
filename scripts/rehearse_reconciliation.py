#!/usr/bin/env python3
"""Rehearse issue 58 on samples priced from fafnir: is `pert` right, judged from outside?

Issue 58 reconciles `pert` to the custodian's reported performance on the
owner's portfolio. This rehearses that comparison where the right answer can
be worked out independently, on portfolios of real securities, so the engine
is checked against cases it was not written around.

It builds two portfolios through `pt`. Prices come through portable's own
fafnir provider (`pt price load`), and splits and dividends through
`pt ca sync`. It values them (`pt value`) and runs `pert periods` at every
level. Every period's return is then set beside a reference computed here
from `duk` directly:

* **Account A** holds SPY and reinvests each dividend at the ex-dividend
  reference price, the previous close less the dividend. fafnir's
  dividend-adjusted series adjusts by the factor (P_prev - D) / P_prev, which
  is that reinvestment. So A's return must equal SPY's adjusted return.
* **Account B** holds NVDA, AGG and VFIAX, reinvested the same way. NVDA split
  4:1 in 2021 and 10:1 in 2024, recorded here as corporate actions on
  unadjusted prices. AGG pays monthly, and VFIAX is a mutual fund. B's return
  must equal the value of the three adjusted series held from inception.
* **Account C** holds SPY and takes its dividends in cash. With no external
  flow, its return is ending value over starting value, from raw closes and
  dividends alone. A - C is the reinvestment.
* **The portfolio** is A + B + C.
* **Portfolio D** holds SPY reinvested as A does, with a large deposit, a sale
  and withdrawal, and a custody fee. Its reference is a day-by-day
  computation written here from the ledger it was built with: inflows count
  from the start of their day and outflows from its end (ADR 0040 §3), and
  the custody fee is added back at the bases that do not deduct it (§8).

Why the reference reads `duk` itself: `CLAUDE.md` keeps every use of fafnir
inside `providers/fafnir.py`, and that path is part of what is under test. A
reference read through the same code would check it against itself. Nothing
in `portable` gains a dependency on `duk` from this script.

What it cannot check: the reference and the ledger both read fafnir's
dividends, so wrong vendor data would be wrong in both. It tests portable's
mechanics, not the warehouse. Issue 58, against the custodian, tests both.

Nothing from fafnir is written to the repository, since vendor prices may not
be redistributed. The portfolios stay in ``--work``. Only the comparison is
printed, and written to ``--write`` when given.

Requires `duk` on PATH reaching a warehouse with fafnir migration 0033, and the
repository's environment (`scripts/bootstrap.sh`). Exits 1 when any measured
period differs from its reference by more than the tolerance, or any period
is refused.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import decimal
import io
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

D = Decimal
DAY = timedelta(days=1)
INCEPTION = date(2015, 12, 31)
CENT = D("0.01")
ZERO = D(0)
#: `pt` keeps six decimals of a quantity (portable_core.decimals.QUANTITY_PLACES).
UNIT = D("0.000001")
#: 0.01 bp. Rounding reinvested units to six decimals moves a return by at
#: most a few hundred-millionths here; anything larger is a defect.
TOLERANCE = D("0.000001")
CONTEXT = decimal.Context(prec=34, rounding=ROUND_HALF_EVEN)

TYPES = {"SPY": "etf", "NVDA": "equity", "AGG": "etf", "VFIAX": "mutual_fund"}
#: The static portfolio: each account's opening units, and whether it reinvests.
STATIC: dict[str, tuple[dict[str, Decimal], bool]] = {
    "A": ({"SPY": D(1000)}, True),
    "B": ({"NVDA": D(3000), "AGG": D(1000), "VFIAX": D(500)}, True),
    "C": ({"SPY": D(1000)}, False),
}
DEPOSIT_ON, DEPOSIT, INVEST = date(2020, 3, 23), D("100250.00"), D("100000.00")
SALE_ON, SALE_UNITS = date(2022, 1, 3), D(500)
FEE_ON, FEE = date(2023, 6, 30), D("250.00")
BASES = ("net_of_fees", "net_of_external_costs_only", "gross_of_fees", "net_of_all_costs")
#: The bases that add a custody fee (`internal_mgmt_cost`) back (ADR 0040 §8).
ADDS_BACK_CUSTODY = {"net_of_external_costs_only", "gross_of_fees"}


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


# ── fafnir, read directly ────────────────────────────────────────────────────


@dataclass
class Series:
    """One security's closes from `duk ph`, by trading day."""

    closes: dict[date, Decimal]
    days: list[date] = field(init=False)

    def __post_init__(self) -> None:
        self.days = sorted(self.closes)

    def at(self, day: date) -> Decimal:
        """The close on *day*, or on the last trading day before it."""
        index = bisect.bisect_right(self.days, day) - 1
        if index < 0:
            raise SystemExit(f"no close on or before {day}")
        return self.closes[self.days[index]]

    def before(self, day: date) -> Decimal:
        """The close on the last trading day strictly before *day*."""
        return self.at(day - DAY)


def duk(*argv: str) -> str:
    done = subprocess.run(["duk", *argv], capture_output=True, text=True, check=False)
    if done.returncode:
        raise SystemExit(f"duk {' '.join(argv)} failed: {done.stderr.strip()}")
    return done.stdout


def closes(symbol: str, through: date, *, adjusted: bool) -> Series:
    argv = [
        "ph",
        symbol,
        "--start-date",
        (INCEPTION - 10 * DAY).isoformat(),
        "--end-date",
        through.isoformat(),
        "--exact",
    ]
    if adjusted:
        argv.append("--adj")
    rows = csv.DictReader(io.StringIO(duk(*argv)))
    return Series({date.fromisoformat(row["date"]): D(row["close"]) for row in rows})


def dividends(symbol: str, through: date) -> list[tuple[date, Decimal]]:
    actions = json.loads(duk("ca", symbol, "--json"))
    return sorted(
        (date.fromisoformat(a["ex_date"]), D(str(a["dividend_amount"])))
        for a in actions
        if a["action_type"] == "dividend"
        and INCEPTION < date.fromisoformat(a["ex_date"]) <= through
    )


# ── portable, run as the owner would ─────────────────────────────────────────


class Cli:
    """`pt` and `pert` in this interpreter, JSON out, fafnir as the provider."""

    def __init__(self, work: Path) -> None:
        self.work = work
        self.env = {**os.environ, "PORTABLE_SOURCE": "fafnir", "NO_COLOR": "1"}
        self.env.pop("PORTABLE_PORT", None)

    def __call__(self, tool: str, port: Path | None, *args: object) -> dict[str, Any]:
        argv = [sys.executable, "-m", tool, "--format", "json"]
        if port is not None:
            argv += ["--port", str(port)]
        argv += [str(arg) for arg in args]
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.env,
            cwd=self.work,
            check=False,
        )
        if done.returncode:
            raise SystemExit(
                f"{tool} {' '.join(argv[4:])} exited {done.returncode}:\n"
                f"{done.stdout}\n{done.stderr}"
            )
        payload: dict[str, Any] = json.loads(done.stdout)
        return payload

    def pt(self, port: Path | None, *args: object) -> dict[str, Any]:
        return self("portable_pt", port, *args)

    def pert(self, port: Path, *args: object) -> dict[str, Any]:
        return self("portable_pert", port, *args)


def start_portfolio(cli: Cli, port: Path, name: str) -> None:
    for stale in (port, port.with_name(port.name + "-wal"), port.with_name(port.name + "-shm")):
        stale.unlink(missing_ok=True)
    cli.pt(None, "init", port, "--name", name, "--inception", INCEPTION)
    cli.pt(port, "policy", "set", "--effective-from", "2015-01-01", "--large-flow-pct", "0.10")
    for symbol, kind in TYPES.items():
        cli.pt(port, "instrument", "add", symbol, "--type", kind)


def open_account(cli: Cli, port: Path, name: str, *, fractional: bool) -> None:
    cli.pt(
        port,
        "account",
        "add",
        "--name",
        name,
        "--type",
        "taxable",
        "--opened",
        INCEPTION,
        "--relief-method",
        "fifo",
        *(["--allows-fractional"] if fractional else []),
    )
    cli.pt(
        port,
        "account",
        "tax-rates",
        "set",
        "-a",
        name,
        "--short",
        "0.37",
        "--long",
        "0.20",
        "--state",
        "0.05",
        "--niit",
        "0.038",
        "--effective-from",
        "2015-01-01",
    )


def apply_actions(
    cli: Cli,
    port: Path,
    through: date,
    raw: dict[str, Series],
    units: dict[str, dict[str, Decimal]],
    reinvests: dict[str, bool],
) -> int:
    """Record every split and dividend `pt ca sync` reports, in date order.

    A dividend is paid on its ex-date (fafnir has no pay dates). An account
    that reinvests buys at the ex-dividend reference price, the previous
    close less the dividend, which is what an adjusted series assumes.
    """
    found = cli.pt(port, "ca", "sync", "--from", INCEPTION, "--to", through)["data"]["rows"]
    events = sorted(
        found, key=lambda a: (a["ex_date"], a["action_type"] != "split", a["symbol"])
    )
    for event in events:
        symbol, on = event["symbol"], date.fromisoformat(event["ex_date"])
        if not INCEPTION < on <= through:
            continue
        if event["action_type"] == "split":
            numerator, denominator = (D(part) for part in str(event["ratio"]).split(":"))
            cli.pt(
                port,
                "ca",
                "split",
                symbol,
                "--ratio",
                f"{numerator.normalize()}:{denominator.normalize()}",
                "-d",
                on,
            )
            for held in units.values():
                if symbol in held:
                    held[symbol] = held[symbol] * numerator / denominator
            continue
        if event["action_type"] != "cash_dividend":
            raise SystemExit(f"unexpected action {event}")
        per_unit = D(event["cash_amount"])
        for account, held in units.items():
            if symbol not in held:
                continue
            amount = cents(held[symbol] * per_unit)
            args: list[object] = [
                "income",
                "dividend",
                symbol,
                "-a",
                account,
                "--amount",
                amount,
                "--ex-date",
                on,
                "--pay-date",
                on,
            ]
            if reinvests[account]:
                bought = (
                    held[symbol] * per_unit / (raw[symbol].before(on) - per_unit)
                ).quantize(UNIT, rounding=ROUND_HALF_EVEN)
                args += ["--reinvest-units", bought]
                held[symbol] += bought
            cli.pt(port, *args)
    return len(events)


def build_static(cli: Cli, port: Path, through: date, raw: dict[str, Series]) -> int:
    start_portfolio(cli, port, "Rehearsal: static")
    units: dict[str, dict[str, Decimal]] = {}
    for account, (holdings, reinvest) in STATIC.items():
        open_account(cli, port, account, fractional=reinvest)
        cost = sum((cents(n * raw[s].at(INCEPTION)) for s, n in holdings.items()), D(0))
        cli.pt(port, "cash", "deposit", "-a", account, "--amount", cost, "--date", INCEPTION)
        for symbol, count in holdings.items():
            cli.pt(
                port,
                "buy",
                symbol,
                "--qty",
                count,
                "--price",
                raw[symbol].at(INCEPTION),
                "--date",
                INCEPTION,
                "-a",
                account,
            )
        units[account] = dict(holdings)
    cli.pt(port, "price", "load", "--from", INCEPTION, "--to", through)
    found = apply_actions(
        cli, port, through, raw, units, {a: r for a, (_, r) in STATIC.items()}
    )
    cli.pt(port, "value", "--from", INCEPTION, "--to", through)
    return found


@dataclass
class Ledger:
    """What portfolio D's account held and did, as the reference reads it."""

    units: Decimal = ZERO
    cash: Decimal = ZERO
    #: Per day: (units bought or sold, cash change, inflow, outflow, fee).
    days: dict[date, list[tuple[Decimal, Decimal, Decimal, Decimal, Decimal]]] = field(
        default_factory=dict
    )

    def record(
        self,
        on: date,
        *,
        units: Decimal = ZERO,
        cash: Decimal = ZERO,
        inflow: Decimal = ZERO,
        outflow: Decimal = ZERO,
        fee: Decimal = ZERO,
    ) -> None:
        self.days.setdefault(on, []).append((units, cash, inflow, outflow, fee))
        self.units += units
        self.cash += cash


def build_flows(cli: Cli, port: Path, through: date, raw: dict[str, Series]) -> Ledger:
    start_portfolio(cli, port, "Rehearsal: flows")
    open_account(cli, port, "D", fractional=True)
    spy = raw["SPY"]
    ledger = Ledger()

    def deposit(on: date, amount: Decimal) -> None:
        cli.pt(port, "cash", "deposit", "-a", "D", "--amount", amount, "--date", on)
        ledger.record(on, cash=amount, inflow=amount)

    def buy(on: date, count: Decimal) -> None:
        cli.pt(
            port, "buy", "SPY", "--qty", count, "--price", spy.at(on), "--date", on, "-a", "D"
        )
        ledger.record(on, units=count, cash=-cents(count * spy.at(on)))

    deposit(INCEPTION, cents(D(1000) * spy.at(INCEPTION)))
    buy(INCEPTION, D(1000))
    cli.pt(port, "price", "load", "--from", INCEPTION, "--to", through)

    events: list[tuple[date, str, Decimal]] = [
        (on, "dividend", per) for on, per in dividends("SPY", through)
    ]
    events += [
        (DEPOSIT_ON, "deposit", DEPOSIT),
        (SALE_ON, "sale", SALE_UNITS),
        (FEE_ON, "fee", FEE),
    ]
    for on, kind, amount in sorted(events):
        if on > through:
            continue
        if kind == "dividend":
            bought = (ledger.units * amount / (spy.before(on) - amount)).quantize(UNIT)
            cli.pt(
                port,
                "income",
                "dividend",
                "SPY",
                "-a",
                "D",
                "--amount",
                cents(ledger.units * amount),
                "--ex-date",
                on,
                "--pay-date",
                on,
                "--reinvest-units",
                bought,
            )
            ledger.record(on, units=bought)
        elif kind == "deposit":
            deposit(on, amount)
            buy(on, (INVEST / spy.at(on)).quantize(UNIT, rounding=ROUND_DOWN))
        elif kind == "sale":
            cli.pt(
                port,
                "sell",
                "SPY",
                "--qty",
                amount,
                "--price",
                spy.at(on),
                "--date",
                on,
                "-a",
                "D",
            )
            proceeds = cents(amount * spy.at(on))
            ledger.record(on, units=-amount, cash=proceeds)
            cli.pt(port, "cash", "withdraw", "-a", "D", "--amount", proceeds, "--date", on)
            ledger.record(on, cash=-proceeds, outflow=proceeds)
        else:
            cli.pt(
                port,
                "cash",
                "fee",
                "-a",
                "D",
                "--amount",
                amount,
                "--fee-class",
                "internal_mgmt_cost",
                "-d",
                on,
            )
            ledger.record(on, cash=-amount, fee=amount)
    cli.pt(port, "value", "--from", INCEPTION, "--to", through)
    return ledger


# ── the references ───────────────────────────────────────────────────────────


Value = Callable[[date], Decimal]


def held_adjusted(
    holdings: dict[str, Decimal], raw: dict[str, Series], adjusted: dict[str, Series]
) -> Value:
    """Each holding's opening value, carried by its adjusted series."""

    def value(day: date) -> Decimal:
        with decimal.localcontext(CONTEXT):
            return sum(
                (
                    n * raw[s].at(INCEPTION) * adjusted[s].at(day) / adjusted[s].at(INCEPTION)
                    for s, n in holdings.items()
                ),
                D(0),
            )

    return value


def held_with_cash(count: Decimal, series: Series, paid: list[tuple[date, Decimal]]) -> Value:
    """Units at the raw close, plus every dividend received in cash so far."""

    def value(day: date) -> Decimal:
        received = sum((cents(count * per) for on, per in paid if on <= day), D(0))
        return count * series.at(day) + received

    return value


def ratio(value: Value) -> Callable[[date, date], Decimal]:
    """The return from the end of *after* to the end of *through*, with no flow."""

    def between(after: date, through: date) -> Decimal:
        # Funded and invested at the close of inception, the first day earns
        # nothing: a period from before it is measured from its end.
        base = max(after, INCEPTION)
        with decimal.localcontext(CONTEXT):
            return value(through) / value(base) - 1

    return between


def daily_chain(ledger: Ledger, spy: Series, through: date) -> dict[str, dict[date, Decimal]]:
    """Portfolio D's growth index per basis, day by day, from the ledger alone.

    ADR 0040 §3: r = (V_E - V_B - CF) / (V_B + inflows), an inflow taken at the
    start of its day and an outflow at its end. §8: a fee a basis does not
    deduct is undone as an outflow at the end of its day.
    """
    index = {basis: {INCEPTION - DAY: D(1)} for basis in BASES}
    units, cash, value_before = D(0), D(0), D(0)
    day = INCEPTION
    while day <= through:
        inflow = outflow = fee = D(0)
        for bought, moved, came, went, charged in ledger.days.get(day, []):
            units += bought
            cash += moved
            inflow += came
            outflow += went
            fee += charged
        value = units * spy.at(day) + cash
        with decimal.localcontext(CONTEXT):
            for basis in BASES:
                undone = fee if basis in ADDS_BACK_CUSTODY else D(0)
                growth = 1 + (value - value_before - inflow + outflow + undone) / (
                    value_before + inflow
                )
                index[basis][day] = index[basis][day - DAY] * growth
        value_before = value
        day += DAY
    return index


# ── the comparison ───────────────────────────────────────────────────────────


@dataclass
class Line:
    level: str
    label: str
    first: str
    last: str
    basis: str
    ours: Decimal | None
    reference: Decimal | None
    refused: str | None = None

    @property
    def difference(self) -> Decimal | None:
        if self.ours is None or self.reference is None:
            return None
        return self.ours - self.reference


def period_rows(
    cli: Cli, port: Path, through: date, account: str | None
) -> list[dict[str, Any]]:
    scope = ["--account", account] if account else []
    data = cli.pert(port, "periods", "--to", through, *scope)["data"]
    rows: list[dict[str, Any]] = data["summary"] + data["fiscal_years"]
    return rows


def compare(
    level: str,
    rows: Sequence[dict[str, Any]],
    bases: Sequence[str],
    reference: Callable[[str, date, date], Decimal],
) -> Iterator[Line]:
    for row in rows:
        if row["returns"] is None:
            yield Line(
                level,
                row["label"],
                row["requested"]["from"],
                row["requested"]["to"],
                "-",
                None,
                None,
                row["refusal"]["code"],
            )
            continue
        after = date.fromisoformat(row["start"]) - DAY
        last = date.fromisoformat(row["end"])
        for entry in row["returns"]:
            if entry["basis"] in bases:
                yield Line(
                    level,
                    row["label"],
                    row["start"],
                    row["end"],
                    entry["basis"],
                    D(entry["cumulative"]["value"]),
                    reference(entry["basis"], after, last),
                )


def bp(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * 10000:+.4f}"


def pct(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * 100:.4f}%"


def report(lines: Sequence[Line], through: date, actions: int) -> tuple[str, bool]:
    measured = [line for line in lines if line.difference is not None]
    worst = max(
        (abs(line.difference) for line in measured if line.difference is not None), default=D(0)
    )
    refused = [line for line in lines if line.refused]
    passed = worst <= TOLERANCE and not refused
    out = [
        "# Reconciliation rehearsal",
        "",
        f"`pert periods` through {through.isoformat()}, against references computed from "
        "fafnir directly (`scripts/rehearse_reconciliation.py`). Returns only: no price, "
        "quantity or amount from the warehouse is recorded here.",
        "",
        f"- {len(measured)} period returns compared, at {len({line.level for line in lines})} "
        f"levels, from {actions} corporate actions recorded through `pt ca sync`.",
        f"- Largest difference: {bp(worst)} bp. Tolerance: {bp(TOLERANCE)} bp.",
        f"- Refused periods: {len(refused)}.",
        f"- **{'Pass' if passed else 'FAIL'}.**",
        "",
    ]
    for level in dict.fromkeys(line.level for line in lines):
        out += [
            f"## {level}",
            "",
            "| Period | From | To | Basis | `pert` | Reference | Difference (bp) |",
            "|:---|:---|:---|:---|---:|---:|---:|",
        ]
        for line in lines:
            if line.level == level:
                out.append(
                    f"| {line.label} | {line.first} | {line.last} | {line.basis} | "
                    f"{pct(line.ours) if not line.refused else line.refused} | "
                    f"{pct(line.reference)} | {bp(line.difference)} |"
                )
        out.append("")
    return "\n".join(out) + "\n", passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--work",
        type=Path,
        required=True,
        help="Where the sample portfolios are built. Not the repository.",
    )
    parser.add_argument("--through", type=date.fromisoformat, default=date(2025, 12, 31))
    parser.add_argument("--write", type=Path, help="Also write the comparison here.")
    args = parser.parse_args()
    through: date = args.through
    args.work.mkdir(parents=True, exist_ok=True)
    cli = Cli(args.work)

    raw = {symbol: closes(symbol, through, adjusted=False) for symbol in TYPES}
    adjusted = {symbol: closes(symbol, through, adjusted=True) for symbol in TYPES}

    static = args.work / "static.port"
    actions = build_static(cli, static, through, raw)
    values: dict[str, Value] = {
        "A": held_adjusted(STATIC["A"][0], raw, adjusted),
        "B": held_adjusted(STATIC["B"][0], raw, adjusted),
        "C": held_with_cash(STATIC["C"][0]["SPY"], raw["SPY"], dividends("SPY", through)),
    }
    values["portfolio"] = lambda day: sum((values[a](day) for a in STATIC), D(0))

    lines: list[Line] = []
    for level, account in (("portfolio", None), ("A", "A"), ("B", "B"), ("C", "C")):
        between = ratio(values[level])
        lines += compare(
            f"Static portfolio: {level}",
            period_rows(cli, static, through, account),
            BASES[:1],
            lambda _basis, a, t, between=between: between(a, t),
        )

    flows = args.work / "flows.port"
    ledger = build_flows(cli, flows, through, raw)
    index = daily_chain(ledger, raw["SPY"], through)

    def chained(basis: str, after: date, last: date) -> Decimal:
        with decimal.localcontext(CONTEXT):
            return index[basis][last] / index[basis][after] - 1

    lines += compare(
        "Flows portfolio: D", period_rows(cli, flows, through, "D"), BASES, chained
    )

    text, passed = report(lines, through, actions)
    print(text)
    if args.write:
        args.write.write_text(text, encoding="utf-8", newline="\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
