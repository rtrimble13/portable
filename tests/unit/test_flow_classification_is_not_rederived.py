"""Nothing outside the classifier classifies a flow. ADR 0007, ``PORT-GIPS-B02``.

`services/cash_flow.py` is the one place a transaction is told to be an
external flow, income, a cost, or internal, and at which level. A second place
that decides it does not announce itself: it produces a return that is
arithmetically defensible and economically meaningless, and it agrees with the
classifier on every case anybody thought to test.

So this reads the source tree rather than trusting convention. Three rules:

1. **No classification is named outside the classifier.** A module that writes
   ``FlowClassification.EXTERNAL`` is deciding what something is; a consumer
   asks the result (``result.is_external``, ``result.classification``).
2. **No result is built outside it.** A ``FlowResult(...)`` anywhere else is a
   classification made without :func:`cash_flow.classify`.
3. **Where flows are consumed, no flow-bearing transaction type is tested.**
   In a module that imports the classifier, and anywhere in `pert`, a
   reference to a type that can be an external flow -- a deposit, a transfer,
   an in-kind transfer -- is a call site re-deriving what the classifier
   already answered. The set of those types is asked of :func:`classify`
   itself, so a new flow-bearing type joins it without anyone remembering to.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from portable_core.domain.enums import FlowLevel, TransactionSource, TransactionType
from portable_core.domain.models import Transaction
from portable_core.services import cash_flow

pytestmark = [pytest.mark.unit, pytest.mark.gips]

SRC = Path(__file__).resolve().parents[2] / "src"

#: The classifier itself, and where the enum is defined.
CLASSIFIER = Path("portable_core/services/cash_flow.py")
ENUMS = Path("portable_core/domain/enums.py")

#: Every module of these packages is on the return path and bound by rule 3
#: whether or not it imports the classifier.
RETURN_PATH_PACKAGES = ("portable_pert",)


def _flow_bearing_types() -> frozenset[str]:
    """Every transaction type :func:`classify` can call external, at any level.

    Asked of the classifier rather than listed here, so the rule cannot drift
    from what it protects. A stock dividend is external only when valued in
    kind, so each type is asked both ways.
    """
    found: set[str] = set()
    for txn_type in TransactionType:
        row = Transaction(
            txn_id=1,
            account_id=1,
            trade_date=date(2025, 6, 30),
            seq=1,
            txn_type=txn_type,
            net_cash_effect=Decimal("100.00"),
            gross_amount=Decimal("100.00"),
            counter_account_id=2 if txn_type is TransactionType.TRANSFER else None,
            source=TransactionSource.MANUAL,
        )
        for level in FlowLevel:
            for in_kind_value in (None, Decimal("100.00")):
                result = cash_flow.classify(row, level, in_kind_value=in_kind_value)
                if result.is_external:
                    found.add(txn_type.name)
    return frozenset(found)


FLOW_BEARING = _flow_bearing_types()


def _imports_classifier(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module == "portable_core.services.cash_flow":
                return True
            if node.module == "portable_core.services" and any(
                alias.name == "cash_flow" for alias in node.names
            ):
                return True
        if isinstance(node, ast.Import) and any(
            alias.name == "portable_core.services.cash_flow" for alias in node.names
        ):
            return True
    return False


def violations(source: str, relative: Path) -> Iterator[str]:
    """Each place in one module that classifies a flow outside the classifier."""
    if relative in (CLASSIFIER, ENUMS):
        return
    tree = ast.parse(source, filename=str(relative))
    on_return_path = relative.parts[0] in RETURN_PATH_PACKAGES or _imports_classifier(tree)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            owner, member = node.value.id, node.attr
            if owner == "FlowClassification":
                yield f"{relative}:{node.lineno} names FlowClassification.{member} (rule 1)"
            if owner == "TransactionType" and member in FLOW_BEARING and on_return_path:
                yield (
                    f"{relative}:{node.lineno} tests TransactionType.{member} where flows "
                    "are consumed (rule 3)"
                )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "FlowResult"
        ):
            yield f"{relative}:{node.lineno} builds a FlowResult (rule 2)"


def test_the_flow_bearing_types_are_the_ones_the_standard_names() -> None:
    """The derived set, pinned, so a change to it is a change someone sees.

    `docs/gips-standard.md` §6.B: deposits, withdrawals and inter-account
    transfers are external flows, and in-kind movements are, valued at the
    time of distribution. Income and costs never are.
    """
    assert _flow_bearing_types() == {
        "DEPOSIT",
        "WITHDRAWAL",
        "TRANSFER",
        "TRANSFER_IN",
        "TRANSFER_OUT",
        "STOCK_DIVIDEND",
    }


@pytest.mark.parametrize(
    ("source", "relative", "rule"),
    [
        (
            "from portable_core.domain.enums import FlowClassification\n"
            "kind = FlowClassification.EXTERNAL\n",
            Path("portable_core/services/returns.py"),
            "rule 1",
        ),
        (
            "from portable_core.services.cash_flow import FlowResult\n"
            "r = FlowResult(None, 0, None)\n",
            Path("portable_core/services/returns.py"),
            "rule 2",
        ),
        (
            "from portable_core.services import cash_flow\n"
            "def f(t):\n    return t.txn_type is TransactionType.DEPOSIT\n",
            Path("portable_core/services/returns.py"),
            "rule 3",
        ),
        (
            "def f(t):\n    return t.txn_type is TransactionType.TRANSFER\n",
            Path("portable_pert/engine.py"),
            "rule 3",
        ),
    ],
)
def test_each_rule_catches_what_it_names(source: str, relative: Path, rule: str) -> None:
    """The reader has teeth: each rule reports the case it exists for."""
    found = list(violations(source, relative))
    assert any(rule in message for message in found), found


def test_a_module_that_does_not_consume_flows_may_name_a_deposit() -> None:
    """Rule 3 binds flow consumers, not the code that records a deposit."""
    source = "def record(t):\n    return t.txn_type is TransactionType.DEPOSIT\n"
    assert list(violations(source, Path("portable_core/services/trading.py"))) == []


def test_no_module_classifies_a_flow_outside_the_classifier() -> None:
    """ADR 0007: one function, and nothing re-derives it."""
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC)
        found.extend(violations(path.read_text(encoding="utf-8"), relative))
    assert found == [], "\n".join(found)
