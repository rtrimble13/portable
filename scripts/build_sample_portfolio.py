#!/usr/bin/env python3
"""Generate `examples/sample.port` from a scripted transaction sequence.

**Generated, not hand-crafted** (bootstrap §9). A hand-made fixture drifts from
what the code actually produces and quietly stops testing anything; a generated
one is rebuilt by `make fixtures` and fails loudly when the engines change.

The corpus spans three accounts and several years and deliberately includes
every path that is easy to get wrong:

* a **split** mid-holding-period, followed by a sale, so the holding period
  can be checked not to have reset;
* a **spinoff** with an explicit fair-market-value allocation;
* a **covered call** written and assigned, so the premium can be checked to
  land in the stock's proceeds;
* a **bond bought between coupons**, so accrued interest can be checked to be
  part of market value;
* a **large cash flow**, and an **inter-account transfer**, so the
  account-vs-portfolio flow distinction can be checked;
* a **wrong trade, reversed, and re-entered**, so the audit trail can be
  checked to show all three;
* a taxable account, a tax-deferred account, and one with a margin loan.

Everything is dated deterministically. There is no randomness and no clock
dependence, so two runs produce byte-identical files -- which is what lets the
fixture be committed and diffed.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from portable_core import __version__  # noqa: E402
from portable_core.decimals import quantize_money  # noqa: E402
from portable_core.domain.enums import (  # noqa: E402
    AccountType,
    DayCount,
    ExerciseStyle,
    FeeClass,
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
from portable_core.services.replay import ReplayEngine  # noqa: E402

D = Decimal
ZERO = D("0.00")

#: Fixed so the generated file is byte-identical across runs. `created_at` is
#: the only wall-clock field in the ledger, and pinning it is what lets the
#: fixture be committed and diffed.
STAMP = "2026-08-27T00:00:00Z"

INCEPTION = date(2021, 1, 4)


def _txn(repos: Repositories, **fields: object) -> int:
    """Append one ledger row, defaulting the fields the fixture never varies.

    `source` is defaulted rather than forced, so a caller can mark a row
    `derived` -- which the corporate-action rows are, because portable produced
    them rather than a human entering them.
    """
    trade_date = fields["trade_date"]
    assert isinstance(trade_date, date)
    fields.setdefault("source", TransactionSource.MANUAL)
    return repos.transactions.append(
        Transaction(
            txn_id=0,
            seq=repos.transactions.next_seq(trade_date),
            created_at=STAMP,
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


def _price(repos: Repositories, instrument_id: int, on: date, value: str) -> None:
    repos.prices.add(
        Price(
            instrument_id=instrument_id,
            price_date=on,
            price=D(value),
            source="file:sample_prices.csv",
            as_of=datetime.combine(on, datetime.min.time(), tzinfo=UTC),
            valuation_level=1,
        )
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

    con = open_portfolio(path, must_exist=False)
    M.initialise(con)
    repos = Repositories(con)

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
            "created_at": STAMP,
            "updated_at": STAMP,
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
                    first_coupon_date=date(2021, 11, 15),
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
        _buy(repos, taxable, treasury, date(2021, 8, 20), "50", "1012.40")

        # Bought between coupons: accrued interest is part of market value and
        # is NOT basis (PORT-GIPS-A06).
        _txn(
            repos,
            account_id=taxable,
            trade_date=date(2021, 11, 15),
            txn_type=TransactionType.COUPON,
            instrument_id=treasury,
            gross_amount=D("1062.50"),
            pay_date=date(2021, 11, 15),
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

        # A covered call written against the AAPL position, then assigned.
        # The premium must land in the stock's PROCEEDS, not as separate P&L.
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

        # ── 2024: a split, and a spinoff ─────────────────────────────────────
        # 100 ACME becomes 200; total basis unchanged, holding period NOT reset.
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
                "relative FMV 48.00/12.00; NEWCO inherits ACME's holding period."
            ),
        )
        repos.corporate_actions.add(
            CorporateAction(
                instrument_id=acme,
                action_type="spinoff",
                ex_date=date(2024, 9, 3),
                target_instrument_id=newco,
                target_ratio=D("0.25"),
                parent_fmv=D("48.00"),
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
            trade_date=date(2025, 5, 15),
            txn_type=TransactionType.COUPON,
            instrument_id=treasury,
            gross_amount=D("1062.50"),
            pay_date=date(2025, 5, 15),
            net_cash_effect=D("1062.50"),
        )

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
                        # Reinvested: no cash, and a new lot.
                        _txn(
                            repos,
                            account_id=account_id,
                            trade_date=pay,
                            txn_type=TransactionType.DIVIDEND_REINVEST,
                            instrument_id=vti,
                            quantity=quantize_money(amount / D("240.00")),
                            price=D("240.00"),
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
        # Month-end marks across the history, so a valuation can be built for
        # any month end without a provider.
        marks = {
            aapl: [
                ("2021-12-31", "177.57"),
                ("2022-12-30", "129.93"),
                ("2023-12-29", "192.53"),
                ("2024-12-31", "250.42"),
                ("2025-06-30", "205.17"),
                ("2025-12-31", "228.60"),
            ],
            vti: [
                ("2021-12-31", "241.05"),
                ("2022-12-30", "196.35"),
                ("2023-12-29", "236.35"),
                ("2024-12-31", "291.13"),
                ("2025-06-30", "297.44"),
                ("2025-12-31", "312.05"),
            ],
            acme: [
                ("2021-12-31", "47.10"),
                ("2022-12-30", "41.85"),
                ("2023-12-29", "52.40"),
                ("2024-12-31", "27.55"),
                ("2025-06-30", "29.80"),
                ("2025-12-31", "31.20"),
            ],
            newco: [("2024-12-31", "13.10"), ("2025-06-30", "14.85"), ("2025-12-31", "15.40")],
            treasury: [
                ("2021-12-31", "1008.20"),
                ("2022-12-30", "941.50"),
                ("2023-12-29", "978.30"),
                ("2024-12-31", "965.10"),
                ("2025-06-30", "982.75"),
                ("2025-12-31", "991.40"),
            ],
            covered_call: [("2023-12-29", "9.10")],
        }
        for instrument_id, series in marks.items():
            for on, value in series:
                _price(repos, instrument_id, date.fromisoformat(on), value)

        counts["transactions"] = repos.transactions.count()

    with transaction(con):
        replay = ReplayEngine(repos).rebuild()

    counts |= {
        "accounts": len(repos.accounts.all()),
        "instruments": len(repos.instruments.all()),
        "positions": replay.positions_created,
        "lots": replay.lots_created,
        "dispositions": replay.dispositions_created,
    }
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "examples" / "sample.port")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    counts = build(args.out, force=args.force)

    print(f"wrote {args.out}")
    for key in sorted(counts):
        print(f"  {key}: {counts[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
