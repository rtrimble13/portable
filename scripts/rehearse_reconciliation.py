#!/usr/bin/env python3
"""Rehearse issue 58 on samples priced from fafnir: is `pert` right, judged from outside?

Issue 58 reconciles `pert` to the custodian's reported performance on the
owner's portfolio. This rehearses that comparison where the right answer can
be worked out independently, on portfolios of real securities, so the engine
is checked against cases it was not written around.

It builds four portfolios through `pt`. Prices come through portable's own
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
* **The money-weighted return** (ADR 0046) of every level, since inception,
  from `pert mwr`. Its reference is solved here by plain bisection, with
  years counted by anniversary in code of this script's own. A, B and C have
  no flow after their funding, so theirs is their growth annualized. D's is
  solved from its ledger: the deposit counted from the start of its day,
  the withdrawal from its end, and at the bases that add the custody fee
  back, the fee as an outflow at the end of its day.
* **Portfolio E** rehearses the estimate tier (ADR 0044). Account E1 holds
  SPY and AGG and takes dividends in cash; E2 holds VFIAX and NVDA and
  reinvests. SPY's and VFIAX's prices are withheld from 2024-12-10 to
  2025-01-10, across the year end, and both go ex-dividend inside that
  stretch. A twin with every price loaded has the same ledger. Without
  `--estimate`, every period that does not reach the stretch must equal the
  twin's, and every one that does must be refused. With it, those must be
  estimated on 2024-12-31 alone, the one month end inside, and the rest
  unchanged. Each carried price is worked out here from the rules: SPY's
  last close before the stretch, less its dividend per unit; VFIAX's
  reinvestment, a purchase at the fund's price. Every period's return must
  equal the values computed from `duk` with those carried prices on
  2024-12-31 and real ones elsewhere.

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
import calendar
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
#: Portfolio E: SPY and VFIAX go unpriced across a year end (ADR 0044).
WITHHELD: dict[str, tuple[dict[str, Decimal], bool]] = {
    "E1": ({"SPY": D(49), "AGG": D(370)}, False),
    "E2": ({"VFIAX": D(215), "NVDA": D(30)}, True),
}
WITHHOLD = ("SPY", "VFIAX")
WITHHELD_FROM, WITHHELD_TO = date(2024, 12, 10), date(2025, 1, 10)
#: The one month end inside the withheld days: the one day an estimate needs.
ESTIMATED_ON = date(2024, 12, 31)
#: Well above either account's carried share. The limit is not under test.
LIMIT = D("0.5")
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


def splits(symbol: str, through: date) -> list[tuple[date, Decimal]]:
    """Each split's ex-date and the units it gives per unit held."""
    actions = json.loads(duk("ca", symbol, "--json"))
    return sorted(
        (
            date.fromisoformat(a["ex_date"]),
            D(str(a["split_numerator"])) / D(str(a["split_denominator"])),
        )
        for a in actions
        if a["action_type"] == "split"
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


def fund_accounts(
    cli: Cli,
    port: Path,
    accounts: dict[str, tuple[dict[str, Decimal], bool]],
    raw: dict[str, Series],
) -> dict[str, dict[str, Decimal]]:
    """Open each account, fund it with the cost of its holdings, and buy them at inception."""
    units: dict[str, dict[str, Decimal]] = {}
    for account, (holdings, reinvest) in accounts.items():
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
    return units


def build_static(cli: Cli, port: Path, through: date, raw: dict[str, Series]) -> int:
    start_portfolio(cli, port, "Rehearsal: static")
    units = fund_accounts(cli, port, STATIC, raw)
    cli.pt(port, "price", "load", "--from", INCEPTION, "--to", through)
    found = apply_actions(
        cli, port, through, raw, units, {a: r for a, (_, r) in STATIC.items()}
    )
    cli.pt(port, "value", "--from", INCEPTION, "--to", through)
    return found


def build_withheld(
    cli: Cli, port: Path, through: date, raw: dict[str, Series], *, withhold: bool
) -> None:
    """Portfolio E, with SPY's and VFIAX's prices withheld or, for the twin, not."""
    start_portfolio(cli, port, "Rehearsal: withheld" if withhold else "Rehearsal: priced twin")
    units = fund_accounts(cli, port, WITHHELD, raw)
    if not withhold:
        cli.pt(port, "price", "load", "--from", INCEPTION, "--to", through)
    else:
        kept = [s for holdings, _ in WITHHELD.values() for s in holdings if s not in WITHHOLD]
        cli.pt(port, "price", "load", *kept, "--from", INCEPTION, "--to", through)
        for first, last in ((INCEPTION, WITHHELD_FROM - DAY), (WITHHELD_TO + DAY, through)):
            cli.pt(port, "price", "load", *WITHHOLD, "--from", first, "--to", last)
        for symbol in WITHHOLD:
            shown = cli.pt(
                port, "price", "show", symbol, "--from", WITHHELD_FROM, "--to", WITHHELD_TO
            )
            if shown["data"]["rows"]:
                raise SystemExit(
                    f"{symbol} has prices in the withheld stretch: {shown['data']}"
                )
    apply_actions(cli, port, through, raw, units, {a: r for a, (_, r) in WITHHELD.items()})
    cli.pt(port, "value", "--from", INCEPTION, "--to", through)


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


@dataclass
class Reinvested:
    """A holding reinvesting every dividend, unit for unit as `apply_actions` records it."""

    #: (first day, units held from it), in order.
    units: list[tuple[date, Decimal]]
    #: (ex-date, amount over units bought): the price each reinvestment paid.
    prices: list[tuple[date, Decimal]]

    def held(self, day: date) -> Decimal:
        return [count for first, count in self.units if first <= day][-1]


def reinvested(
    count: Decimal,
    series: Series,
    paid: list[tuple[date, Decimal]],
    split: Sequence[tuple[date, Decimal]] = (),
) -> Reinvested:
    """In date order, a split before a dividend on its day, as `apply_actions` takes them."""
    held = count
    units, prices = [(INCEPTION, count)], []
    events = sorted(
        [(on, 0, ratio) for on, ratio in split] + [(on, 1, per) for on, per in paid]
    )
    for on, kind, per in events:
        if kind == 0:
            held = held * per
            units.append((on, held))
            continue
        amount = cents(held * per)
        bought = (held * per / (series.before(on) - per)).quantize(
            UNIT, rounding=ROUND_HALF_EVEN
        )
        held += bought
        units.append((on, held))
        with decimal.localcontext(CONTEXT):
            prices.append((on, amount / bought))
    return Reinvested(units, prices)


@dataclass
class Carried:
    """A price ADR 0044 §1 carries to ESTIMATED_ON, worked out from `duk` here."""

    symbol: str
    quantity: Decimal
    price: Decimal
    price_date: date
    source: str
    #: The price the holding is worth on the day, had it been loaded.
    actual: Decimal


def withheld_references(
    raw: dict[str, Series], through: date
) -> tuple[dict[str, Value], dict[str, Carried]]:
    """Portfolio E's value each day, the carried prices on ESTIMATED_ON in place.

    E1's SPY: its last close before the stretch, lowered by the dividend that
    went ex inside it, per unit entitled. E2's VFIAX: the price its December
    reinvestment paid, a purchase at the fund's price, newer than any close
    before the stretch, and with no distribution after it to lower it.

    E2's units are followed as `apply_actions` records them, split by split
    and reinvestment by reinvestment, rather than read off the adjusted
    series, and each holding is valued to the cent, as a snapshot is. Off
    the adjusted series, or unrounded, thirty NVDA shares drift from the
    ledger by thousandths of a basis point, and the estimate is checked
    closer than that.
    """
    on = ESTIMATED_ON
    e1, e2 = WITHHELD["E1"][0], WITHHELD["E2"][0]
    paid = {s: dividends(s, through) for s in ("SPY", "AGG", "VFIAX")}

    spy = raw["SPY"]
    last = spy.days[bisect.bisect_right(spy.days, WITHHELD_FROM - DAY) - 1]
    with decimal.localcontext(CONTEXT):
        lowered = sum(
            (cents(e1["SPY"] * per) / e1["SPY"] for ex, per in paid["SPY"] if last < ex <= on),
            D(0),
        )
    fund = reinvested(e2["VFIAX"], raw["VFIAX"], paid["VFIAX"])
    nvda = reinvested(
        e2["NVDA"], raw["NVDA"], dividends("NVDA", through), splits("NVDA", through)
    )
    bought_on, price = [(ex, p) for ex, p in fund.prices if ex <= on][-1]
    assert bought_on > WITHHELD_FROM, "VFIAX's reinvestment should fall in the stretch"
    with decimal.localcontext(CONTEXT):
        carried = {
            "E1": Carried("SPY", e1["SPY"], spy.closes[last] - lowered, last, "price",
                          spy.at(on)),
            "E2": Carried("VFIAX", fund.held(on), price, bought_on, "reinvestment",
                          raw["VFIAX"].at(on)),
        }  # fmt: skip

    def account_value(name: str, value: Value) -> Value:
        def estimated(day: date) -> Decimal:
            if day != on:
                return value(day)
            c = carried[name]
            with decimal.localcontext(CONTEXT):
                return value(day) + c.quantity * (c.price - c.actual)

        return estimated

    def e1_value(day: date) -> Decimal:
        return sum((held_with_cash(n, raw[s], paid[s])(day) for s, n in e1.items()), D(0))

    def e2_value(day: date) -> Decimal:
        # A snapshot holds each holding's value to the cent (ADR 0005).
        with decimal.localcontext(CONTEXT):
            return cents(fund.held(day) * raw["VFIAX"].at(day)) + cents(
                nvda.held(day) * raw["NVDA"].at(day)
            )

    values: dict[str, Value] = {
        "E1": account_value("E1", e1_value),
        "E2": account_value("E2", e2_value),
    }
    values["portfolio"] = lambda day: values["E1"](day) + values["E2"](day)
    return values, carried


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


def anniversary_years(after: date, on: date) -> Decimal:
    """Whole anniversaries from *after*, then the days left over of the year they fall in.

    Written here rather than imported, so the reference does not share
    `portable`'s year count (ADR 0045 §5).
    """

    def shifted(day: date, count: int) -> date:
        year = day.year + count
        if day.month == 2 and day.day == 29 and not calendar.isleap(year):
            return date(year, 2, 28)
        return day.replace(year=year)

    whole = on.year - after.year
    while whole > 0 and shifted(after, whole) > on:
        whole -= 1
    base, following = shifted(after, whole), shifted(after, whole + 1)
    with decimal.localcontext(CONTEXT):
        return D(whole) + D((on - base).days) / D((following - base).days)


def irr(after: date, amounts: Sequence[tuple[date, Decimal]]) -> Decimal:
    """The annual rate at which money in (positive) and out (negative) balance, by bisection.

    Each amount is at the end of its day. A plain search on 1 + R between 0.01
    and 10, which holds any portfolio here, so nothing of `portable`'s solver
    is reused.
    """
    with decimal.localcontext(CONTEXT):
        timed = [(anniversary_years(after, on), amount) for on, amount in amounts]

        def value(growth: Decimal) -> Decimal:
            return sum((a * (-t * growth.ln()).exp() for t, a in timed), D(0))

        low, high = D("0.01"), D(10)
        for _ in range(250):
            mid = (low + high) / 2
            if (value(low) > 0) == (value(mid) > 0):
                low = mid
            else:
                high = mid
        return mid - 1


def d_mwr_amounts(
    ledger: Ledger, spy: Series, through: date, basis: str
) -> list[tuple[date, Decimal]]:
    """Portfolio D's money in and out, as ADR 0040 §3 and §8 time it."""
    amounts: list[tuple[date, Decimal]] = []
    for on, rows in sorted(ledger.days.items()):
        for _units, _cash, came, went, charged in rows:
            if came:
                amounts.append((on - DAY, came))
            if went:
                amounts.append((on, -went))
            if charged and basis in ADDS_BACK_CUSTODY:
                amounts.append((on, -charged))
    amounts.append((through, -(ledger.units * spy.at(through) + ledger.cash)))
    return amounts


def mwr_lines(
    cli: Cli,
    port: Path,
    through: date,
    account: str | None,
    level: str,
    bases: Sequence[str],
    reference: Callable[[str, date], Decimal],
) -> Iterator[Line]:
    """`pert mwr` since inception, beside the reference, per basis."""
    scope = ["--account", account] if account else []
    data = cli.pert(port, "mwr", "--to", through, *scope)["data"]
    after = date.fromisoformat(data["period"]["start"]) - DAY
    for entry in data["returns"]:
        if entry["basis"] in bases:
            yield Line(
                f"Money-weighted: {level}",
                "Since inception, annualized",
                data["period"]["start"],
                data["period"]["end"],
                entry["basis"],
                D(entry["mwr"]["value"]),
                reference(entry["basis"], after),
            )


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
    cli: Cli, port: Path, through: date, account: str | None, *extra: object
) -> list[dict[str, Any]]:
    scope = ["--account", account] if account else []
    data = cli.pert(port, "periods", "--to", through, *scope, *extra)["data"]
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


def rehearse_estimates(
    cli: Cli, work: Path, through: date, raw: dict[str, Series]
) -> tuple[list[Line], list[str], list[str]]:
    """Portfolio E against its twin and its reference: (lines, failures, findings)."""
    twin, withheld = work / "withheld-twin.port", work / "withheld.port"
    build_withheld(cli, twin, through, raw, withhold=False)
    build_withheld(cli, withheld, through, raw, withhold=True)
    values, carried = withheld_references(raw, through)

    lines: list[Line] = []
    failures: list[str] = []
    refused = estimated = unchanged = 0
    peak = D(0)
    for level, account in (("portfolio", None), ("E1", "E1"), ("E2", "E2")):
        between = ratio(values[level])
        priced = period_rows(cli, twin, through, account)
        plain = period_rows(cli, withheld, through, account)
        flagged = period_rows(cli, withheld, through, account, "--estimate", LIMIT)
        for twin_row, plain_row, row in zip(priced, plain, flagged, strict=True):
            name = f"{level} {row['label']}"
            if twin_row["returns"] is None:
                # Outside the record, priced or not.
                if plain_row["returns"] is not None or row["returns"] is not None:
                    failures.append(f"{name}: measured here, refused in the twin")
                continue
            after = date.fromisoformat(twin_row["start"]) - DAY
            last = date.fromisoformat(twin_row["end"])
            if not after <= ESTIMATED_ON <= last:
                unchanged += 1
                if plain_row["returns"] != twin_row["returns"]:
                    failures.append(f"{name}: changed by prices it does not reach")
                if (row["returns"], row["estimate"]) != (plain_row["returns"], None):
                    failures.append(f"{name}: changed by --estimate, with nothing to estimate")
            else:
                refused += plain_row["refusal"] is not None
                if (plain_row["refusal"] or {}).get("code") != "PT-E-GIPS-VALUATION-GAP":
                    failures.append(f"{name}: not refused without --estimate")
                estimate = row["estimate"]
                days = [day["date"] for day in estimate["days"]] if estimate else []
                if days != [ESTIMATED_ON.isoformat()]:
                    failures.append(f"{name}: estimated on {days or 'no day'}")
                    continue
                estimated += 1
                (day,) = estimate["days"]
                peak = max(peak, D(day["share"]))
                expected = [
                    carried[a] for a in (("E1", "E2") if account is None else (account,))
                ]
                seen = [
                    (h["symbol"], D(h["quantity"]), D(h["price"]), h["price_date"], h["source"])
                    for h in day["holdings"]
                ]
                wanted = [
                    (c.symbol, c.quantity, c.price, c.price_date.isoformat(), c.source)
                    for c in expected
                ]
                if seen != wanted:
                    failures.append(f"{name}: carried {seen}, the rules give {wanted}")
            for entry in row["returns"][:1]:
                lines.append(
                    Line(
                        f"Withheld, with --estimate: {level}",
                        row["label"],
                        row["start"],
                        row["end"],
                        entry["basis"],
                        D(entry["cumulative"]["value"]),
                        between(after, last),
                    )
                )
    findings = [
        f"{unchanged} periods that do not reach the withheld days are the twin's, with and "
        "without `--estimate`.",
        f"{refused} periods that do are refused without it, and {estimated} are estimated "
        f"with it, on {ESTIMATED_ON.isoformat()} alone. The largest share carried was "
        f"{peak * 100:.2f}% of a day's value, against a limit of {LIMIT * 100:.0f}%.",
    ]
    return lines, failures, findings


def bp(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * 10000:+.4f}"


def pct(value: Decimal | None) -> str:
    return "—" if value is None else f"{value * 100:.4f}%"


def report(
    lines: Sequence[Line],
    through: date,
    actions: int,
    failures: Sequence[str] = (),
    findings: Sequence[str] = (),
) -> tuple[str, bool]:
    measured = [line for line in lines if line.difference is not None]
    worst = max(
        (abs(line.difference) for line in measured if line.difference is not None), default=D(0)
    )
    refused = [line for line in lines if line.refused]
    passed = worst <= TOLERANCE and not refused and not failures
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
        *(f"- {finding}" for finding in findings),
        *(f"- FAILED: {failure}" for failure in failures),
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

    for level, account in (("portfolio", None), ("A", "A"), ("B", "B"), ("C", "C")):
        # Funded at the close of inception and never again: the deposit counts
        # from the start of that day, and the rate is the growth annualized.
        value = values[level]
        lines += mwr_lines(
            cli,
            static,
            through,
            account,
            f"static {level}",
            BASES[:1],
            lambda _basis, after, value=value: irr(
                after, [(after, value(INCEPTION)), (through, -value(through))]
            ),
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
    lines += mwr_lines(
        cli,
        flows,
        through,
        "D",
        "D",
        BASES,
        lambda basis, after: irr(after, d_mwr_amounts(ledger, raw["SPY"], through, basis)),
    )

    estimated, failures, findings = rehearse_estimates(cli, args.work, through, raw)
    lines += estimated

    text, passed = report(lines, through, actions, failures, findings)
    print(text)
    if args.write:
        args.write.write_text(text, encoding="utf-8", newline="\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
