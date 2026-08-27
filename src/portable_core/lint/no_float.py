"""Lint rule: no binary floating point in any money path.

``CLAUDE.md`` invariant 1 and bootstrap §3.2. Prices, quantities, amounts, and
rates are :class:`decimal.Decimal` in Python and canonical decimal ``TEXT`` in
SQLite. A ``float`` in a money path does not fail — it produces a number that is
wrong in the seventeenth decimal place, and then wrong in the second after ten
thousand of them have been summed. That is the "silently wrong number" failure
mode this repository is built to prevent.

What is flagged
---------------

In Python sources under ``src/``:

* a ``float`` literal (``0.5``, ``1e-9``) anywhere;
* ``float`` used as a type annotation, a base class, or a cast;
* ``round(...)`` with a non-``Decimal``-typed first argument is **not** flagged —
  that needs types, not an AST, and the dataclass ``__post_init__`` guard in
  :mod:`portable_core.domain` catches it at runtime instead.

In SQL sources under ``src/portable_core/schema/``:

* a ``REAL``, ``FLOAT``, ``DOUBLE``, or ``NUMERIC`` column type. ``NUMERIC`` is
  included deliberately: SQLite's ``NUMERIC`` affinity silently stores as
  ``REAL`` anything it cannot hold as an integer, which is this exact bug
  wearing a disguise (ADR 0005).

The suppression marker, and where it does not work
--------------------------------------------------

``# no-float: allow -- <reason>`` suppresses exactly one line, and the reason is
**required** — a bare marker is itself a finding.

The marker has no effect inside the money-critical packages listed in
:data:`MONEY_CRITICAL`. Using it there is reported as ``PT-LINT-FLOAT-003``:
"marker not permitted in a money-critical module". That is what "do not silence
it" means operationally — the rule is arguable at the edges of the codebase and
absolute at its centre.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from portable_core.lint._common import Finding, iter_repo_files, read_text, repo_root

#: Packages where a ``float`` is never acceptable and the suppression marker is
#: itself an error. These are the modules that touch money, and the boundary is
#: drawn at the package rather than the function so that it does not move
#: quietly.
MONEY_CRITICAL: tuple[str, ...] = (
    "src/portable_core/domain",
    "src/portable_core/services",
    "src/portable_core/persistence",
    "src/portable_core/schema",
    "src/portable_core/formatters",
    "src/portable_core/providers",
    "src/portable_pt",
)

#: Modules exempt from the Python scan entirely: they are about the rule, or
#: about the machine, not about money.
_EXEMPT_PREFIXES: tuple[str, ...] = (
    "src/portable_core/lint/",
    "src/portable_core/native/",
)

_MARKER = re.compile(r"#\s*no-float:\s*allow\b(?P<rest>.*)$")
_SQL_FLOAT_TYPE = re.compile(
    r"\b(?P<kind>REAL|FLOAT|DOUBLE(?:\s+PRECISION)?|NUMERIC)\b",
    re.IGNORECASE,
)
_SQL_COMMENT = re.compile(r"--.*$")


def _is_money_critical(rel: str) -> bool:
    return any(rel == p or rel.startswith(p + "/") for p in MONEY_CRITICAL)


def _marker_on(line: str) -> tuple[bool, str]:
    """Return ``(present, reason)`` for the suppression marker on *line*."""
    m = _MARKER.search(line)
    if m is None:
        return False, ""
    return True, m.group("rest").lstrip(" -:").strip()


class _FloatVisitor(ast.NodeVisitor):
    """Collect every syntactic appearance of binary floating point."""

    def __init__(self) -> None:
        self.hits: list[tuple[int, int, str]] = []

    def visit_Constant(self, node: ast.Constant) -> None:
        # complex is float twice over; bool is an int subclass and is fine.
        if isinstance(node.value, float):
            self.hits.append((node.lineno, node.col_offset, f"float literal {node.value!r}"))
        elif isinstance(node.value, complex):
            self.hits.append((node.lineno, node.col_offset, "complex literal"))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "float":
            self.hits.append((node.lineno, node.col_offset, "use of `float`"))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # numpy.float64, builtins.float, ...
        if node.attr.startswith("float") or node.attr in {"double", "longdouble"}:
            self.hits.append(
                (node.lineno, node.col_offset, f"floating-point attribute `{node.attr}`")
            )
        self.generic_visit(node)


def _check_python(path: Path, rel: str, text: str) -> list[Finding]:
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:  # a broken file is ruff's problem, not ours
        return [
            Finding(
                path,
                exc.lineno or 1,
                exc.offset or 1,
                "PT-LINT-FLOAT-000",
                f"unparseable: {exc.msg}",
            )
        ]

    lines = text.splitlines()
    visitor = _FloatVisitor()
    visitor.visit(tree)

    findings: list[Finding] = []
    critical = _is_money_critical(rel)

    for lineno, col, what in visitor.hits:
        line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
        present, reason = _marker_on(line)
        if present and critical:
            findings.append(
                Finding(
                    path,
                    lineno,
                    col + 1,
                    "PT-LINT-FLOAT-003",
                    "`no-float: allow` is not permitted in a money-critical module "
                    f"({what}). Use Decimal. See CLAUDE.md invariant 1 and ADR 0005.",
                )
            )
            continue
        if present and not reason:
            findings.append(
                Finding(
                    path,
                    lineno,
                    col + 1,
                    "PT-LINT-FLOAT-002",
                    "`no-float: allow` requires a reason: `# no-float: allow -- why`",
                )
            )
            continue
        if present:
            continue
        findings.append(
            Finding(
                path,
                lineno,
                col + 1,
                "PT-LINT-FLOAT-001",
                f"{what} in a money path. Use decimal.Decimal "
                "(CLAUDE.md invariant 1, ADR 0005).",
            )
        )
    return findings


def _check_sql(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        line = _SQL_COMMENT.sub("", raw)
        for m in _SQL_FLOAT_TYPE.finditer(line):
            kind = m.group("kind").upper()
            detail = (
                "SQLite's NUMERIC affinity silently stores as REAL what it cannot hold "
                "as an integer"
                if kind == "NUMERIC"
                else "binary floating point"
            )
            findings.append(
                Finding(
                    path,
                    i,
                    m.start() + 1,
                    "PT-LINT-FLOAT-010",
                    f"column type `{kind}` -- {detail}. Money is TEXT (ADR 0005).",
                )
            )
    return findings


def run(root: Path | None = None) -> list[Finding]:
    """Scan the repository and return every finding, deterministically ordered."""
    root = root or repo_root()
    findings: list[Finding] = []

    src = root / "src"
    if src.is_dir():
        for path in iter_repo_files(src, frozenset({".py", ".sql"})):
            rel = path.relative_to(root).as_posix()
            if any(rel.startswith(p) for p in _EXEMPT_PREFIXES):
                continue
            text = read_text(path)
            if text is None:
                continue
            if path.suffix == ".py":
                findings.extend(_check_python(path, rel, text))
            else:
                findings.extend(_check_sql(path, text))

    return sorted(findings, key=lambda f: (f.path.as_posix(), f.line, f.column, f.code))


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 when clean, 1 when the rule fires."""
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]).resolve() if args else repo_root()
    findings = run(root)
    for finding in findings:
        print(finding.render(root))
    if findings:
        print(f"\nno-float: {len(findings)} finding(s). This rule may not be silenced.")
        return 1
    print("no-float: clean")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
