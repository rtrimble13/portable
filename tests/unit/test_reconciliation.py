"""Reconciliation: per account, including cash.

`docs/broker-import.md` §9 makes this the acceptance criterion for an import.
Parser tests establish that an adapter does what it claims; only this
establishes that what it claims is right.

The two tests that matter most are the ones the previous implementation could
not have passed by construction: offsetting errors across two accounts, and a
cash break with every share count correct.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from portable_core.domain.enums import InstrumentType, TransactionType
from portable_core.domain.models import Account, Instrument
from portable_core.errors import ValidationError
from portable_core.persistence.repositories import Repositories
from portable_core.services.reconciliation import ExternalHolding, ReconciliationService
from portable_core.services.replay import ReplayEngine
from portable_core.services.trading import TradeIntent, TradingService
from tests.conftest import append

pytestmark = pytest.mark.unit

D = Decimal
TOL = D("0.01")
ON = date(2024, 1, 10)


def _fund_and_buy(
    repos: Repositories, account: Account, instrument: Instrument, qty: str
) -> None:
    append(
        repos,
        account.account_id,
        TransactionType.DEPOSIT,
        date(2024, 1, 2),
        net_cash_effect=D("100000.00"),
    )
    ReplayEngine(repos).rebuild()
    service = TradingService(repos)
    service.commit(
        service.plan(
            TradeIntent(
                account=account,
                instrument=instrument,
                txn_type=TransactionType.BUY,
                quantity=D(qty),
                price=D("100.00"),
                trade_date=ON,
            )
        )
    )


def _held(account: str, symbol: str, amount: str) -> ExternalHolding:
    return ExternalHolding(account=account, identifier=symbol, amount=D(amount))


def _cash(account: str, amount: str) -> ExternalHolding:
    return ExternalHolding(
        account=account, identifier="SWEEP", amount=D(amount), is_cash_equivalent=True
    )


# ── the two failures the old command could not see ───────────────────────────


def test_offsetting_errors_across_accounts_are_breaks_not_a_clean_total(
    repos: Repositories, taxable_account: Account, ira_account: Account, aapl: Instrument
) -> None:
    """Summing accounts into one namespace hides exactly this.

    140 shares are held across two accounts and the statement also says 140 --
    but in the wrong proportions. A portfolio-wide comparison balances; a
    per-account one reports two breaks, which is the truth.
    """
    _fund_and_buy(repos, taxable_account, aapl, "100")
    _fund_and_buy(repos, ira_account, aapl, "40")

    outcome = ReconciliationService(repos).reconcile(
        [
            _held(taxable_account.name, "AAPL", "110"),
            _held(ira_account.name, "AAPL", "30"),
            _cash(taxable_account.name, "90000.00"),
            _cash(ira_account.name, "96000.00"),
        ],
        [taxable_account, ira_account],
        tolerance=TOL,
    )

    position_breaks = [b for b in outcome.breaks if b.kind == "position"]
    assert len(position_breaks) == 2
    assert {b.account for b in position_breaks} == {taxable_account.name, ira_account.name}
    # And the two differences do cancel, which is what made this invisible.
    assert sum(b.difference for b in position_breaks) == 0


def test_a_cash_break_is_reported_when_every_quantity_agrees(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """The failure mode cash reconciliation exists for.

    A sign error, a dropped row, or a double-counted transfer all leave the
    share counts right and the money wrong. Comparing quantities alone passes
    on every one of them.
    """
    _fund_and_buy(repos, taxable_account, aapl, "100")

    outcome = ReconciliationService(repos).reconcile(
        [_held(taxable_account.name, "AAPL", "100"), _cash(taxable_account.name, "85000.00")],
        [taxable_account],
        tolerance=TOL,
    )

    assert [b.kind for b in outcome.breaks] == ["cash"]
    assert outcome.breaks[0].ours == D("90000.00")
    assert outcome.breaks[0].theirs == D("85000.00")


# ── cash ─────────────────────────────────────────────────────────────────────


def test_sweep_positions_are_folded_into_the_custodian_cash_line(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """ADR 0013: a custodian reports its sweep as a position; portable holds cash.

    This is the one place that decision has to be undone, and doing it here is
    what keeps a thousand sweep rows out of the ledger.
    """
    _fund_and_buy(repos, taxable_account, aapl, "100")

    outcome = ReconciliationService(repos).reconcile(
        [
            _held(taxable_account.name, "AAPL", "100"),
            _cash(taxable_account.name, "50000.00"),
            _cash(taxable_account.name, "40000.00"),
        ],
        [taxable_account],
        tolerance=TOL,
    )
    assert outcome.breaks == ()
    cash_line = next(line for line in outcome.lines if line.kind == "cash")
    assert cash_line.theirs == D("90000.00")
    # A sweep vehicle is never compared as a position.
    assert all(line.identifier != "SWEEP" for line in outcome.lines if line.kind == "position")


def test_a_cash_line_is_always_rendered_even_when_the_statement_is_silent(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """ "They agree" and "nobody checked" must not look the same."""
    _fund_and_buy(repos, taxable_account, aapl, "100")
    outcome = ReconciliationService(repos).reconcile(
        [_held(taxable_account.name, "AAPL", "100")], [taxable_account], tolerance=TOL
    )
    cash_line = next(line for line in outcome.lines if line.kind == "cash")
    assert cash_line.theirs == D("0.00")
    assert cash_line.is_break is True


def test_a_margin_loan_nets_against_cash(repos: Repositories, taxable_account: Account) -> None:
    """A statement presents a margin balance as negative cash, and so does this.

    `valuation_snapshot` composes market value the same way: securities plus
    cash less the loan.
    """
    repos.valuations.set_cash(
        taxable_account.account_id, D("10000.00"), currency=taxable_account.currency
    )
    repos.con.execute(
        "UPDATE cash_balance SET margin_loan = '4000.00' WHERE account_id = ?",
        (taxable_account.account_id,),
    )
    outcome = ReconciliationService(repos).reconcile(
        [_cash(taxable_account.name, "6000.00")], [taxable_account], tolerance=TOL
    )
    assert outcome.breaks == ()


def test_cash_from_a_flag_and_from_the_file_is_refused(
    repos: Repositories, taxable_account: Account
) -> None:
    """Adding them would double the balance; picking one would hide the other."""
    with pytest.raises(ValidationError):
        ReconciliationService(repos).reconcile(
            [_cash(taxable_account.name, "1000.00")],
            [taxable_account],
            tolerance=TOL,
            cash_override={taxable_account.name: D("1000.00")},
        )


# ── identifying what the statement names ─────────────────────────────────────


def test_a_holding_stated_by_cusip_resolves(
    repos: Repositories, taxable_account: Account
) -> None:
    """Custodians identify by CUSIP as readily as by ticker."""
    instrument_id = repos.instruments.add(
        Instrument(
            instrument_id=0,
            symbol="MSFT",
            instrument_type=InstrumentType.EQUITY,
            cusip="594918104",
        )
    )
    instrument = repos.instruments.get(instrument_id)
    assert instrument is not None
    _fund_and_buy(repos, taxable_account, instrument, "50")

    outcome = ReconciliationService(repos).reconcile(
        [
            ExternalHolding(taxable_account.name, "594918104", D("50")),
            _cash(taxable_account.name, "95000.00"),
        ],
        [taxable_account],
        tolerance=TOL,
    )
    assert outcome.breaks == ()
    assert any(line.identifier == "MSFT" for line in outcome.lines)


def test_an_unidentifiable_holding_is_reported_not_raised(
    repos: Repositories, taxable_account: Account
) -> None:
    """One unknown line must not abandon the whole comparison.

    Reporting it as a break is the honest answer: portable does not hold it
    under any name it knows, and the reader needs to see that rather than an
    exception.
    """
    outcome = ReconciliationService(repos).reconcile(
        [ExternalHolding(taxable_account.name, "NOPE", D("10"))],
        [taxable_account],
        tolerance=TOL,
    )
    line = next(line for line in outcome.lines if line.identifier == "NOPE")
    assert (line.ours, line.theirs, line.is_break) == (D("0.00"), D("10"), True)


# ── attribution ──────────────────────────────────────────────────────────────


def test_rows_without_an_account_are_refused_when_several_are_reconciled(
    repos: Repositories, taxable_account: Account, ira_account: Account
) -> None:
    """Guessing would put a holding in the wrong account and still balance."""
    with pytest.raises(ValidationError):
        ReconciliationService(repos).reconcile(
            [ExternalHolding(None, "AAPL", D("100"))],
            [taxable_account, ira_account],
            tolerance=TOL,
        )


def test_rows_without_an_account_are_fine_when_one_is_reconciled(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    _fund_and_buy(repos, taxable_account, aapl, "100")
    outcome = ReconciliationService(repos).reconcile(
        [
            ExternalHolding(None, "AAPL", D("100")),
            ExternalHolding(None, "SWEEP", D("90000.00"), is_cash_equivalent=True),
        ],
        [taxable_account],
        tolerance=TOL,
    )
    assert outcome.breaks == ()


def test_a_statement_naming_an_unreconciled_account_is_refused(
    repos: Repositories, taxable_account: Account
) -> None:
    """Ignoring those lines would report a clean result on a partly-read file."""
    with pytest.raises(ValidationError):
        ReconciliationService(repos).reconcile(
            [ExternalHolding("Somewhere Else", "AAPL", D("100"))],
            [taxable_account],
            tolerance=TOL,
        )


def test_tolerance_admits_sub_share_rounding(
    repos: Repositories, taxable_account: Account, aapl: Instrument
) -> None:
    """Real statements disagree in the third decimal; that is not a break."""
    _fund_and_buy(repos, taxable_account, aapl, "100")
    outcome = ReconciliationService(repos).reconcile(
        [
            _held(taxable_account.name, "AAPL", "99.995"),
            _cash(taxable_account.name, "90000.00"),
        ],
        [taxable_account],
        tolerance=TOL,
    )
    assert outcome.breaks == ()
