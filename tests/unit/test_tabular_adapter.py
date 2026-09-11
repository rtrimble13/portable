"""The generic tabular adapter: two TOML files and no Python per custodian.

ADR 0018. The tests are organised the way the refusals are: everything that can
be wrong with a mapping file is caught when the file loads, because a map is
reviewed once and used for every row thereafter; everything that can be wrong
with the *data* is caught with the row quoted, because that is what a person
has to go and look at.

The claim under test throughout is ADR 0018 §3 -- **a capability is declared on
validated data, not on a present column** -- and the sharp end of it is that a
withheld capability's data is not merely unreported but unread.
"""

from __future__ import annotations

import textwrap
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portable_core.domain.enums import FeeClass, TransactionType
from portable_core.errors import PortableError, ValidationError
from portable_core.importers import (
    ABSENCE_MEANS,
    CHECKS,
    ImportCapability,
    Sign,
    TabularAdapter,
    load_activity_map,
    load_source,
)
from portable_core.importers.source import NumberFormat

pytestmark = pytest.mark.unit

# ── a minimal, valid adapter, which every test varies one piece of ───────────

SOURCE = """
broker = "acme"
name = "Acme Custody"

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
cost_basis = "Cost Basis"

[documents.transactions]
file = "transactions.csv"
[documents.transactions.columns]
trade_date = "Trade Date"
settlement_date = "Settle Date"
account = "Account"
activity = "Activity"
identifier = "Symbol"
quantity = "Quantity"
amount = "Amount"
external_id = "Confirm"
"""

ACTIVITY = """
[[activity]]
match = "Bought"
txn_type = "buy"
quantity = "positive"
cash = "negative"

[[activity]]
match = "Sold"
txn_type = "sell"
quantity = "negative"
cash = "positive"

[[activity]]
match = "Contribution"
txn_type = "deposit"
cash = "positive"
"""

HOLDINGS = """\
As Of,Account,Symbol,Quantity,Cost Basis
03/31/2026,Main,AAPL,100,15000.00
03/31/2026,Main,SWEEP,2500.00,2500.00
"""

TRANSACTIONS = """\
Trade Date,Settle Date,Account,Activity,Symbol,Quantity,Amount,Confirm
01/05/2026,01/07/2026,Main,Contribution,,,20000.00,T1
01/06/2026,01/08/2026,Main,Bought,AAPL,100,15000.00,T2
"""


def _adapter(
    tmp_path: Path,
    *,
    source: str = SOURCE,
    activity: str = ACTIVITY,
    holdings: str = HOLDINGS,
    transactions: str = TRANSACTIONS,
) -> Path:
    root = tmp_path / "acme"
    root.mkdir(exist_ok=True)
    (root / "source.toml").write_text(textwrap.dedent(source), encoding="utf-8")
    (root / "activity_map.toml").write_text(textwrap.dedent(activity), encoding="utf-8")
    (root / "holdings.csv").write_text(holdings, encoding="utf-8")
    (root / "transactions.csv").write_text(transactions, encoding="utf-8")
    return root


def _read(tmp_path: Path, **kwargs: str):  # type: ignore[no-untyped-def]
    return TabularAdapter.load(_adapter(tmp_path, **kwargs)).read()


# ── the required minimum ─────────────────────────────────────────────────────


def test_the_two_required_documents_are_required(tmp_path: Path) -> None:
    """ADR 0018 §1. Neither substitutes for the other."""
    source = SOURCE.replace('[documents.transactions]\nfile = "transactions.csv"', "")
    source = source.split("[documents.transactions.columns]")[0]
    with pytest.raises(ValidationError) as excinfo:
        load_source(_adapter(tmp_path, source=source))
    assert "transactions" in str(excinfo.value)
    assert "ADR 0018" in str(excinfo.value)


def test_a_missing_required_column_names_the_field(tmp_path: Path) -> None:
    source = SOURCE.replace('quantity = "Quantity"\ncost_basis', "cost_basis")
    with pytest.raises(ValidationError, match=r"missing required field.*quantity"):
        load_source(_adapter(tmp_path, source=source))


def test_a_column_the_file_does_not_have_lists_the_ones_it_does(tmp_path: Path) -> None:
    """The error a mapping typo actually produces, and the only useful form of it."""
    holdings = HOLDINGS.replace("Symbol", "Ticker")
    with pytest.raises(ValidationError) as excinfo:
        _read(tmp_path, holdings=holdings)
    message = str(excinfo.value)
    assert "'Symbol'" in message and "mapped as identifier" in message
    assert "'Ticker'" in message


def test_a_holdings_snapshot_with_no_stated_date_is_refused(tmp_path: Path) -> None:
    """A snapshot with no date cannot anchor a reconstruction (ADR 0017)."""
    source = SOURCE.replace('as_of = "As Of"\n', "")
    with pytest.raises(ValidationError, match="states no as-of date"):
        load_source(_adapter(tmp_path, source=source))


def test_the_as_of_date_may_be_stated_in_the_spec_instead(tmp_path: Path) -> None:
    """Custodians that put the date in a page header rather than a column."""
    source = SOURCE.replace(
        '[documents.holdings]\nfile = "holdings.csv"',
        '[documents.holdings]\nfile = "holdings.csv"\nas_of = "2026-03-31"',
    ).replace('as_of = "As Of"\n', "")
    holdings = "Account,Symbol,Quantity,Cost Basis\nMain,AAPL,100,15000.00\nMain,SWEEP,1,1\n"
    report = _read(tmp_path, source=source, holdings=holdings)
    assert report.as_of == date(2026, 3, 31)


def test_two_as_of_dates_are_not_one_snapshot(tmp_path: Path) -> None:
    holdings = HOLDINGS.replace("03/31/2026,Main,SWEEP", "02/28/2026,Main,SWEEP")
    with pytest.raises(ValidationError, match="more than one as-of date"):
        _read(tmp_path, holdings=holdings)


def test_cash_on_the_snapshot_is_required(tmp_path: Path) -> None:
    """ADR 0018 §1: the only check that catches a sign error or a dropped row."""
    holdings = "As Of,Account,Symbol,Quantity,Cost Basis\n03/31/2026,Main,AAPL,100,15000\n"
    with pytest.raises(ValidationError) as excinfo:
        _read(tmp_path, holdings=holdings)
    assert "states no cash for Main" in str(excinfo.value)
    assert "cash_equivalents" in str(excinfo.value)


def test_an_account_with_no_cash_line_can_be_declared_as_an_exception(
    tmp_path: Path,
) -> None:
    """A recorded decision, not a silent pass."""
    source = SOURCE.replace(
        '[documents.holdings]\nfile = "holdings.csv"',
        '[documents.holdings]\nfile = "holdings.csv"\nallow_missing_cash = ["Main"]',
    )
    holdings = "As Of,Account,Symbol,Quantity,Cost Basis\n03/31/2026,Main,AAPL,100,15000\n"
    report = _read(tmp_path, source=source, holdings=holdings)
    assert len(report.holdings) == 1


def test_a_sweep_identifier_is_cash_for_the_account_that_declares_it(
    tmp_path: Path,
) -> None:
    """ADR 0013, generalised: a declared set per account, not two tickers."""
    report = _read(tmp_path)
    by_symbol = {h.identifier: h for h in report.holdings}
    assert by_symbol["SWEEP"].is_cash_equivalent
    assert not by_symbol["AAPL"].is_cash_equivalent


def test_a_cash_line_stated_as_a_balance_takes_it_as_its_quantity(tmp_path: Path) -> None:
    """ADR 0013. Custodians state cash as a balance, not a share count; for a
    par-priced vehicle the two are one number. A security line with no
    quantity is still refused -- the fallback is for cash only."""
    source = SOURCE.replace(
        'cost_basis = "Cost Basis"', 'cost_basis = "Cost Basis"\nmarket_value = "MV"'
    )
    holdings = (
        "As Of,Account,Symbol,Quantity,Cost Basis,MV\n"
        "03/31/2026,Main,AAPL,100,15000.00,20000.00\n"
        "03/31/2026,Main,SWEEP,,2500.00,2500.00\n"
    )
    report = _read(tmp_path, source=source, holdings=holdings)
    sweep = next(h for h in report.holdings if h.identifier == "SWEEP")
    assert sweep.quantity == Decimal("2500.00") and sweep.is_cash_equivalent

    with pytest.raises(ValidationError, match="no quantity"):
        _read(tmp_path, source=source, holdings=holdings.replace("AAPL,100,", "AAPL,,"))


def test_a_spreadsheet_is_refused_by_name_with_the_remedy(tmp_path: Path) -> None:
    """Invariant 10: no half-support for a format the runtime cannot read."""
    root = _adapter(tmp_path)
    (root / "holdings.xlsx").write_bytes(b"PK\x03\x04not really")
    (root / "source.toml").write_text(
        textwrap.dedent(SOURCE).replace('file = "holdings.csv"', 'file = "holdings.xlsx"'),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="Export it as CSV"):
        TabularAdapter.load(root).read()


# ── the activity map ─────────────────────────────────────────────────────────


def test_an_unmapped_activity_stops_the_import_and_quotes_the_row(
    tmp_path: Path,
) -> None:
    """No default arm. A guess here is a plausible number in a tax report."""
    transactions = TRANSACTIONS + "02/01/2026,02/03/2026,Main,Reorganization,X,1,0,T3\n"
    with pytest.raises(ValidationError) as excinfo:
        _read(tmp_path, transactions=transactions)
    assert "unmapped activity 'Reorganization'" in str(excinfo.value)
    assert "row 3" in str(excinfo.value)
    assert excinfo.value.context["mapped"] == ["Bought", "Sold", "Contribution"]


def test_matching_folds_case_and_collapses_whitespace(tmp_path: Path) -> None:
    """One vocabulary written two ways is still one vocabulary."""
    transactions = TRANSACTIONS.replace("Main,Bought,", "Main,BOUGHT,")
    assert len(_read(tmp_path, transactions=transactions).transactions) == 2


def test_matching_is_exact_and_never_a_prefix(tmp_path: Path) -> None:
    """A new activity string must not inherit an old one's meaning."""
    transactions = TRANSACTIONS.replace("Main,Bought,", "Main,Bought to Cover,")
    with pytest.raises(ValidationError, match="unmapped activity 'Bought to Cover'"):
        _read(tmp_path, transactions=transactions)


def test_two_rules_for_one_string_are_refused(tmp_path: Path) -> None:
    activity = ACTIVITY + '\n[[activity]]\nmatch = "bought"\ntxn_type = "sell"\n'
    with pytest.raises(ValidationError, match="mapped twice"):
        load_activity_map(_adapter(tmp_path, activity=activity) / "activity_map.toml")


def test_a_fee_with_no_fee_class_is_refused_at_load(tmp_path: Path) -> None:
    """PORT-GIPS-D01: the three return bases are derived from it."""
    activity = ACTIVITY + '\n[[activity]]\nmatch = "Fee"\ntxn_type = "fee"\n'
    with pytest.raises(ValidationError, match="fee with no `fee_class`"):
        load_activity_map(_adapter(tmp_path, activity=activity) / "activity_map.toml")


def test_a_fee_class_on_something_that_is_not_a_fee_is_refused(tmp_path: Path) -> None:
    activity = ACTIVITY.replace(
        'match = "Contribution"\ntxn_type = "deposit"',
        'match = "Contribution"\ntxn_type = "deposit"\nfee_class = "other_admin"',
    )
    with pytest.raises(ValidationError, match="is not a fee"):
        load_activity_map(_adapter(tmp_path, activity=activity) / "activity_map.toml")


def test_a_fee_rule_carries_its_class_through(tmp_path: Path) -> None:
    activity = (
        ACTIVITY + '\n[[activity]]\nmatch = "Advisory Fee"\ntxn_type = "fee"\n'
        'fee_class = "external_mgmt_fee"\ncash = "negative"\n'
    )
    loaded = load_activity_map(_adapter(tmp_path, activity=activity) / "activity_map.toml")
    rule = loaded.rule_for("advisory fee")
    assert rule.txn_type is TransactionType.FEE
    assert rule.fee_class is FeeClass.EXTERNAL_MGMT_FEE


def test_a_skipped_row_must_say_why(tmp_path: Path) -> None:
    """A decision has to be distinguishable from an oversight."""
    activity = ACTIVITY + '\n[[activity]]\nmatch = "Memo"\nskip = true\n'
    with pytest.raises(ValidationError, match="skipped with no `reason`"):
        load_activity_map(_adapter(tmp_path, activity=activity) / "activity_map.toml")


def test_a_skipped_row_is_reported_rather_than_dropped(tmp_path: Path) -> None:
    activity = (
        ACTIVITY + '\n[[activity]]\nmatch = "Memo"\nskip = true\nreason = "a restatement"\n'
    )
    transactions = TRANSACTIONS + "02/01/2026,02/03/2026,Main,Memo,AAPL,100,,T3\n"
    report = _read(tmp_path, activity=activity, transactions=transactions)
    assert len(report.transactions) == 2
    assert report.skipped[0].activity == "Memo"
    assert report.skipped[0].reason == "a restatement"


def test_an_unknown_transaction_type_lists_the_known_ones(tmp_path: Path) -> None:
    activity = ACTIVITY.replace('txn_type = "buy"', 'txn_type = "purchase"')
    with pytest.raises(ValidationError) as excinfo:
        load_activity_map(_adapter(tmp_path, activity=activity) / "activity_map.toml")
    assert "unknown txn_type 'purchase'" in str(excinfo.value)
    assert "buy" in str(excinfo.value)


def test_an_invalid_sign_convention_lists_the_valid_ones(tmp_path: Path) -> None:
    activity = ACTIVITY.replace('cash = "negative"', 'cash = "outflow"')
    with pytest.raises(ValidationError, match="invalid `cash` convention"):
        load_activity_map(_adapter(tmp_path, activity=activity) / "activity_map.toml")


# ── sign conventions ─────────────────────────────────────────────────────────


def test_the_map_normalises_signs_to_the_canonical_convention(tmp_path: Path) -> None:
    """Positive is in, negative is out -- whatever the custodian wrote.

    The custodian here states magnitudes and puts the direction in the activity
    string, which is the common case and the one that produces a sign error if
    the amount column is taken at face value.
    """
    report = _read(tmp_path)
    contribution, bought = report.transactions
    assert contribution.amount == Decimal("20000.00")
    assert contribution.quantity is None
    assert bought.amount == Decimal("-15000.00")
    assert bought.quantity == Decimal("100")


def test_as_stated_keeps_the_custodians_own_sign(tmp_path: Path) -> None:
    activity = ACTIVITY.replace(
        'match = "Bought"\ntxn_type = "buy"\nquantity = "positive"\ncash = "negative"',
        'match = "Bought"\ntxn_type = "buy"\nquantity = "as_stated"\ncash = "as_stated"',
    )
    transactions = TRANSACTIONS.replace(",100,15000.00,T2", ",100,-15000.00,T2")
    report = _read(tmp_path, activity=activity, transactions=transactions)
    assert report.transactions[1].amount == Decimal("-15000.00")


def test_a_blank_where_the_map_expects_a_value_is_refused(tmp_path: Path) -> None:
    """A blank is not a zero, and the import will not choose one."""
    transactions = TRANSACTIONS.replace(",100,15000.00,T2", ",,15000.00,T2")
    with pytest.raises(ValidationError) as excinfo:
        _read(tmp_path, transactions=transactions)
    assert "no quantity" in str(excinfo.value)
    assert "row 2" in str(excinfo.value)


def test_a_stated_absence_of_cash_is_a_stated_zero(tmp_path: Path) -> None:
    """`cash = "none"` is the map asserting the activity moves no cash."""
    activity = ACTIVITY + (
        '\n[[activity]]\nmatch = "Split"\ntxn_type = "split"\n'
        'quantity = "positive"\ncash = "none"\n'
    )
    transactions = TRANSACTIONS + "02/01/2026,02/03/2026,Main,Split,AAPL,100,,T3\n"
    report = _read(tmp_path, activity=activity, transactions=transactions)
    assert report.transactions[2].amount == Decimal("0")
    assert report.transactions[2].quantity == Decimal("100")


# ── number and date formats ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1,234.56", "1234.56"),
        ("$1,234.56", "1234.56"),
        ("(1,234.56)", "-1234.56"),
        ("-1234.56", "-1234.56"),
        ("0", "0"),
    ],
)
def test_numbers_are_read_in_the_declared_format(text: str, expected: str) -> None:
    assert NumberFormat().number(text, what="Amount", row=1) == Decimal(expected)


def test_a_cell_that_is_not_a_number_names_the_row_and_the_column() -> None:
    with pytest.raises(ValidationError) as excinfo:
        NumberFormat().number("n/m", what="Amount", row=7)
    assert "row 7" in str(excinfo.value)
    assert "Amount" in str(excinfo.value)


def test_infinity_is_not_a_number() -> None:
    """`Decimal` accepts it happily, which is exactly the problem."""
    with pytest.raises(ValidationError, match="not a finite number"):
        NumberFormat().number("Infinity", what="Amount", row=1)


def test_a_date_matching_no_declared_pattern_lists_the_patterns() -> None:
    with pytest.raises(ValidationError) as excinfo:
        NumberFormat(dates=("%m/%d/%Y",)).date("2026-01-02", what="Trade Date", row=3)
    assert "%m/%d/%Y" in str(excinfo.value)
    assert "row 3" in str(excinfo.value)


def test_patterns_are_tried_in_the_declared_order() -> None:
    fmt = NumberFormat(dates=("%m/%d/%Y", "%Y-%m-%d"))
    assert fmt.date("01/02/2026", what="d", row=1) == date(2026, 1, 2)
    assert fmt.date("2026-01-02", what="d", row=1) == date(2026, 1, 2)


def test_a_declared_blank_is_none_and_never_zero(tmp_path: Path) -> None:
    holdings = HOLDINGS.replace("100,15000.00", "100,N/A")
    report = _read(tmp_path, holdings=holdings)
    assert report.holdings[0].cost_basis is None


def test_an_invalid_date_pattern_is_caught_when_the_source_loads(
    tmp_path: Path,
) -> None:
    """Not six thousand rows into an import."""
    source = SOURCE.replace('dates = ["%m/%d/%Y"]', 'dates = ["%Q"]')
    with pytest.raises(ValidationError, match="not a valid pattern"):
        load_source(_adapter(tmp_path, source=source))


def test_a_pattern_that_carries_only_part_of_a_date_is_refused(
    tmp_path: Path,
) -> None:
    """`%Y-%m` parses without complaint and invents the first of the month.

    This is the one that would not have surfaced at all: no exception, no
    warning, every trade dated the 1st.
    """
    source = SOURCE.replace('dates = ["%m/%d/%Y"]', 'dates = ["%Y-%m"]')
    with pytest.raises(ValidationError) as excinfo:
        load_source(_adapter(tmp_path, source=source))
    assert "does not carry a whole date" in str(excinfo.value)
    assert "2026-03-01" in str(excinfo.value)


@pytest.mark.parametrize("pattern", ["%m/%d/%Y", "%Y-%m-%d", "%Y%m%d", "%m/%d/%y"])
def test_whole_date_patterns_are_accepted(tmp_path: Path, pattern: str) -> None:
    source = SOURCE.replace('dates = ["%m/%d/%Y"]', f'dates = ["{pattern}"]')
    assert load_source(_adapter(tmp_path, source=source)).numbers.dates == (pattern,)


# ── capabilities ─────────────────────────────────────────────────────────────


CAPABILITIES = """
[[capability]]
name = "cost_basis"
document = "holdings"
field = "cost_basis"
check = "populated"

[[capability]]
name = "settlement_date"
document = "transactions"
field = "settlement_date"
check = "not_before_trade_date"

[[capability]]
name = "transaction_id"
document = "transactions"
field = "external_id"
check = "unique"
"""


def test_a_capability_whose_check_passes_is_declared(tmp_path: Path) -> None:
    report = _read(tmp_path, source=SOURCE + CAPABILITIES)
    assert ImportCapability.COST_BASIS in report.capabilities
    assert ImportCapability.SETTLEMENT_DATE in report.capabilities
    assert report.holdings[0].cost_basis == Decimal("15000.00")


def test_a_column_is_not_a_capability(tmp_path: Path) -> None:
    """ADR 0018 §3, and the reference custodian's actual trap.

    The settlement column is fully populated with real dates. Most of them
    precede their own trade date, so the column holds something other than what
    its header says -- and importing it would put that something in the ledger.
    """
    transactions = TRANSACTIONS.replace("01/07/2026", "01/03/2026").replace(
        "01/08/2026", "01/04/2026"
    )
    report = _read(tmp_path, source=SOURCE + CAPABILITIES, transactions=transactions)

    assert ImportCapability.SETTLEMENT_DATE not in report.capabilities
    finding = report.capabilities.finding(ImportCapability.SETTLEMENT_DATE)
    assert finding.check == "not_before_trade_date"
    assert "before their own" in (finding.reason or "")


def test_a_withheld_capability_strips_its_data(tmp_path: Path) -> None:
    """Not merely unreported -- unread.

    A capability that labels data which flows through anyway is a comment, not
    a safeguard. Nothing downstream can then read a field no check validated.
    """
    transactions = TRANSACTIONS.replace("01/07/2026", "01/03/2026").replace(
        "01/08/2026", "01/04/2026"
    )
    report = _read(tmp_path, source=SOURCE + CAPABILITIES, transactions=transactions)
    assert all(t.settlement_date is None for t in report.transactions)
    # The capability that still passes keeps its data, so this is a targeted
    # strip rather than the document failing wholesale.
    assert [t.external_id for t in report.transactions] == ["T1", "T2"]


def test_a_repeated_identifier_is_not_an_identifier(tmp_path: Path) -> None:
    """Declaring TRANSACTION_ID on a recycled confirm number would make ADR
    0012's re-import safety a fiction."""
    transactions = TRANSACTIONS.replace(",T2", ",T1")
    report = _read(tmp_path, source=SOURCE + CAPABILITIES, transactions=transactions)
    assert ImportCapability.TRANSACTION_ID not in report.capabilities
    assert "appears on rows 1 and 2" in (
        report.capabilities.finding(ImportCapability.TRANSACTION_ID).reason or ""
    )
    assert all(t.external_id is None for t in report.transactions)


def test_a_partly_populated_column_fails_a_full_populated_check(tmp_path: Path) -> None:
    holdings = HOLDINGS.replace("100,15000.00", "100,")
    report = _read(tmp_path, source=SOURCE + CAPABILITIES, holdings=holdings)
    assert ImportCapability.COST_BASIS not in report.capabilities
    assert "1 of 2 rows" in (
        report.capabilities.finding(ImportCapability.COST_BASIS).reason or ""
    )


def test_a_min_ratio_lets_a_custodian_be_partly_complete(tmp_path: Path) -> None:
    """Lowering the bar is a review decision, recorded in the file."""
    source = SOURCE + CAPABILITIES.replace(
        'field = "cost_basis"\ncheck = "populated"',
        'field = "cost_basis"\ncheck = "populated"\nmin_ratio = "0.5"',
    )
    holdings = HOLDINGS.replace("100,15000.00", "100,")
    report = _read(tmp_path, source=source, holdings=holdings)
    assert ImportCapability.COST_BASIS in report.capabilities


def test_an_unmapped_column_withholds_rather_than_crashes(tmp_path: Path) -> None:
    source = SOURCE.replace('cost_basis = "Cost Basis"\n', "") + CAPABILITIES
    holdings = (
        "As Of,Account,Symbol,Quantity\n03/31/2026,Main,AAPL,100\n03/31/2026,Main,SWEEP,1\n"
    )
    report = _read(tmp_path, source=source, holdings=holdings)
    finding = report.capabilities.finding(ImportCapability.COST_BASIS)
    assert not finding.declared
    assert "maps no cost_basis column" in (finding.reason or "")


def test_external_flows_is_a_fact_about_the_vocabulary_not_a_column(
    tmp_path: Path,
) -> None:
    source = SOURCE + (
        '\n[[capability]]\nname = "external_flows"\ndocument = "transactions"\n'
        'check = "activity_covers"\ntypes = ["deposit", "withdrawal"]\n'
    )
    report = _read(tmp_path, source=source)
    finding = report.capabilities.finding(ImportCapability.EXTERNAL_FLOWS)
    assert not finding.declared
    assert "withdrawal" in (finding.reason or "")

    activity = ACTIVITY + (
        '\n[[activity]]\nmatch = "Distribution"\ntxn_type = "withdrawal"\ncash = "negative"\n'
    )
    assert (
        ImportCapability.EXTERNAL_FLOWS
        in _read(tmp_path, source=source, activity=activity).capabilities
    )


def test_history_to_inception_fails_on_a_truncated_window(tmp_path: Path) -> None:
    """The two-year window a custodian will not extend, caught by name."""
    source = SOURCE + (
        '\n[[capability]]\nname = "history_to_inception"\ndocument = "transactions"\n'
        'check = "history_since"\nsince = "2019-01-01"\n'
    )
    report = _read(tmp_path, source=source)
    finding = report.capabilities.finding(ImportCapability.HISTORY_TO_INCEPTION)
    assert not finding.declared
    assert "ADR 0017" in (finding.reason or "")


def test_matches_is_how_a_symbol_column_is_told_from_a_description(
    tmp_path: Path,
) -> None:
    source = SOURCE + (
        '\n[[capability]]\nname = "instrument_symbol"\ndocument = "holdings"\n'
        'field = "identifier"\ncheck = "matches"\ntypes = ["[A-Z]{1,5}"]\n'
    )
    assert ImportCapability.INSTRUMENT_SYMBOL in _read(tmp_path, source=source).capabilities

    holdings = HOLDINGS.replace(",AAPL,", ",Apple Inc Common Stock,")
    report = _read(tmp_path, source=source, holdings=holdings)
    assert ImportCapability.INSTRUMENT_SYMBOL not in report.capabilities


def test_every_capability_is_reported_declared_or_not(tmp_path: Path) -> None:
    """A set of what is present cannot answer "why not?"."""
    report = _read(tmp_path, source=SOURCE + CAPABILITIES)
    reported = {f.capability for f in report.capabilities.findings}
    assert reported == set(ImportCapability)
    undeclared = report.capabilities.finding(ImportCapability.LOT_DETAIL)
    assert undeclared.reason == "the source declares no check for it"


def test_every_capability_states_what_its_absence_costs() -> None:
    """ADR 0018's table, kept in code so it cannot drift from the behaviour."""
    assert set(ABSENCE_MEANS) == set(ImportCapability)
    assert all(text.strip() for text in ABSENCE_MEANS.values())


def test_an_unknown_check_is_refused_when_the_adapter_loads(tmp_path: Path) -> None:
    source = SOURCE + CAPABILITIES.replace('check = "populated"', 'check = "looks_ok"')
    with pytest.raises(ValidationError) as excinfo:
        TabularAdapter.load(_adapter(tmp_path, source=source))
    assert "unknown check 'looks_ok'" in str(excinfo.value)
    assert "populated" in str(excinfo.value)


def test_an_unknown_capability_name_lists_the_known_ones(tmp_path: Path) -> None:
    source = SOURCE + '\n[[capability]]\nname = "lot_history"\ncheck = "populated"\n'
    with pytest.raises(ValidationError, match="unknown capability 'lot_history'"):
        load_source(_adapter(tmp_path, source=source))


def test_one_capability_cannot_be_declared_twice(tmp_path: Path) -> None:
    source = SOURCE + CAPABILITIES + CAPABILITIES
    with pytest.raises(ValidationError, match="declares cost_basis twice"):
        load_source(_adapter(tmp_path, source=source))


def test_every_named_check_is_exercised_by_this_module() -> None:
    """A check nobody tests is a check nobody has read."""
    exercised = {"populated", "unique", "matches", "not_before_trade_date"}
    exercised |= {"history_since", "activity_covers"}
    assert exercised == set(CHECKS)


# ── provenance and shape ─────────────────────────────────────────────────────


def test_the_report_carries_a_digest_per_document(tmp_path: Path) -> None:
    """So a batch can name exactly what it was built from."""
    report = _read(tmp_path)
    assert [name for name, _ in report.files] == ["holdings.csv", "transactions.csv"]
    assert all(len(digest) == 64 for _, digest in report.files)


def test_the_period_is_the_history_it_actually_covers(tmp_path: Path) -> None:
    report = _read(tmp_path)
    assert report.period == (date(2026, 1, 5), date(2026, 1, 6))
    assert report.accounts == ("Main",)


def test_the_source_row_is_kept_verbatim(tmp_path: Path) -> None:
    """A reviewer approving a batch is approving a mapping *from* something."""
    report = _read(tmp_path)
    assert report.transactions[1].source_row["Amount"] == "15000.00"
    assert report.transactions[1].source_row["Activity"] == "Bought"


def test_a_missing_mapping_file_is_a_portable_error(tmp_path: Path) -> None:
    root = _adapter(tmp_path)
    (root / "activity_map.toml").unlink()
    with pytest.raises(PortableError, match="no activity map"):
        TabularAdapter.load(root)


def test_a_malformed_toml_file_names_itself(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="not valid TOML"):
        load_source(_adapter(tmp_path, source="broker = [unclosed"))


def test_signs_apply_to_none_without_inventing_a_value() -> None:
    from portable_core.importers.activity import ActivityRule

    rule = ActivityRule(match="x", txn_type=None, quantity=Sign.NONE, cash=Sign.POSITIVE)
    assert rule.apply_quantity(Decimal("5")) is None
    assert rule.apply_cash(None) is None
