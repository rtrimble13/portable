"""The four mapping primitives the reference custodian needed and the first
grammar lacked -- and which are custodian-neutral, so they live in the engine.

ADR 0018 §4 promised that a custodian with plain tabular exports is two
mapping files and no Python. Measured against the reference custodian in
`docs/broker-import.md` §12 that promise had four holes, each of which would
have forced either per-custodian Python or a per-row edit of the batch:

- transaction rows carry a security *name* and no symbol (the crosswalk);
- one activity word means several events, told apart by the note (note keys);
- sweep bookkeeping is a third of the file and must be dropped by a rule that
  cannot also swallow a real movement (the cash-equivalent whitelist);
- both legs of an internal transfer are reported, once per account (pairing).

Each test here is one of those holes, closed. The refusals matter as much as
the happy paths: every one of these primitives is a place where a plausible
wrong number gets in, and the grammar's job is to make the mapping author say
what they mean.
"""

from __future__ import annotations

import textwrap
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from portable_core.domain.enums import TransactionType
from portable_core.errors import ValidationError
from portable_core.importers import (
    ImportCapability,
    Sign,
    TabularAdapter,
    load_activity_map,
    load_crosswalk,
)

pytestmark = pytest.mark.unit

SOURCE = """
broker = "acme"

[format]
dates = ["%m/%d/%Y"]

[cash_equivalents]
default = ["SWEEP"]

[documents.holdings]
file = "holdings.csv"
[documents.holdings.columns]
as_of = "As Of"
account = "Account"
identifier = "Symbol"
quantity = "Quantity"

[documents.transactions]
file = "transactions.csv"
[documents.transactions.columns]
trade_date = "Trade Date"
account = "Account"
activity = "Activity"
identifier = "Symbol"
quantity = "Quantity"
amount = "Amount"
note = "Description"
"""

ACTIVITY = """
[[activity]]
match = "Bought"
txn_type = "buy"
quantity = "positive"
cash = "negative"
"""

HOLDINGS = """\
As Of,Account,Symbol,Quantity
03/31/2026,Main,AAPL,100
03/31/2026,Main,SWEEP,2500.00
03/31/2026,IRA,SWEEP,100.00
"""

TRANSACTIONS = """\
Trade Date,Account,Activity,Symbol,Quantity,Amount,Description
01/06/2026,Main,Bought,AAPL,100,15000.00,APPLE INC
"""


def _adapter(
    tmp_path: Path,
    *,
    source: str = SOURCE,
    activity: str = ACTIVITY,
    holdings: str = HOLDINGS,
    transactions: str = TRANSACTIONS,
    instruments: str | None = None,
) -> Path:
    root = tmp_path / "acme"
    root.mkdir(exist_ok=True)
    (root / "source.toml").write_text(textwrap.dedent(source), encoding="utf-8")
    (root / "activity_map.toml").write_text(textwrap.dedent(activity), encoding="utf-8")
    (root / "holdings.csv").write_text(holdings, encoding="utf-8")
    (root / "transactions.csv").write_text(transactions, encoding="utf-8")
    if instruments is not None:
        (root / "instruments.toml").write_text(textwrap.dedent(instruments), encoding="utf-8")
    return root


def _read(tmp_path: Path, **kwargs: Any):  # type: ignore[no-untyped-def]
    return TabularAdapter.load(_adapter(tmp_path, **kwargs)).read()


# ── the crosswalk ────────────────────────────────────────────────────────────

NAMED_SOURCE = SOURCE.replace(
    '[documents.transactions]\nfile = "transactions.csv"',
    '[documents.transactions]\nfile = "transactions.csv"\ncrosswalk = "instruments.toml"',
)
NAMED_TRANSACTIONS = """\
Trade Date,Account,Activity,Symbol,Quantity,Amount,Description
01/06/2026,Main,Bought,APPLE INC COM,100,15000.00,
"""
INSTRUMENTS = """
[[instrument]]
name = "Apple Inc Com"
symbol = "AAPL"
"""


def test_a_name_resolves_to_its_symbol_through_the_crosswalk(tmp_path: Path) -> None:
    """Most custodians put a description on the row and a symbol nowhere."""
    report = _read(
        tmp_path,
        source=NAMED_SOURCE,
        transactions=NAMED_TRANSACTIONS,
        instruments=INSTRUMENTS,
    )
    assert report.transactions[0].identifier == "AAPL"
    # The source row keeps what the custodian wrote: the review is of a
    # mapping *from* something, and the identity hash is over the raw text.
    assert report.transactions[0].source_row["Symbol"] == "APPLE INC COM"


def test_an_unmapped_name_is_a_refusal_naming_the_row(tmp_path: Path) -> None:
    """No fuzzy match. A name that resolves to a *plausible* wrong symbol
    reconciles at the quantity level and is wrong at every other."""
    transactions = NAMED_TRANSACTIONS + "01/07/2026,Main,Bought,APPLE INC CL A,1,150.00,\n"
    with pytest.raises(ValidationError) as excinfo:
        _read(tmp_path, source=NAMED_SOURCE, transactions=transactions, instruments=INSTRUMENTS)
    assert excinfo.value.code == "PT-E-INSTRUMENT-UNMAPPED"
    assert "row 2" in str(excinfo.value)
    assert excinfo.value.context["name"] == "APPLE INC CL A"
    assert excinfo.value.context["mapped"] == ["Apple Inc Com"]


def test_the_crosswalk_folds_case_and_whitespace_and_nothing_else(tmp_path: Path) -> None:
    transactions = NAMED_TRANSACTIONS.replace("APPLE INC COM", "apple  inc   com")
    report = _read(
        tmp_path, source=NAMED_SOURCE, transactions=transactions, instruments=INSTRUMENTS
    )
    assert report.transactions[0].identifier == "AAPL"


def test_a_declared_crosswalk_file_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="no crosswalk"):
        TabularAdapter.load(_adapter(tmp_path, source=NAMED_SOURCE))


def test_a_crosswalk_needs_an_identifier_column_to_resolve(tmp_path: Path) -> None:
    source = NAMED_SOURCE.replace(
        'identifier = "Symbol"\nquantity = "Quantity"\namount', "amount"
    )
    with pytest.raises(ValidationError, match="maps no `identifier` column"):
        TabularAdapter.load(_adapter(tmp_path, source=source, instruments=INSTRUMENTS))


@pytest.mark.parametrize(
    ("body", "complaint"),
    [
        ("", "at least one"),
        ('[[instrument]]\nsymbol = "AAPL"\n', "needs a non-empty `name`"),
        ('[[instrument]]\nname = "Apple"\n', "needs a non-empty `symbol`"),
        (
            '[[instrument]]\nname = "Apple"\nsymbol = "AAPL"\n'
            '[[instrument]]\nname = "APPLE"\nsymbol = "AAPL"\n',
            "mapped twice",
        ),
        ('[[instrument]]\nname = "Apple"\nsymbol = "AAPL"\ncusip = "x"\n', "unknown key"),
    ],
)
def test_everything_wrong_with_a_crosswalk_is_refused_at_load(
    tmp_path: Path, body: str, complaint: str
) -> None:
    path = tmp_path / "instruments.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValidationError, match=complaint):
        load_crosswalk(path)


def test_cash_equivalents_are_matched_on_the_resolved_identifier(tmp_path: Path) -> None:
    """Declare the sweep vehicle once, by symbol, whatever the custodian calls it."""
    source = NAMED_SOURCE.replace(
        '[documents.holdings]\nfile = "holdings.csv"',
        '[documents.holdings]\nfile = "holdings.csv"\ncrosswalk = "instruments.toml"',
    )
    holdings = HOLDINGS.replace(",SWEEP,", ",GOVT MONEY MARKET FUND,").replace(
        ",AAPL,", ",APPLE INC COM,"
    )
    instruments = (
        INSTRUMENTS + '\n[[instrument]]\nname = "Govt Money Market Fund"\nsymbol = "SWEEP"\n'
    )
    report = _read(
        tmp_path,
        source=source,
        holdings=holdings,
        transactions=NAMED_TRANSACTIONS,
        instruments=instruments,
    )
    sweep = [h for h in report.holdings if h.identifier == "SWEEP"]
    assert len(sweep) == 2 and all(h.is_cash_equivalent for h in sweep)


def test_the_crosswalk_does_not_earn_instrument_symbol(tmp_path: Path) -> None:
    """The custodian still supplies no symbols; `portable` resolves them. The
    capability describes the export, and the absence note says a crosswalk is
    what the absence costs -- which is exactly what was paid."""
    report = _read(
        tmp_path,
        source=NAMED_SOURCE,
        transactions=NAMED_TRANSACTIONS,
        instruments=INSTRUMENTS,
    )
    assert ImportCapability.INSTRUMENT_SYMBOL not in report.capabilities


# ── note-keyed rules ─────────────────────────────────────────────────────────

EXPENSE_ACTIVITY = (
    ACTIVITY
    + """
[[activity]]
match = "Expense"
note = "Management Fee"
txn_type = "fee"
fee_class = "external_mgmt_fee"
cash = "negative"

[[activity]]
match = "Expense"
note = "Wire"
txn_type = "fee"
fee_class = "other_admin"
cash = "negative"
"""
)


def test_one_activity_word_maps_by_its_note(tmp_path: Path) -> None:
    """The reference custodian writes *Expense* for a fee, for the transfer that
    funds another account's fee, and for a withdrawal. Only the note tells."""
    transactions = TRANSACTIONS + (
        "01/07/2026,Main,Expense,,,400.00,Management Fee Q4\n"
        "01/08/2026,Main,Expense,,,25.00,Outgoing Wire\n"
    )
    report = _read(tmp_path, activity=EXPENSE_ACTIVITY, transactions=transactions)
    fee, wire = report.mapped[1], report.mapped[2]
    assert fee.txn_type is TransactionType.FEE and fee.fee_class is not None
    assert fee.fee_class.value == "external_mgmt_fee"
    assert wire.fee_class is not None and wire.fee_class.value == "other_admin"
    assert fee.rule == "activity:Expense [Management Fee]"


def test_a_note_matching_no_pattern_is_a_refusal_not_a_fall_through(tmp_path: Path) -> None:
    """No default arm, one level down."""
    transactions = TRANSACTIONS + "01/07/2026,Main,Expense,,,60.00,Transfer to Cover Fee\n"
    with pytest.raises(ValidationError) as excinfo:
        _read(tmp_path, activity=EXPENSE_ACTIVITY, transactions=transactions)
    assert excinfo.value.code == "PT-E-ACTIVITY-UNMAPPED"
    assert "matches none of the note patterns" in str(excinfo.value)
    assert excinfo.value.context["patterns"] == ["Management Fee", "Wire"]
    assert "row 2" in str(excinfo.value)


def test_a_note_matching_two_patterns_is_an_ambiguity_in_the_map(tmp_path: Path) -> None:
    transactions = TRANSACTIONS + "01/07/2026,Main,Expense,,,60.00,Management Fee via Wire\n"
    with pytest.raises(ValidationError, match="matches 2 note patterns"):
        _read(tmp_path, activity=EXPENSE_ACTIVITY, transactions=transactions)


def test_note_patterns_search_case_insensitively(tmp_path: Path) -> None:
    transactions = TRANSACTIONS + "01/07/2026,Main,Expense,,,400.00,MANAGEMENT FEE\n"
    report = _read(tmp_path, activity=EXPENSE_ACTIVITY, transactions=transactions)
    assert report.mapped[1].txn_type is TransactionType.FEE


def test_a_bare_rule_beside_note_keyed_rules_is_refused_at_load(tmp_path: Path) -> None:
    """An un-keyed rule would catch every row the patterns miss."""
    activity = EXPENSE_ACTIVITY + '\n[[activity]]\nmatch = "Expense"\ntxn_type = "withdrawal"\n'
    with pytest.raises(ValidationError, match="no `note` pattern beside 2 that have one"):
        load_activity_map(_adapter(tmp_path, activity=activity) / "activity_map.toml")


def test_two_rules_with_one_note_pattern_are_refused_at_load(tmp_path: Path) -> None:
    activity = EXPENSE_ACTIVITY + (
        '\n[[activity]]\nmatch = "expense"\nnote = "management fee"\ntxn_type = "withdrawal"\n'
    )
    with pytest.raises(ValidationError, match="mapped twice"):
        load_activity_map(_adapter(tmp_path, activity=activity) / "activity_map.toml")


def test_an_invalid_note_pattern_is_refused_at_load(tmp_path: Path) -> None:
    activity = ACTIVITY + '\n[[activity]]\nmatch = "X"\nnote = "("\ntxn_type = "fee"\n'
    with pytest.raises(ValidationError, match="not a valid regex"):
        load_activity_map(_adapter(tmp_path, activity=activity) / "activity_map.toml")


def test_note_keyed_rules_need_a_note_column(tmp_path: Path) -> None:
    """Caught with both files in hand, not six thousand rows in."""
    source = SOURCE.replace('note = "Description"\n', "")
    with pytest.raises(ValidationError, match="maps no `note` column"):
        TabularAdapter.load(_adapter(tmp_path, source=source, activity=EXPENSE_ACTIVITY))


# ── the cash-equivalent whitelist ────────────────────────────────────────────

SWEEP_ACTIVITY = (
    ACTIVITY
    + """
[[activity]]
match = "MoneyTransfer"
skip = true
identifiers = "cash_equivalents"
reason = "a movement between the cash ledger and the sweep vehicle; cash either way (ADR 0013)"
"""
)


def test_a_sweep_movement_is_dropped_when_it_names_a_declared_vehicle(tmp_path: Path) -> None:
    transactions = TRANSACTIONS + "01/07/2026,Main,MoneyTransfer,SWEEP,,500.00,Sweep\n"
    report = _read(tmp_path, activity=SWEEP_ACTIVITY, transactions=transactions)
    assert [s.activity for s in report.skipped] == ["MoneyTransfer"]
    assert len(report.transactions) == 1


def test_the_same_rule_refuses_a_movement_naming_anything_else(tmp_path: Path) -> None:
    """The one thing that must never happen: a real movement discarded by a
    rule written for bookkeeping noise."""
    transactions = TRANSACTIONS + "01/07/2026,Main,MoneyTransfer,AAPL,,500.00,Journal\n"
    with pytest.raises(ValidationError) as excinfo:
        _read(tmp_path, activity=SWEEP_ACTIVITY, transactions=transactions)
    assert excinfo.value.code == "PT-E-ACTIVITY-UNMAPPED"
    assert "names a security identifier" in str(excinfo.value)
    assert excinfo.value.context["classes"] == ["cash_equivalents"]
    assert "row 2" in str(excinfo.value)


def test_the_whitelist_is_per_account(tmp_path: Path) -> None:
    source = SOURCE.replace('default = ["SWEEP"]', 'default = ["SWEEP"]\nIRA = ["SPAXX"]')
    holdings = HOLDINGS.replace("IRA,SWEEP", "IRA,SPAXX")
    transactions = TRANSACTIONS + (
        "01/07/2026,IRA,MoneyTransfer,SPAXX,,500.00,Sweep\n"
        "01/08/2026,Main,MoneyTransfer,SPAXX,,500.00,Sweep\n"
    )
    with pytest.raises(ValidationError, match="row 3"):
        _read(
            tmp_path,
            source=source,
            holdings=holdings,
            activity=SWEEP_ACTIVITY,
            transactions=transactions,
        )


def test_a_restricted_row_with_no_identifier_is_refused(tmp_path: Path) -> None:
    transactions = TRANSACTIONS + "01/07/2026,Main,MoneyTransfer,,,500.00,Sweep\n"
    with pytest.raises(ValidationError, match="names no identifier"):
        _read(tmp_path, activity=SWEEP_ACTIVITY, transactions=transactions)


def test_the_whitelist_needs_an_identifier_column(tmp_path: Path) -> None:
    source = SOURCE.replace('identifier = "Symbol"\nquantity = "Quantity"\namount', "amount")
    with pytest.raises(ValidationError, match="maps no `identifier` column"):
        TabularAdapter.load(_adapter(tmp_path, source=source, activity=SWEEP_ACTIVITY))


# ── pairing ──────────────────────────────────────────────────────────────────

PAIR_ACTIVITY = (
    ACTIVITY
    + """
[[activity]]
match = "Expense"
note = "Management Fee"
txn_type = "fee"
fee_class = "external_mgmt_fee"
cash = "negative"

[[activity]]
match = "Expense"
note = "Transfer to Cover"
txn_type = "transfer"
# Positive in the paying account, negative in the receiving one: the custodian
# signs from its own side of the ledger.
cash = "inverted"
[activity.pair]
counterpart = 'FEE FOR (?P<account>.+)$'
unpaired_out = "withdrawal"
"""
)

QUARTER = TRANSACTIONS + (
    "01/07/2026,Main,Expense,,,400.00,Management Fee\n"
    "01/07/2026,Main,Expense,,,150.00,Transfer to Cover Mgmt Fee FEE FOR IRA\n"
    "01/07/2026,IRA,Expense,,,-150.00,Transfer to Cover Mgmt Fee FEE PAID BY OTHER\n"
    "01/07/2026,IRA,Expense,,,150.00,Management Fee\n"
    "01/07/2026,Main,Expense,,,60.00,Transfer to Cover Mgmt Fee FEE FOR OUTSIDE\n"
)


def test_the_two_legs_of_one_transfer_become_one_row(tmp_path: Path) -> None:
    """ADR 0014's quarter: nine broker rows, seven ledger rows, no fee twice."""
    report = _read(tmp_path, activity=PAIR_ACTIVITY, transactions=QUARTER)
    by_row = {m.record.source_row["Description"]: m for m in report.mapped}

    paying = by_row["Transfer to Cover Mgmt Fee FEE FOR IRA"]
    assert paying.txn_type is TransactionType.TRANSFER
    assert paying.counter_account == "IRA"
    assert paying.record.amount == Decimal("-150.00")
    assert "paired with row 4" in paying.rule

    receiving = by_row["Transfer to Cover Mgmt Fee FEE PAID BY OTHER"]
    assert receiving.is_skipped
    assert "receiving leg of the transfer on row 3" in (receiving.reason or "")
    assert any(s.index == 4 for s in report.skipped)

    # Both legs stay in the records: the cash roll-back needs each account's
    # side of the movement, whichever one the ledger records it from.
    amounts = {(t.account, t.amount) for t in report.transactions if t.activity == "Expense"}
    assert ("IRA", Decimal("150.00")) in amounts and ("Main", Decimal("-150.00")) in amounts


def test_a_leg_to_an_account_outside_the_portfolio_takes_the_declared_fallback(
    tmp_path: Path,
) -> None:
    """Money that left. Classified as a fee it would depress the return by an
    amount that was simply removed (ADR 0014)."""
    report = _read(tmp_path, activity=PAIR_ACTIVITY, transactions=QUARTER)
    outside = next(
        m for m in report.mapped if "FEE FOR OUTSIDE" in m.record.source_row["Description"]
    )
    assert outside.txn_type is TransactionType.WITHDRAWAL
    assert outside.rule.endswith("(unpaired → withdrawal)")


def test_a_named_counterpart_in_the_export_with_no_leg_is_a_hole_not_a_withdrawal(
    tmp_path: Path,
) -> None:
    """The fallback is for money that left the portfolio, never for a row the
    history is missing."""
    transactions = QUARTER.replace(
        "01/07/2026,IRA,Expense,,,-150.00,Transfer to Cover Mgmt Fee FEE PAID BY OTHER\n", ""
    )
    with pytest.raises(ValidationError) as excinfo:
        _read(tmp_path, activity=PAIR_ACTIVITY, transactions=transactions)
    assert "names 'IRA' as the receiving account, which is in this export" in str(excinfo.value)
    assert excinfo.value.context["row"] == 3


def test_an_unpaired_leg_with_no_fallback_is_refused(tmp_path: Path) -> None:
    activity = PAIR_ACTIVITY.replace('unpaired_out = "withdrawal"\n', "")
    with pytest.raises(ValidationError, match="has no counterpart") as excinfo:
        _read(tmp_path, activity=activity, transactions=QUARTER)
    assert "unpaired_out" in str(excinfo.value)


def test_an_unpaired_inbound_leg_is_refused_without_its_own_fallback(tmp_path: Path) -> None:
    """Direction is the leg's direction: an inbound leg cannot fall back to a
    withdrawal, and `unpaired_out` does not cover it."""
    transactions = TRANSACTIONS + (
        "01/07/2026,IRA,Expense,,,-150.00,Transfer to Cover Mgmt Fee FEE PAID BY OTHER\n"
    )
    with pytest.raises(ValidationError, match="unpaired_in"):
        _read(tmp_path, activity=PAIR_ACTIVITY, transactions=transactions)


def test_two_candidates_with_nothing_to_choose_between_them_are_refused(tmp_path: Path) -> None:
    """Two IRAs funded with the same amount on the same day."""
    activity = PAIR_ACTIVITY.replace("counterpart = 'FEE FOR (?P<account>.+)$'\n", "")
    holdings = HOLDINGS + "03/31/2026,Roth,SWEEP,100.00\n"
    transactions = TRANSACTIONS + (
        "01/07/2026,Main,Expense,,,150.00,Transfer to Cover\n"
        "01/07/2026,IRA,Expense,,,-150.00,Transfer to Cover\n"
        "01/07/2026,Roth,Expense,,,-150.00,Transfer to Cover\n"
    )
    with pytest.raises(ValidationError, match="could pair with rows 3, 4") as excinfo:
        _read(tmp_path, activity=activity, holdings=holdings, transactions=transactions)
    assert "Declare a `counterpart` pattern" in str(excinfo.value)


def test_the_counterpart_pattern_decides_between_them(tmp_path: Path) -> None:
    holdings = HOLDINGS + "03/31/2026,Roth,SWEEP,100.00\n"
    transactions = TRANSACTIONS + (
        "01/07/2026,Main,Expense,,,150.00,Transfer to Cover FEE FOR Roth\n"
        "01/07/2026,Main,Expense,,,150.00,Transfer to Cover FEE FOR IRA\n"
        "01/07/2026,IRA,Expense,,,-150.00,Transfer to Cover FEE PAID BY OTHER\n"
        "01/07/2026,Roth,Expense,,,-150.00,Transfer to Cover FEE PAID BY OTHER\n"
    )
    report = _read(
        tmp_path, activity=PAIR_ACTIVITY, holdings=holdings, transactions=transactions
    )
    transfers = {
        m.record.source_row["Description"]: m.counter_account
        for m in report.mapped
        if m.txn_type is TransactionType.TRANSFER
    }
    assert transfers == {
        "Transfer to Cover FEE FOR Roth": "Roth",
        "Transfer to Cover FEE FOR IRA": "IRA",
    }


def test_a_leg_that_moves_no_cash_is_refused(tmp_path: Path) -> None:
    transactions = (
        TRANSACTIONS + "01/07/2026,Main,Expense,,,0.00,Transfer to Cover FEE FOR IRA\n"
    )
    with pytest.raises(ValidationError, match="moves no cash"):
        _read(tmp_path, activity=PAIR_ACTIVITY, transactions=transactions)


def test_the_pairing_fallbacks_count_toward_the_vocabulary(tmp_path: Path) -> None:
    """`activity_covers` reads what the map can produce; an unpaired leg that
    becomes a withdrawal is a withdrawal the map produces."""
    types = load_activity_map(
        _adapter(tmp_path, activity=PAIR_ACTIVITY) / "activity_map.toml"
    ).types()
    assert TransactionType.WITHDRAWAL in types and TransactionType.TRANSFER in types


@pytest.mark.parametrize(
    ("body", "complaint"),
    [
        ('[[activity]]\nmatch = "J"\ntxn_type = "transfer"\n', "no \\[activity.pair\\] table"),
        (
            '[[activity]]\nmatch = "J"\ntxn_type = "deposit"\n[activity.pair]\n',
            "but is not a transfer",
        ),
        (
            '[[activity]]\nmatch = "J"\ntxn_type = "transfer"\n[activity.pair]\n'
            'counterpart = "FOR (.+)"\n',
            "no named group `account`",
        ),
        (
            '[[activity]]\nmatch = "J"\ntxn_type = "transfer"\n[activity.pair]\n'
            'unpaired_out = "deposit"\n',
            "must be one of withdrawal",
        ),
        (
            '[[activity]]\nmatch = "J"\ntxn_type = "transfer"\n[activity.pair]\n'
            'unpaired_in = "withdrawal"\n',
            "must be one of deposit",
        ),
        (
            '[[activity]]\nmatch = "J"\ntxn_type = "transfer"\n[activity.pair]\nsides = 2\n',
            "unknown key",
        ),
    ],
)
def test_everything_wrong_with_a_pairing_rule_is_refused_at_load(
    tmp_path: Path, body: str, complaint: str
) -> None:
    with pytest.raises(ValidationError, match=complaint):
        load_activity_map(_adapter(tmp_path, activity=body) / "activity_map.toml")


def test_inverted_reverses_the_custodians_own_sign() -> None:
    from portable_core.importers.activity import ActivityRule

    rule = ActivityRule(match="x", txn_type=None, quantity=Sign.NONE, cash=Sign.INVERTED)
    assert rule.apply_cash(Decimal("150")) == Decimal("-150")
    assert rule.apply_cash(Decimal("-150")) == Decimal("150")
    assert rule.apply_cash(None) is None


def test_the_reference_date_survives_a_pairing_report(tmp_path: Path) -> None:
    """A pairing rule changes what rows mean, never when they happened."""
    report = _read(tmp_path, activity=PAIR_ACTIVITY, transactions=QUARTER)
    assert report.period == (date(2026, 1, 6), date(2026, 1, 7))


# ── the identifier class as a key ────────────────────────────────────────────

REINVEST_ACTIVITY = (
    ACTIVITY
    + """
[[activity]]
match = "Reinvested Dividend"
identifiers = "cash_equivalents"
txn_type = "dividend"
quantity = "none"
cash = "positive"

[[activity]]
match = "Reinvested Dividend"
identifiers = "securities"
txn_type = "dividend_reinvest"
quantity = "positive"
cash = "positive"
"""
)


def test_one_activity_word_maps_by_the_class_of_its_identifier(tmp_path: Path) -> None:
    """A distribution reinvested into the sweep is income; into a fund it is
    income and a lot. The custodian writes one word for both."""
    transactions = TRANSACTIONS + (
        "01/07/2026,Main,Reinvested Dividend,SWEEP,12.5,12.50,sweep dividend\n"
        "01/07/2026,Main,Reinvested Dividend,AAPL,0.5,100.00,fund dividend\n"
    )
    report = _read(tmp_path, activity=REINVEST_ACTIVITY, transactions=transactions)
    sweep, fund = report.mapped[1], report.mapped[2]
    assert sweep.txn_type is TransactionType.DIVIDEND and sweep.record.quantity is None
    assert fund.txn_type is TransactionType.DIVIDEND_REINVEST
    assert fund.record.quantity == Decimal("0.5")
    assert fund.rule == "activity:Reinvested Dividend [securities]"


def test_a_class_keyed_rule_beside_an_unkeyed_sibling_is_refused_at_load(
    tmp_path: Path,
) -> None:
    activity = REINVEST_ACTIVITY + (
        '\n[[activity]]\nmatch = "Reinvested Dividend"\ntxn_type = "dividend"\n'
    )
    with pytest.raises(ValidationError, match="no `identifiers` class beside"):
        load_activity_map(_adapter(tmp_path, activity=activity) / "activity_map.toml")


def test_an_invalid_identifier_class_lists_the_valid_ones(tmp_path: Path) -> None:
    activity = (
        ACTIVITY + '\n[[activity]]\nmatch = "X"\nidentifiers = "bonds"\ntxn_type = "buy"\n'
    )
    with pytest.raises(ValidationError, match="cash_equivalents, securities"):
        load_activity_map(_adapter(tmp_path, activity=activity) / "activity_map.toml")


def test_note_and_class_keys_compose(tmp_path: Path) -> None:
    """Two keys on one activity: first the note narrows, then the class."""
    activity = (
        ACTIVITY
        + """
[[activity]]
match = "Income"
note = "Reinvest"
identifiers = "cash_equivalents"
txn_type = "interest"
quantity = "none"
cash = "positive"

[[activity]]
match = "Income"
note = "Reinvest"
identifiers = "securities"
txn_type = "dividend_reinvest"
quantity = "positive"
cash = "positive"

[[activity]]
match = "Income"
note = "Cash"
txn_type = "dividend"
quantity = "none"
cash = "positive"
"""
    )
    transactions = TRANSACTIONS + (
        "01/07/2026,Main,Income,SWEEP,1,1.00,Reinvest\n"
        "01/07/2026,Main,Income,AAPL,1,1.00,Reinvest\n"
        "01/07/2026,Main,Income,AAPL,,1.00,Cash\n"
    )
    report = _read(tmp_path, activity=activity, transactions=transactions)
    assert [m.txn_type for m in report.mapped[1:]] == [
        TransactionType.INTEREST,
        TransactionType.DIVIDEND_REINVEST,
        TransactionType.DIVIDEND,
    ]


# ── pairing across a date window, and by alias ───────────────────────────────

COUNTERPART_LINE = "counterpart = 'FEE FOR \\S*?(?P<account>\\d{2})$'\n"
WINDOW_ACTIVITY = (
    ACTIVITY
    + r"""
[[activity]]
match = "Expense"
note = "Transfer to Cover"
txn_type = "transfer"
cash = "inverted"
[activity.pair]
counterpart = 'FEE FOR \S*?(?P<account>\d{2})$'
window_days = 5
unpaired_out = "withdrawal"
"""
)
ALIAS_SOURCE = SOURCE + '\n[account_aliases]\n"48" = "IRA"\n"49" = "Roth"\n'


def test_legs_pair_across_the_declared_window_nearest_first(tmp_path: Path) -> None:
    """The reference custodian dates the receiving leg three days before the
    paying one. Same day is never assumed; the window is declared."""
    holdings = HOLDINGS + "03/31/2026,Roth,SWEEP,100.00\n"
    transactions = TRANSACTIONS + (
        "01/04/2026,IRA,Expense,,,-150.00,Transfer to Cover FEE PAID BY OTHER\n"
        "01/07/2026,Main,Expense,,,150.00,Transfer to Cover FEE FOR WEF000048\n"
    )
    report = _read(
        tmp_path,
        source=ALIAS_SOURCE,
        activity=WINDOW_ACTIVITY,
        holdings=holdings,
        transactions=transactions,
    )
    paying = next(m for m in report.mapped if m.txn_type is TransactionType.TRANSFER)
    assert paying.counter_account == "IRA"


def test_an_alias_resolves_the_token_the_note_uses_for_an_account(tmp_path: Path) -> None:
    """The note names accounts by number; the alias keeps the number out of
    the mapping file and still lets the counterpart decide."""
    holdings = HOLDINGS + "03/31/2026,Roth,SWEEP,100.00\n"
    transactions = TRANSACTIONS + (
        "01/04/2026,IRA,Expense,,,150.00,Transfer to Cover FEE PAID BY OTHER\n"
        "01/04/2026,Roth,Expense,,,150.00,Transfer to Cover FEE PAID BY OTHER\n"
        "01/07/2026,Main,Expense,,,150.00,Transfer to Cover FEE FOR WEF000049\n"
        "01/07/2026,Main,Expense,,,150.00,Transfer to Cover FEE FOR WEF000048\n"
    )
    # Both receiving legs are inbound, so their cash sign must read as such
    # under "inverted": the custodian writes them negative.
    transactions = transactions.replace(
        ",150.00,Transfer to Cover FEE PAID", ",-150.00,Transfer to Cover FEE PAID"
    )
    report = _read(
        tmp_path,
        source=ALIAS_SOURCE,
        activity=WINDOW_ACTIVITY,
        holdings=holdings,
        transactions=transactions,
    )
    transfers = sorted(
        (m.record.note or "")[-2:] + "->" + (m.counter_account or "")
        for m in report.mapped
        if m.txn_type is TransactionType.TRANSFER
    )
    assert transfers == ["48->IRA", "49->Roth"]


def test_two_candidates_at_the_same_distance_are_refused(tmp_path: Path) -> None:
    activity = WINDOW_ACTIVITY.replace(COUNTERPART_LINE, "")
    holdings = HOLDINGS + "03/31/2026,Roth,SWEEP,100.00\n"
    transactions = TRANSACTIONS + (
        "01/04/2026,IRA,Expense,,,-150.00,Transfer to Cover\n"
        "01/10/2026,Roth,Expense,,,-150.00,Transfer to Cover\n"
        "01/07/2026,Main,Expense,,,150.00,Transfer to Cover\n"
    )
    with pytest.raises(ValidationError, match="could pair with rows"):
        _read(tmp_path, activity=activity, holdings=holdings, transactions=transactions)


def test_the_nearer_candidate_wins_when_distances_differ(tmp_path: Path) -> None:
    activity = WINDOW_ACTIVITY.replace(COUNTERPART_LINE, "")
    holdings = HOLDINGS + "03/31/2026,Roth,SWEEP,100.00\n"
    transactions = TRANSACTIONS + (
        "01/04/2026,IRA,Expense,,,-150.00,Transfer to Cover\n"
        "01/06/2026,Roth,Expense,,,-150.00,Transfer to Cover\n"
        "01/07/2026,Main,Expense,,,150.00,Transfer to Cover\n"
        "01/09/2026,Main,Expense,,,150.00,Transfer to Cover\n"
    )
    report = _read(tmp_path, activity=activity, holdings=holdings, transactions=transactions)
    pairs = [
        (m.record.trade_date.day, m.counter_account)
        for m in report.mapped
        if m.txn_type is TransactionType.TRANSFER
    ]
    assert pairs == [(7, "Roth"), (9, "IRA")]


def test_a_leg_outside_the_window_does_not_pair(tmp_path: Path) -> None:
    activity = WINDOW_ACTIVITY.replace("window_days = 5", "window_days = 1")
    transactions = TRANSACTIONS + (
        "01/04/2026,IRA,Expense,,,-150.00,Transfer to Cover FEE PAID BY OTHER\n"
        "01/07/2026,Main,Expense,,,150.00,Transfer to Cover FEE FOR WEF000048\n"
    )
    with pytest.raises(ValidationError, match="names 'IRA' as the receiving account"):
        _read(tmp_path, source=ALIAS_SOURCE, activity=activity, transactions=transactions)


def test_a_negative_window_is_refused_at_load(tmp_path: Path) -> None:
    activity = WINDOW_ACTIVITY.replace("window_days = 5", "window_days = -1")
    with pytest.raises(ValidationError, match="non-negative integer"):
        load_activity_map(_adapter(tmp_path, activity=activity) / "activity_map.toml")


# ── withholding attached to its income row ───────────────────────────────────

WITHHOLD_ACTIVITY = (
    ACTIVITY
    + """
[[activity]]
match = "Dividend"
txn_type = "dividend"
quantity = "none"
cash = "positive"

[[activity]]
match = "Foreign Tax Paid"
attach = "taxes_withheld"
quantity = "none"
cash = "negative"
"""
)


def test_a_withholding_line_lands_on_its_income_row_as_taxes_withheld(tmp_path: Path) -> None:
    """PORT-GIPS-A06: withholding is tax, not a fee. The return is earned on
    the gross and the cash moved by the net, and the ledger wants both on one
    row."""
    transactions = TRANSACTIONS + (
        "01/07/2026,Main,Dividend,AAPL,,100.00,APPLE INC\n"
        "01/07/2026,Main,Foreign Tax Paid,AAPL,,15.00,APPLE INC\n"
    )
    report = _read(tmp_path, activity=WITHHOLD_ACTIVITY, transactions=transactions)
    dividend, tax = report.mapped[1], report.mapped[2]
    assert dividend.txn_type is TransactionType.DIVIDEND
    assert dividend.taxes_withheld == Decimal("15.00")
    assert dividend.record.amount == Decimal("100.00")  # gross, as the custodian stated
    assert tax.is_skipped and "attached to row 2" in tax.rule
    assert any(s.index == 3 for s in report.skipped)
    # Both rows stay in the records: the cash roll-back needs the net.
    assert sum(t.amount for t in report.transactions if t.identifier == "AAPL") == Decimal(
        "-15000.00"
    ) + Decimal("85.00")


@pytest.mark.parametrize(
    ("extra", "complaint"),
    [
        ("01/07/2026,Main,Foreign Tax Paid,AAPL,,15.00,x\n", "no income row to attach to"),
        (
            "01/07/2026,Main,Dividend,AAPL,,100.00,x\n"
            "01/07/2026,Main,Dividend,AAPL,,50.00,x\n"
            "01/07/2026,Main,Foreign Tax Paid,AAPL,,15.00,x\n",
            "could attach to rows 2, 3",
        ),
        (
            "01/07/2026,Main,Dividend,AAPL,,100.00,x\n"
            "01/07/2026,Main,Foreign Tax Paid,AAPL,,0.00,x\n",
            "withholds nothing",
        ),
    ],
)
def test_an_attachment_with_no_single_target_is_refused(
    tmp_path: Path, extra: str, complaint: str
) -> None:
    with pytest.raises(ValidationError, match=complaint):
        _read(tmp_path, activity=WITHHOLD_ACTIVITY, transactions=TRANSACTIONS + extra)


@pytest.mark.parametrize(
    ("body", "complaint"),
    [
        ('[[activity]]\nmatch = "T"\nattach = "fees"\n', "invalid `attach`"),
        (
            '[[activity]]\nmatch = "T"\nattach = "taxes_withheld"\ntxn_type = "fee"\n',
            "neither a ledger row nor a skip",
        ),
    ],
)
def test_everything_wrong_with_an_attach_is_refused_at_load(
    tmp_path: Path, body: str, complaint: str
) -> None:
    with pytest.raises(ValidationError, match=complaint):
        load_activity_map(_adapter(tmp_path, activity=body) / "activity_map.toml")


def test_the_withholding_survives_into_the_batch() -> None:
    from portable_core.domain.import_records import MappedTransaction, TransactionRecord
    from portable_core.services.import_extract import build_incremental_batch

    record = TransactionRecord(
        trade_date=date(2026, 1, 7),
        account="Main",
        activity="Dividend",
        identifier="AAPL",
        quantity=None,
        amount=Decimal("100.00"),
        source_row={},
    )
    extract = build_incremental_batch(
        broker="acme",
        mapped=[
            MappedTransaction(
                record=record,
                rule="activity:Dividend",
                txn_type=TransactionType.DIVIDEND,
                taxes_withheld=Decimal("15.00"),
            )
        ],
        inception={"Main": date(2025, 1, 1)},
        in_ledger=lambda a, r: False,
    )
    row = extract.batch.rows[0]
    assert row.amount == Decimal("100.00") and row.taxes_withheld == Decimal("15.00")


# ── value, where no cash moved ───────────────────────────────────────────────

VALUE_ACTIVITY = (
    ACTIVITY
    + """
[[activity]]
match = "Reinvested"
txn_type = "dividend_reinvest"
quantity = "positive"
cash = "none"
value = "positive"
"""
)


def test_a_rule_can_read_the_amount_as_value_rather_than_cash(tmp_path: Path) -> None:
    """A reinvested distribution moves no cash and is worth something. The
    record keeps the two apart: the cash roll-back reads one, the batch reads
    the other."""
    transactions = TRANSACTIONS + "01/07/2026,Main,Reinvested,AAPL,0.5,100.00,fund\n"
    report = _read(tmp_path, activity=VALUE_ACTIVITY, transactions=transactions)
    record = report.transactions[1]
    assert record.amount == Decimal("0")
    assert record.value == Decimal("100.00")
    assert record.quantity == Decimal("0.5")


def test_a_rule_reading_one_column_as_two_facts_is_refused_at_load(tmp_path: Path) -> None:
    activity = VALUE_ACTIVITY.replace(
        'cash = "none"\nvalue = "positive"', 'cash = "positive"\nvalue = "positive"'
    )
    with pytest.raises(ValidationError, match="both `cash` and `value`"):
        load_activity_map(_adapter(tmp_path, activity=activity) / "activity_map.toml")


def test_a_value_is_a_magnitude(tmp_path: Path) -> None:
    activity = VALUE_ACTIVITY.replace('value = "positive"', 'value = "negative"')
    with pytest.raises(ValidationError, match="a value is a magnitude"):
        load_activity_map(_adapter(tmp_path, activity=activity) / "activity_map.toml")


def test_the_value_is_what_the_batch_carries_when_no_cash_moved() -> None:
    from portable_core.domain.import_records import MappedTransaction, TransactionRecord
    from portable_core.services.import_extract import build_incremental_batch

    record = TransactionRecord(
        trade_date=date(2026, 1, 7),
        account="Main",
        activity="Reinvested",
        identifier="AAPL",
        quantity=Decimal("0.5"),
        amount=Decimal("0"),
        source_row={},
        value=Decimal("100.00"),
    )
    extract = build_incremental_batch(
        broker="acme",
        mapped=[
            MappedTransaction(
                record=record,
                rule="activity:Reinvested",
                txn_type=TransactionType.DIVIDEND_REINVEST,
            )
        ],
        inception={"Main": date(2025, 1, 1)},
        in_ledger=lambda a, r: False,
    )
    row = extract.batch.rows[0]
    assert row.amount == Decimal("100.00")
    assert row.quantity == Decimal("0.5")
    assert row.price == Decimal("200")
