#!/usr/bin/env python3
"""Generate `examples/sample.port` from a scripted transaction sequence.

**Generated, not hand-crafted** (`CLAUDE.md`). A hand-made fixture drifts from
what the code actually produces and quietly stops testing anything; a generated
one is rebuilt by `make fixtures` and fails loudly when the engines change.

The corpus spans three accounts and several years and deliberately includes
every path that is easy to get wrong:

* a **split** mid-holding-period, followed by a sale, so the holding period
  can be checked not to have reset;
* a **spinoff** with an explicit fair-market-value allocation;
* a **covered call** written and left to expire worthless, so the premium can
  be checked to be short-term gain however long it was open, and the written
  option's cash to be a receipt (ADR 0038);
* a **bond bought between coupons**, so accrued interest can be checked to be
  part of market value;
* a **large cash flow**, and an **inter-account transfer**, so the
  account-vs-portfolio flow distinction can be checked;
* a **wrong trade, reversed, and re-entered**, so the audit trail can be
  checked to show all three;
* a taxable account, a tax-deferred account, and one with a margin loan.

It is priced and valued, so that `pert twr` runs on it with nothing run first:
a price on every day a return needs one (``PRICE_RUNS`` says which and why),
written to ``sample_prices.csv`` beside it as well, and a stored snapshot for
every account and every day (issue 59).

Everything is dated deterministically. There is no randomness, and the clock
every row is stamped with is a fixed one handed to the repositories and to the
migration runner (ADR 0035). Freed space is zeroed whatever the SQLite library
was compiled with. So two runs produce byte-identical files, which is what lets
the fixture be committed and diffed. The one exception is the header field in
which SQLite records its own version.
`tests/integration/test_sample_fixture.py` holds two builds to one another, and
the committed file to a fresh build.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from portable_core import __version__  # noqa: E402
from portable_core.clock import FixedClock  # noqa: E402
from portable_core.config.settings import DEFAULTS  # noqa: E402
from portable_core.decimals import quantize_money  # noqa: E402
from portable_core.domain.enums import (  # noqa: E402
    AccountType,
    DayCount,
    ExerciseStyle,
    FeeClass,
    FlowLevel,
    InstrumentType,
    OptionRight,
    ReliefMethod,
    TransactionSource,
    TransactionType,
)
from portable_core.domain.models import (  # noqa: E402
    Account,
    BondDetail,
    CorporateAction,
    Instrument,
    OptionDetail,
    Price,
    ReturnPolicy,
    TaxRateSchedule,
    Transaction,
)
from portable_core.persistence.connection import open_portfolio, transaction  # noqa: E402
from portable_core.persistence.repositories import Repositories  # noqa: E402
from portable_core.schema import migrations as M  # noqa: E402
from portable_core.services import snapshots  # noqa: E402
from portable_core.services.cash_flow import classify  # noqa: E402
from portable_core.services.replay import ReplayEngine  # noqa: E402

D = Decimal
ZERO = D("0.00")

#: The moment every row in the fixture claims to have been written. Fixed, so
#: that two runs write the same bytes. It reaches every table through
#: one clock, handed to the repositories and to the migration runner (ADR 0035):
#: the ledger's `created_at`, but also the policy, account, instrument, tax-rate
#: and corporate-action rows, and each migration's `applied_at`.
STAMP = datetime(2026, 8, 27, tzinfo=UTC)
CLOCK = FixedClock(STAMP)

INCEPTION = date(2021, 1, 4)

#: The last day the fixture values. The ledger's last row is dated here.
LAST_DAY = date(2025, 12, 31)

# ── the price path ───────────────────────────────────────────────────────────
#
# `pert twr` needs a complete valuation at every month end, at every external
# cash flow, and either side of every corporate action (ADR 0040 §6,
# PORT-GIPS-A03, B03). So the fixture prices every instrument it holds on each
# of those days, and nowhere else: the days between are left unpriced on
# purpose, so that a return over the sample bridges them with Modified Dietz
# as well as linking daily sub-periods.
#
# Each instrument's path is a set of anchors: the price of every trade the
# ledger below records, and a handful of marks. Between two anchors the price
# moves in a straight line in calendar days, rounded half-even to the quoted
# precision. That is all it is -- no randomness -- and it means a return over
# the sample can be worked out by hand from `examples/sample_prices.csv`.
#
# A path is a list of RUNS. A corporate action is a jump, not a move: the
# price on the day before the ex-date is the cum price and the price on the
# ex-date the ex price, so a run ends on the first and the next begins on the
# second, and asking for a price between them is refused. Prices are
# unadjusted, as portfolio accounting requires (`CLAUDE.md`, "Adjusted vs.
# unadjusted prices"): ACME halves at its split rather than its history being
# halved.
PRICE_RUNS: dict[str, tuple[Decimal, list[list[tuple[str, str]]]]] = {
    "AAPL": (
        D("0.01"),
        [
            [
                ("2021-01-11", "128.98"),  # bought, taxable
                ("2021-01-12", "128.10"),  # bought, Roth
                ("2021-12-31", "177.57"),
                ("2022-12-30", "129.93"),
                # Below the 200 strike on 2024-01-19, so the call expires.
                ("2023-12-29", "192.53"),
                ("2024-02-20", "181.56"),  # bought, IRA
                ("2024-12-31", "250.42"),
                ("2025-05-20", "211.26"),  # sold, taxable
                ("2025-06-30", "205.17"),
                ("2025-12-31", "228.60"),
            ]
        ],
    ),
    "VTI": (
        D("0.01"),
        [
            [
                ("2021-01-11", "196.40"),  # bought, taxable
                ("2021-01-12", "197.11"),  # bought, IRA
                ("2021-12-31", "241.05"),
                ("2022-12-30", "196.35"),
                ("2023-03-03", "205.90"),  # bought, taxable
                ("2023-06-16", "213.15"),  # bought, Roth
                ("2023-12-29", "236.35"),
                ("2024-07-10", "268.44"),  # sold, taxable
                ("2024-12-31", "291.13"),
                ("2025-06-10", "289.11"),  # sold, IRA
                ("2025-06-30", "297.44"),
                ("2025-12-31", "312.05"),
            ]
        ],
    ),
    "ACME": (
        D("0.01"),
        [
            [
                ("2021-03-15", "42.30"),  # bought, taxable
                ("2021-12-31", "47.10"),
                ("2022-02-14", "38.10"),  # the wrong sale, reversed
                ("2022-02-15", "38.10"),  # sold, taxable
                ("2022-12-30", "41.85"),
                ("2023-12-29", "52.40"),
                ("2024-03-28", "54.00"),  # cum split (03-29 is Good Friday)
            ],
            [
                ("2024-04-01", "27.00"),  # ex split, 2-for-1: exactly half
                ("2024-08-30", "27.40"),  # cum spinoff: 24.40 + 0.25 x 12.00
            ],
            [
                ("2024-09-03", "24.40"),  # ex spinoff: the parent FMV recorded
                ("2024-12-31", "27.55"),
                ("2025-06-30", "29.80"),
                ("2025-12-31", "31.20"),
            ],
        ],
    ),
    "NEWCO": (
        D("0.01"),
        [
            [
                ("2024-09-03", "12.00"),  # first day: the spun FMV recorded
                ("2024-12-31", "13.10"),
                ("2025-06-30", "14.85"),
                ("2025-12-31", "15.40"),
            ]
        ],
    ),
    # Percent of par (ADR 0041): 100.82 is 1,008.20 per 1,000 of face.
    "T-4.25-2030": (
        D("0.001"),
        [
            [
                ("2021-08-20", "101.240"),  # bought, taxable
                ("2021-12-31", "100.820"),
                ("2022-12-30", "94.150"),
                ("2023-12-29", "97.830"),
                ("2024-12-31", "96.510"),
                ("2025-06-30", "98.275"),
                ("2025-12-31", "99.140"),
            ]
        ],
    ),
    "AAPL  240119C00200000": (
        D("0.01"),
        [
            [
                ("2023-09-15", "8.45"),  # written, taxable
                ("2023-12-29", "9.10"),
                ("2024-01-19", "0.00"),  # expired worthless
            ]
        ],
    ),
}

#: Days the NYSE was closed, 2021-2025. A price is never dated on one, so a
#: month end that falls on one is valued from the trading day before it.
NYSE_HOLIDAYS = frozenset(
    date.fromisoformat(day)
    for day in (
        # 2021
        "2021-01-01",
        "2021-01-18",
        "2021-02-15",
        "2021-04-02",
        "2021-05-31",
        "2021-07-05",
        "2021-09-06",
        "2021-11-25",
        "2021-12-24",
        # 2022
        "2022-01-17",
        "2022-02-21",
        "2022-04-15",
        "2022-05-30",
        "2022-06-20",
        "2022-07-04",
        "2022-09-05",
        "2022-11-24",
        "2022-12-26",
        # 2023
        "2023-01-02",
        "2023-01-16",
        "2023-02-20",
        "2023-04-07",
        "2023-05-29",
        "2023-06-19",
        "2023-07-04",
        "2023-09-04",
        "2023-11-23",
        "2023-12-25",
        # 2024
        "2024-01-01",
        "2024-01-15",
        "2024-02-19",
        "2024-03-29",
        "2024-05-27",
        "2024-06-19",
        "2024-07-04",
        "2024-09-02",
        "2024-11-28",
        "2024-12-25",
        # 2025
        "2025-01-01",
        "2025-01-09",
        "2025-01-20",
        "2025-02-17",
        "2025-04-18",
        "2025-05-26",
        "2025-06-19",
        "2025-07-04",
        "2025-09-01",
        "2025-11-27",
        "2025-12-25",
    )
)


def _trading_day_on_or_before(on: date) -> date:
    while on.weekday() >= 5 or on in NYSE_HOLIDAYS:
        on -= timedelta(days=1)
    return on


def _month_ends(first: date, last: date) -> list[date]:
    """The last trading day of every month from *first*'s to *last*'s."""
    ends: list[date] = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        following = date(year + month // 12, month % 12 + 1, 1)
        ends.append(_trading_day_on_or_before(following - timedelta(days=1)))
        year, month = following.year, following.month
    return ends


def _mark(symbol: str, on: date) -> Decimal | None:
    """*symbol*'s price on *on* along its path, or None outside it.

    Raises:
        SystemExit: *on* falls between two runs -- between a cum and an ex
            price -- where the path has no price, and inventing one would put
            a corporate action's jump on the wrong day.
    """
    places, runs = PRICE_RUNS[symbol]
    anchors = [[(date.fromisoformat(d), D(p)) for d, p in run] for run in runs]
    if on < anchors[0][0][0] or on > anchors[-1][-1][0]:
        return None
    for run in anchors:
        for (d0, p0), (d1, p1) in pairwise(run):
            if d0 <= on <= d1:
                elapsed = D((on - d0).days) / D((d1 - d0).days)
                return (p0 + (p1 - p0) * elapsed).quantize(places, rounding=ROUND_HALF_EVEN)
    raise SystemExit(f"{symbol} has no price on {on}: it falls between a cum and an ex price")


def _txn(repos: Repositories, **fields: object) -> int:
    """Append one ledger row, defaulting the fields the fixture never varies.

    `source` is defaulted rather than forced, so a caller can mark a row
    `derived` -- which the corporate-action rows are, because portable produced
    them rather than a human entering them. `created_at` is left to the
    repository, which reads it from the fixture's clock.
    """
    trade_date = fields["trade_date"]
    assert isinstance(trade_date, date)
    fields.setdefault("source", TransactionSource.MANUAL)
    return repos.transactions.append(
        Transaction(
            txn_id=0,
            seq=repos.transactions.next_seq(trade_date),
            **fields,  # type: ignore[arg-type]
        )
    )


def _buy(
    repos: Repositories,
    account_id: int,
    instrument_id: int,
    on: date,
    qty: str,
    price: str,
    *,
    fees: str = "1.00",
) -> int:
    gross = quantize_money(D(qty) * D(price))
    return _txn(
        repos,
        account_id=account_id,
        trade_date=on,
        settlement_date=on + timedelta(days=1),
        txn_type=TransactionType.BUY,
        instrument_id=instrument_id,
        quantity=D(qty),
        price=D(price),
        gross_amount=gross,
        fees=D(fees),
        fee_class=FeeClass.TRANSACTION_COST,
        net_cash_effect=quantize_money(-(gross + D(fees))),
    )


def _sell(
    repos: Repositories,
    account_id: int,
    instrument_id: int,
    on: date,
    qty: str,
    price: str,
    *,
    fees: str = "1.00",
    method: ReliefMethod | None = None,
    lots: str | None = None,
) -> int:
    gross = quantize_money(D(qty) * D(price))
    return _txn(
        repos,
        account_id=account_id,
        trade_date=on,
        settlement_date=on + timedelta(days=1),
        txn_type=TransactionType.SELL,
        instrument_id=instrument_id,
        quantity=D(qty),
        price=D(price),
        gross_amount=gross,
        fees=D(fees),
        fee_class=FeeClass.TRANSACTION_COST,
        relief_method=method,
        lot_selection=lots,
        net_cash_effect=quantize_money(gross - D(fees)),
    )


def _dividend(
    repos: Repositories,
    account_id: int,
    instrument_id: int,
    ex: date,
    pay: date,
    amount: str,
    *,
    qualified: bool = True,
) -> int:
    return _txn(
        repos,
        account_id=account_id,
        trade_date=pay,
        txn_type=TransactionType.DIVIDEND,
        instrument_id=instrument_id,
        gross_amount=D(amount),
        ex_date=ex,
        pay_date=pay,
        is_qualified=qualified,
        net_cash_effect=D(amount),
    )


def _price(repos: Repositories, instrument_id: int, on: date, value: Decimal) -> None:
    repos.prices.add(
        Price(
            instrument_id=instrument_id,
            price_date=on,
            price=value,
            source="file:sample_prices.csv",
            as_of=datetime.combine(on, datetime.min.time(), tzinfo=UTC),
            valuation_level=1,
        )
    )


def _write_prices_csv(path: Path, rows: list[tuple[str, date, Decimal]]) -> None:
    """The prices the sample holds, as a file `pt price load` reads.

    Written by the generator, never by hand, so that it cannot disagree with
    the prices in `examples/sample.port`. LF line endings on every platform,
    so that a build on Windows writes the same bytes.
    """
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["symbol", "date", "price", "source", "valuation_level"])
        for symbol, on, value in sorted(rows):
            writer.writerow([symbol, on.isoformat(), str(value), "example", 1])


def build(path: Path, *, force: bool = False, prices_csv: Path | None = None) -> dict[str, int]:
    if path.exists():
        if not force:
            raise SystemExit(f"{path} exists. Pass --force to regenerate.")
        path.unlink()
        for suffix in ("-wal", "-shm"):
            extra = path.with_name(path.name + suffix)
            if extra.exists():
                extra.unlink()

    con = open_portfolio(path, must_exist=False)
    # Zero what a delete frees, whatever this SQLite was compiled with. The
    # migrations that rebuild a table free pages. Ubuntu's SQLite is compiled
    # to zero them and other builds are not, and the old bytes those builds
    # leave behind were the whole difference between their files (ADR 0035).
    con.execute("PRAGMA secure_delete = ON")
    M.initialise(con, clock=CLOCK)
    repos = Repositories(con, clock=CLOCK)

    with transaction(con):
        for key, value in {
            "portfolio_name": "Sample Portfolio",
            "description": (
                "A generated demonstration portfolio: three accounts, several "
                "years, options, a bond, a split, a spinoff, a large cash flow, "
                "an inter-account transfer, and a reversed trade."
            ),
            "inception_date": INCEPTION.isoformat(),
            "base_currency": "USD",
            "fiscal_year_end": "12-31",
            "schema_version": str(M.CURRENT_SCHEMA_VERSION),
            "created_at": CLOCK.stamp(),
            "updated_at": CLOCK.stamp(),
            "portable_version": __version__,
        }.items():
            repos.meta.set(key, value)

        # GIPS requires the entity to define the large-cash-flow threshold and
        # supplies no number (PORT-GIPS-B03). The fixture defines one so that
        # `pert` has something to consume, and so the refusal path is tested
        # against a portfolio that deliberately lacks one instead.
        repos.policies.add(
            ReturnPolicy(
                policy_id=0,
                effective_from=INCEPTION,
                large_flow_basis="percent",
                large_flow_value=D("0.10"),
                materiality_return_bps=D("5"),
                note="10% of beginning market value.",
            )
        )

        # ── accounts ─────────────────────────────────────────────────────────
        taxable = repos.accounts.add(
            Account(
                account_id=0,
                name="Taxable Brokerage",
                account_type=AccountType.TAXABLE,
                opened_date=INCEPTION,
                custodian="Example Broker",
                account_alias="****1234",
                default_relief_method=ReliefMethod.FIFO,
            )
        )
        ira = repos.accounts.add(
            Account(
                account_id=0,
                name="Rollover IRA",
                account_type=AccountType.TAX_DEFERRED,
                opened_date=INCEPTION,
                custodian="Example Broker",
                default_relief_method=ReliefMethod.FIFO,
            )
        )
        roth = repos.accounts.add(
            Account(
                account_id=0,
                name="Roth IRA",
                account_type=AccountType.TAX_EXEMPT,
                opened_date=INCEPTION,
                custodian="Example Broker",
                default_relief_method=ReliefMethod.FIFO,
            )
        )

        # Effective-dated, and deliberately CHANGING mid-history, so that the
        # "a rate change never restates a past disposition" property has
        # something to be tested against.
        repos.accounts.add_rate_schedule(
            TaxRateSchedule(
                rate_id=0,
                account_id=taxable,
                effective_from=INCEPTION,
                short_term_federal=D("0.32"),
                long_term_federal=D("0.15"),
                state=D("0.05"),
                niit=D("0.038"),
            )
        )
        repos.accounts.add_rate_schedule(
            TaxRateSchedule(
                rate_id=0,
                account_id=taxable,
                effective_from=date(2023, 1, 1),
                short_term_federal=D("0.37"),
                long_term_federal=D("0.20"),
                state=D("0.05"),
                niit=D("0.038"),
                note="Moved into the top bracket.",
            )
        )

        # ── instruments ──────────────────────────────────────────────────────
        aapl = repos.instruments.add(
            Instrument(
                instrument_id=0,
                symbol="AAPL",
                instrument_type=InstrumentType.EQUITY,
                name="Apple Inc.",
                exchange="NASDAQ",
                sector="Technology",
                asset_class="us_equity",
                cusip="037833100",
            )
        )
        vti = repos.instruments.add(
            Instrument(
                instrument_id=0,
                symbol="VTI",
                instrument_type=InstrumentType.ETF,
                name="Vanguard Total Stock Market ETF",
                exchange="NYSEARCA",
                asset_class="us_equity",
            )
        )
        acme = repos.instruments.add(
            Instrument(
                instrument_id=0,
                symbol="ACME",
                instrument_type=InstrumentType.EQUITY,
                name="Acme Industrials",
                exchange="NYSE",
                sector="Industrials",
                asset_class="us_equity",
            )
        )
        newco = repos.instruments.add(
            Instrument(
                instrument_id=0,
                symbol="NEWCO",
                instrument_type=InstrumentType.EQUITY,
                name="Newco Holdings (spun from ACME)",
                exchange="NYSE",
                sector="Industrials",
                asset_class="us_equity",
                source="derived:spinoff",
            )
        )
        treasury = repos.instruments.add(
            Instrument(
                instrument_id=0,
                symbol="T-4.25-2030",
                instrument_type=InstrumentType.BOND,
                name="US Treasury 4.25% 2030",
                asset_class="us_treasury",
                bond=BondDetail(
                    issuer="US Treasury",
                    coupon_rate=D("0.0425"),
                    coupon_frequency=2,
                    maturity_date=date(2030, 5, 15),
                    day_count=DayCount.ACT_ACT,
                    face_value=D("1000"),
                    quote_basis="percent_of_par",
                ),
            )
        )
        covered_call = repos.instruments.add(
            Instrument(
                instrument_id=0,
                symbol="AAPL  240119C00200000",
                instrument_type=InstrumentType.OPTION,
                name="AAPL Jan-2024 200 Call",
                option=OptionDetail(
                    underlier_instrument_id=aapl,
                    option_right=OptionRight.CALL,
                    strike=D("200"),
                    expiry=date(2024, 1, 19),
                    multiplier=D("100"),
                    occ_symbol="AAPL  240119C00200000",
                    exercise_style=ExerciseStyle.AMERICAN,
                ),
            )
        )

        counts = {"transactions": 0}

        # ── 2021: funding and first purchases ────────────────────────────────
        _txn(
            repos,
            account_id=taxable,
            trade_date=INCEPTION,
            txn_type=TransactionType.DEPOSIT,
            net_cash_effect=D("250000.00"),
            note="Initial funding.",
        )
        _txn(
            repos,
            account_id=ira,
            trade_date=INCEPTION,
            txn_type=TransactionType.DEPOSIT,
            net_cash_effect=D("180000.00"),
            note="Rollover from a former employer plan.",
        )
        _txn(
            repos,
            account_id=roth,
            trade_date=INCEPTION,
            txn_type=TransactionType.DEPOSIT,
            net_cash_effect=D("40000.00"),
        )

        _buy(repos, taxable, aapl, date(2021, 1, 11), "400", "128.98")
        _buy(repos, taxable, vti, date(2021, 1, 11), "300", "196.40")
        _buy(repos, ira, vti, date(2021, 1, 12), "600", "197.11")
        _buy(repos, roth, aapl, date(2021, 1, 12), "150", "128.10")
        _buy(repos, taxable, acme, date(2021, 3, 15), "500", "42.30")
        # $50,000 of face at 101.24 percent of par, bought between coupons.
        # The seller is paid the interest accrued from the 05-15 coupon to
        # settlement on 08-21: 1,062.50 x 98 / 184 = 565.92. That is cash, and
        # NOT basis (ADR 0041, PORT-GIPS-A06).
        _txn(
            repos,
            account_id=taxable,
            trade_date=date(2021, 8, 20),
            settlement_date=date(2021, 8, 21),
            txn_type=TransactionType.BUY,
            instrument_id=treasury,
            quantity=D("50000"),
            price=D("101.24"),
            gross_amount=D("50620.00"),
            fees=D("1.00"),
            fee_class=FeeClass.TRANSACTION_COST,
            accrued_interest=D("-565.92"),
            net_cash_effect=D("-51186.92"),
        )
        # Every coupon it was owed, each paid on its scheduled date. An
        # unrecorded one would leave every valuation after it incomplete.
        for paid in [date(year, month, 15) for year in range(2021, 2026) for month in (5, 11)]:
            if date(2021, 11, 15) <= paid <= date(2025, 11, 15):
                _txn(
                    repos,
                    account_id=taxable,
                    trade_date=paid,
                    txn_type=TransactionType.COUPON,
                    instrument_id=treasury,
                    gross_amount=D("1062.50"),
                    ex_date=paid,
                    pay_date=paid,
                    net_cash_effect=D("1062.50"),
                    note="Semi-annual coupon.",
                )

        for ex, pay, amount in [
            (date(2021, 2, 5), date(2021, 2, 11), "82.00"),
            (date(2021, 5, 7), date(2021, 5, 13), "88.00"),
            (date(2021, 8, 6), date(2021, 8, 12), "88.00"),
            (date(2021, 11, 5), date(2021, 11, 11), "88.00"),
        ]:
            _dividend(repos, taxable, aapl, ex, pay, amount)

        # ── 2022: a wrong trade, reversed, and re-entered ────────────────────
        # The audit trail must show all three, and current state must be right.
        wrong = _sell(
            repos, taxable, acme, date(2022, 2, 14), "500", "38.10", method=ReliefMethod.FIFO
        )
        _txn(
            repos,
            account_id=taxable,
            trade_date=date(2022, 2, 15),
            txn_type=TransactionType.REVERSAL,
            instrument_id=acme,
            quantity=D("500"),
            price=D("38.10"),
            gross_amount=D("-19050.00"),
            reverses_txn_id=wrong,
            net_cash_effect=D("-19049.00"),
            note=f"Reverses txn {wrong}: wrong quantity entered.",
        )
        _sell(repos, taxable, acme, date(2022, 2, 15), "200", "38.10", method=ReliefMethod.FIFO)

        _txn(
            repos,
            account_id=taxable,
            trade_date=date(2022, 6, 30),
            txn_type=TransactionType.FEE,
            gross_amount=D("120.00"),
            fees=D("120.00"),
            fee_class=FeeClass.INTERNAL_MGMT_COST,
            net_cash_effect=D("-120.00"),
            note="Annual custody fee -- reduces net-of-fees only (PORT-GIPS-D01).",
        )

        for ex, pay, amount in [
            (date(2022, 2, 4), date(2022, 2, 10), "92.00"),
            (date(2022, 5, 6), date(2022, 5, 12), "92.00"),
            (date(2022, 8, 5), date(2022, 8, 11), "92.00"),
            (date(2022, 11, 4), date(2022, 11, 10), "92.00"),
        ]:
            _dividend(repos, taxable, aapl, ex, pay, amount)

        # ── 2023: a large cash flow and an inter-account transfer ────────────
        _txn(
            repos,
            account_id=taxable,
            trade_date=date(2023, 3, 1),
            txn_type=TransactionType.DEPOSIT,
            net_cash_effect=D("120000.00"),
            note="Large cash flow: above the 10% policy threshold.",
        )
        _buy(repos, taxable, vti, date(2023, 3, 3), "400", "205.90")

        # ONE row with a counter account. At portfolio level this nets to zero
        # and yields NO flow -- not two flows that cancel (ADR 0007).
        _txn(
            repos,
            account_id=taxable,
            trade_date=date(2023, 6, 15),
            txn_type=TransactionType.TRANSFER,
            counter_account_id=roth,
            net_cash_effect=D("-6500.00"),
            note="Annual Roth contribution, funded from the brokerage.",
        )
        _buy(repos, roth, vti, date(2023, 6, 16), "30", "213.15")

        for ex, pay, amount in [
            (date(2023, 2, 10), date(2023, 2, 16), "96.00"),
            (date(2023, 5, 12), date(2023, 5, 18), "96.00"),
            (date(2023, 8, 11), date(2023, 8, 17), "96.00"),
            (date(2023, 11, 10), date(2023, 11, 16), "96.00"),
        ]:
            _dividend(repos, taxable, aapl, ex, pay, amount)

        # A covered call written against the AAPL position. Writing it RECEIVES
        # the premium, less the commission (ADR 0038).
        _txn(
            repos,
            account_id=taxable,
            trade_date=date(2023, 9, 15),
            txn_type=TransactionType.SELL_SHORT,
            instrument_id=covered_call,
            quantity=D("1"),
            price=D("8.45"),
            gross_amount=D("845.00"),
            fees=D("0.65"),
            fee_class=FeeClass.TRANSACTION_COST,
            net_cash_effect=D("844.35"),
            note="Covered call written against the AAPL position.",
        )

        # ── 2024: the call expires, a split, and a spinoff ───────────────────
        # AAPL closed below the 200 strike on the expiry date, so the call
        # lapses: its 844.35 premium is short-term gain, however long it was
        # open. The row is an event on the option; replay derives the rest.
        _txn(
            repos,
            account_id=taxable,
            trade_date=date(2024, 1, 19),
            txn_type=TransactionType.OPTION_EXPIRATION,
            instrument_id=covered_call,
            quantity=D("1"),
            net_cash_effect=ZERO,
            source=TransactionSource.DERIVED,
            note="expired worthless",
        )

        # 300 ACME becomes 600; total basis unchanged, holding period NOT reset.
        # Each corporate action is TWO records: the ledger entry saying it
        # happened, and the corporate_action row carrying the parameters. The
        # second is what lets `pt rebuild` reproduce the effect rather than
        # silently reverting it (ADR 0010).
        split_txn = _txn(
            repos,
            account_id=taxable,
            trade_date=date(2024, 4, 1),
            txn_type=TransactionType.SPLIT,
            instrument_id=acme,
            quantity=D("600"),
            ex_date=date(2024, 4, 1),
            net_cash_effect=ZERO,
            source=TransactionSource.DERIVED,
            note="2-for-1 split. Basis unchanged; holding period NOT reset.",
        )
        repos.corporate_actions.add(
            CorporateAction(
                instrument_id=acme,
                action_type="split",
                ex_date=date(2024, 4, 1),
                split_numerator=D("2"),
                split_denominator=D("1"),
                applied_txn_id=split_txn,
            )
        )

        spinoff_txn = _txn(
            repos,
            account_id=taxable,
            trade_date=date(2024, 9, 3),
            txn_type=TransactionType.SPINOFF,
            instrument_id=acme,
            ex_date=date(2024, 9, 3),
            net_cash_effect=ZERO,
            source=TransactionSource.DERIVED,
            note=(
                "Spinoff of NEWCO, 0.25 per ACME share. Basis allocated by "
                "relative FMV 24.40/12.00; NEWCO inherits ACME's holding period."
            ),
        )
        repos.corporate_actions.add(
            CorporateAction(
                instrument_id=acme,
                action_type="spinoff",
                ex_date=date(2024, 9, 3),
                target_instrument_id=newco,
                target_ratio=D("0.25"),
                # The ex-date prices of both, which the price path opens at.
                parent_fmv=D("24.40"),
                target_fmv=D("12.00"),
                applied_txn_id=spinoff_txn,
            )
        )

        _buy(repos, ira, aapl, date(2024, 2, 20), "100", "181.56")
        _sell(repos, taxable, vti, date(2024, 7, 10), "200", "268.44", method=ReliefMethod.FIFO)

        for ex, pay, amount in [
            (date(2024, 2, 9), date(2024, 2, 15), "96.00"),
            (date(2024, 5, 10), date(2024, 5, 16), "100.00"),
            (date(2024, 8, 12), date(2024, 8, 19), "100.00"),
            (date(2024, 11, 8), date(2024, 11, 14), "100.00"),
        ]:
            _dividend(repos, taxable, aapl, ex, pay, amount)

        # ── 2025: margin, a partial sale with spec-ID, and income ────────────
        _txn(
            repos,
            account_id=taxable,
            trade_date=date(2025, 2, 3),
            txn_type=TransactionType.MARGIN_INTEREST,
            gross_amount=D("312.50"),
            fees=D("312.50"),
            fee_class=FeeClass.INTERNAL_MGMT_COST,
            net_cash_effect=D("-312.50"),
            note="Margin interest -- a financing cost, not a fee.",
        )

        _sell(
            repos, taxable, aapl, date(2025, 5, 20), "100", "211.26", method=ReliefMethod.FIFO
        )
        _sell(repos, ira, vti, date(2025, 6, 10), "150", "289.11", method=ReliefMethod.FIFO)

        _txn(
            repos,
            account_id=taxable,
            trade_date=date(2025, 8, 1),
            txn_type=TransactionType.RETURN_OF_CAPITAL,
            instrument_id=vti,
            gross_amount=D("150.00"),
            pay_date=date(2025, 8, 1),
            net_cash_effect=D("150.00"),
            note="Return of capital: reduces basis, not income; not a flow.",
        )

        for ex, pay, amount in [
            (date(2025, 2, 10), date(2025, 2, 13), "100.00"),
            (date(2025, 5, 12), date(2025, 5, 15), "104.00"),
        ]:
            _dividend(repos, taxable, aapl, ex, pay, amount)

        _txn(
            repos,
            account_id=taxable,
            trade_date=date(2025, 9, 30),
            txn_type=TransactionType.WITHDRAWAL,
            net_cash_effect=D("-25000.00"),
            note="Distribution to the owner.",
        )

        # ── recurring activity across the whole history ─────────────────────
        # VTI distributes quarterly in every account, and the Roth reinvests
        # rather than taking cash. Both paths matter: a reinvested dividend is
        # still INCOME and still not an external cash flow (PORT-GIPS-B02),
        # while also opening a new lot with its own holding period.
        vti_distributions = [
            (
                2021,
                [("03-24", "0.60"), ("06-24", "0.68"), ("09-24", "0.71"), ("12-22", "0.80")],
            ),
            (
                2022,
                [("03-24", "0.71"), ("06-27", "0.76"), ("09-27", "0.77"), ("12-22", "0.84")],
            ),
            (
                2023,
                [("03-23", "0.76"), ("06-23", "0.82"), ("09-22", "0.84"), ("12-21", "0.95")],
            ),
            (
                2024,
                [("03-21", "0.87"), ("06-24", "0.91"), ("09-23", "0.94"), ("12-23", "1.05")],
            ),
            (2025, [("03-24", "0.94"), ("06-23", "0.98")]),
        ]
        holdings_by_account = {taxable: D("300"), ira: D("600"), roth: D("30")}

        for year, quarters in vti_distributions:
            for day, per_share in quarters:
                ex = date.fromisoformat(f"{year}-{day}")
                pay = ex + timedelta(days=5)
                for account_id, shares in holdings_by_account.items():
                    # The Roth only holds VTI from mid-2023.
                    if account_id == roth and ex < date(2023, 6, 16):
                        continue
                    amount = quantize_money(D(per_share) * shares)
                    if account_id == roth:
                        # Reinvested: no cash, and a new lot, bought at the
                        # day's price along VTI's path. At any other price the
                        # new shares would be worth more or less than the
                        # dividend that bought them, and the difference would
                        # show as return.
                        reinvest_at = _mark("VTI", pay)
                        assert reinvest_at is not None
                        _txn(
                            repos,
                            account_id=account_id,
                            trade_date=pay,
                            txn_type=TransactionType.DIVIDEND_REINVEST,
                            instrument_id=vti,
                            quantity=quantize_money(amount / reinvest_at),
                            price=reinvest_at,
                            gross_amount=amount,
                            ex_date=ex,
                            pay_date=pay,
                            is_qualified=True,
                            net_cash_effect=ZERO,
                            note="Dividend reinvested.",
                        )
                    else:
                        _dividend(repos, account_id, vti, ex, pay, str(amount))

        # A modest, regular contribution to the IRA, so the flow series has
        # something periodic in it as well as the one large flow.
        for year in (2022, 2023, 2024, 2025):
            _txn(
                repos,
                account_id=ira,
                trade_date=date(year, 1, 15),
                txn_type=TransactionType.DEPOSIT,
                net_cash_effect=D("7000.00"),
                note="Annual contribution.",
            )
            _txn(
                repos,
                account_id=ira,
                trade_date=date(year, 12, 31),
                txn_type=TransactionType.INTEREST,
                gross_amount=D("42.15"),
                net_cash_effect=D("42.15"),
                note="Sweep interest -- income, never an external flow.",
            )

        # ── prices ───────────────────────────────────────────────────────────
        # Every instrument held, on every day a return needs a value (see
        # PRICE_RUNS), and only on those days.
        ids = {
            "AAPL": aapl,
            "VTI": vti,
            "ACME": acme,
            "NEWCO": newco,
            "T-4.25-2030": treasury,
            "AAPL  240119C00200000": covered_call,
        }
        assert set(ids) == set(PRICE_RUNS), "every instrument has a path, and only those"
        symbol_of = {instrument_id: symbol for symbol, instrument_id in ids.items()}
        ledger = repos.transactions.in_ledger_order()

        # The path and the ledger must agree: every row that states a price
        # traded at the path's price that day. Otherwise the day's value and
        # the day's trade disagree, and the difference shows as return.
        for txn in ledger:
            if txn.price is None or txn.instrument_id is None:
                continue
            on_path = _mark(symbol_of[txn.instrument_id], txn.trade_date)
            if on_path != txn.price:
                raise SystemExit(
                    f"txn {txn.txn_id} trades {symbol_of[txn.instrument_id]} at "
                    f"{txn.price} on {txn.trade_date}; its price path says {on_path}"
                )

        valued = set(_month_ends(INCEPTION, LAST_DAY))
        for txn in ledger:
            # The one classifier (ADR 0007). Valued on the day of every
            # external flow and the day before it, so a large flow always has
            # a value either side (PORT-GIPS-B03): an inflow counts at the
            # start of its day, so it needs the close of the day before.
            flow = classify(txn, FlowLevel.ACCOUNT)
            if flow.is_external:
                valued.add(_trading_day_on_or_before(flow.flow_date))
                valued.add(_trading_day_on_or_before(flow.flow_date - timedelta(days=1)))
        for action in repos.corporate_actions.all():
            # The cum price and the ex price, on consecutive trading days.
            assert _trading_day_on_or_before(action.ex_date) == action.ex_date
            valued.add(action.ex_date)
            valued.add(_trading_day_on_or_before(action.ex_date - timedelta(days=1)))

        price_rows: list[tuple[str, date, Decimal]] = []
        for on in sorted(valued):
            for symbol, instrument_id in ids.items():
                value = _mark(symbol, on)
                if value is not None:
                    _price(repos, instrument_id, on, value)
                    price_rows.append((symbol, on, value))

        counts["transactions"] = repos.transactions.count()

    with transaction(con):
        replay = ReplayEngine(repos).rebuild()

    # Stored snapshots, every account, every day, as `pt value --from
    # INCEPTION --to LAST_DAY` would store them (ADR 0024), so that `pert twr`
    # runs on the committed file with no step before it. The days between
    # valuation days are stored too, incomplete, because a missing snapshot is
    # refused where an incomplete one is bridged (ADR 0040 §2, §6).
    with transaction(con):
        built, warnings = snapshots.value_range(
            repos,
            repos.accounts.all(),
            INCEPTION,
            LAST_DAY,
            staleness_tolerance_days=int(DEFAULTS["staleness_tolerance_days"]),
        )
        warnings.extend(snapshots.store(repos, built))
    if warnings:
        raise SystemExit("valuing the sample warned:\n" + "\n".join(warnings))
    # Every account that has begun is completely valued on every valuation day.
    incomplete = [
        f"{snapshot.account_id} on {snapshot.snapshot_date}"
        for snapshot in built
        if snapshot.snapshot_date in valued and not snapshot.is_complete
    ]
    if incomplete:
        raise SystemExit(f"incomplete on a valuation day: {incomplete}")

    if prices_csv is not None:
        _write_prices_csv(prices_csv, price_rows)

    counts |= {
        "accounts": len(repos.accounts.all()),
        "instruments": len(repos.instruments.all()),
        "positions": replay.positions_created,
        "lots": replay.lots_created,
        "dispositions": replay.dispositions_created,
        "prices": len(price_rows),
        "snapshots": len(built),
    }
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "examples" / "sample.port")
    parser.add_argument(
        "--prices",
        type=Path,
        default=None,
        help="Where to write the price file. Default: sample_prices.csv beside --out.",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    prices = args.prices or args.out.with_name("sample_prices.csv")
    counts = build(args.out, force=args.force, prices_csv=prices)

    print(f"wrote {args.out}")
    print(f"wrote {prices}")
    for key in sorted(counts):
        print(f"  {key}: {counts[key]}")
    # Recorded in the file's header too, the one field a build on another
    # library is expected to change (ADR 0035).
    print(f"  written by SQLite {sqlite3.sqlite_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
