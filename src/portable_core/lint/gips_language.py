"""Lint rule: no compliance claim about the GIPS standards, anywhere.

``PORT-GIPS-J05``; ``CLAUDE.md`` invariant 11.

GIPS for Firms **1.A.9** prohibits statements describing the calculation
methodology as being in accordance, in compliance, or consistent with the
Global Investment Performance Standards, or similar statements; **1.A.8**
prohibits any claim of partial compliance. Those provisions are quoted verbatim
in ``docs/gips-standard.md`` ``PORT-GIPS-J05``, which is the allow-listed place
for them; :data:`PROHIBITED` below carries them as patterns.

The prohibition is addressed to firms, and `portable`'s owner is not a firm --
but those forms of words are prohibited *precisely because CFA Institute judges
them misleading*, and a tool built to this standard should not adopt the one
construction the standard singles out.

Compliance is entity-wide and "cannot be met on a composite, pooled fund, or
portfolio basis" (Firms 1.A.1; Asset Owners 21.A.1). It is not a property a
piece of software can have.

The allow-list is exactly three things
--------------------------------------

1. ``docs/gips-standard.md`` — the standard itself, which must quote the
   prohibitions in order to state them.
2. The approved disclaimer from that document's §9.3, wherever it appears,
   matched against :data:`portable_core.disclaimer.GIPS_DISCLAIMER` with
   whitespace normalised so it may be wrapped, indented, or line-broken.
3. Any line carrying an explicit ``gips-lint: allow`` marker.

The marker exists so that ``CLAUDE.md`` invariant 11, this module's docstring,
and this rule's own test fixture can name the prohibited phrases in order to
forbid them. **Using it anywhere else is silencing the rule.**
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from portable_core.disclaimer import GIPS_DISCLAIMER
from portable_core.lint._common import Finding, iter_repo_files, read_text, repo_root

#: The prohibited constructions, as written in `docs/gips-standard.md`
#: PORT-GIPS-J05. Each carries the reason it is prohibited, which is printed
#: with the finding so that whoever trips it learns why rather than reaching
#: for the marker.
PROHIBITED: tuple[tuple[str, str], ...] = (
    (
        r"(?:\bin\s+)?\b(?:compliance|accordance|conformity)\s+with\s+(?:the\s+)?GIPS\b",
        "compliance is entity-wide and cannot be claimed for a portfolio (1.A.1 / 21.A.1)",
    ),
    (
        r"\bGIPS[-\u2010-\u2015\u2212 ]compliant\b",
        "compliance is entity-wide and cannot be claimed for a portfolio (1.A.1 / 21.A.1)",
    ),
    (
        r"\bGIPS[-\u2010-\u2015\u2212 ]consistent\b",
        "named verbatim in the prohibition at 1.A.9",
    ),
    (
        r"\bconsistent\s+with\s+(?:the\s+)?GIPS\b",
        "named verbatim in the prohibition at 1.A.9",
    ),
    (
        r"\bcomplies\s+with\s+(?:the\s+)?GIPS\b",
        "compliance is entity-wide and cannot be claimed for a portfolio (1.A.1 / 21.A.1)",
    ),
    (
        r"\bGIPS[-\u2010-\u2015\u2212 ]verified\b",
        "verification is an engagement performed by an independent verifier on an entity",
    ),
)

#: The hyphen class used above spans ASCII hyphen-minus, the Unicode dash
#: range U+2010..U+2015, the minus sign U+2212, and a space. A word
#: processor silently substitutes a typographic dash, and the substituted
#: form is exactly as much of a claim as the ASCII one.
_COMPILED: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pat, re.IGNORECASE), why) for pat, why in PROHIBITED
)

#: Paths allow-listed wholesale, relative to the repository root.
ALLOWLISTED_PATHS: frozenset[str] = frozenset({"docs/gips-standard.md"})

_MARKER = re.compile(r"gips-lint:\s*allow\b", re.IGNORECASE)

#: What may appear *between* the disclaimer's tokens without breaking the match:
#: whitespace and the punctuation a formatter adds when laying text out --
#: block-quote and comment markers, table pipes, emphasis, and the quotes,
#: commas, backslashes and parentheses of adjacent Python string literals.
#: None of these characters occurs inside any word of the disclaimer.
_DISCLAIMER_SEPARATOR: str = r"""[\s"'\\,()>#*|]*"""

#: Scanned suffixes -- "source, docs, templates, or fixtures", plus config and
#: anything else that can end up in front of a reader.
_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".pyi",
        ".sql",
        ".md",
        ".rst",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".cfg",
        ".ini",
        ".csv",
        ".tsv",
        ".html",
        ".j2",
        ".jinja",
        ".sh",
        ".ps1",
        ".bat",
        ".cpp",
        ".hpp",
        ".h",
        ".cmake",
        "",
    }
)

#: Extensionless files worth scanning; everything else without a suffix is skipped.
_EXTENSIONLESS: frozenset[str] = frozenset({"Makefile", "CMakeLists.txt", "LICENSE"})


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _disclaimer_pattern() -> re.Pattern[str]:
    """A separator-flexible matcher for the approved disclaimer.

    The disclaimer is allow-listed because it *itself* names one of the
    prohibited constructions, in the course of denying it. The negation is the
    entire point of the text, and the rule must not fire on the one wording
    that exists in order to say the right thing.

    Tokens are joined by :data:`_DISCLAIMER_SEPARATOR` rather than by ``\\s+``
    so that the same text is recognised however a formatter has laid it out: as
    wrapped prose, as a Markdown block quote, as a comment block, as a table
    cell, or as the adjacent Python string literals of its own definition in
    :mod:`portable_core.disclaimer`. The match stays tight despite that --
    every one of its hundred-odd tokens must still appear, in order, separated
    by nothing but layout punctuation. A partial quotation does not match, and
    therefore does not inherit the exemption.
    """
    return re.compile(
        _DISCLAIMER_SEPARATOR.join(re.escape(tok) for tok in GIPS_DISCLAIMER.split()),
        re.IGNORECASE,
    )


_DISCLAIMER_RE = _disclaimer_pattern()


def _allowed_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _DISCLAIMER_RE.finditer(text)]


def _line_col(text: str, offset: int) -> tuple[int, int]:
    line = text.count("\n", 0, offset) + 1
    start = text.rfind("\n", 0, offset) + 1
    return line, offset - start + 1


def _check(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    spans = _allowed_spans(text)
    lines = text.splitlines()

    for pattern, why in _COMPILED:
        for match in pattern.finditer(text):
            start = match.start()
            if any(lo <= start < hi for lo, hi in spans):
                continue  # inside the approved disclaimer
            line, col = _line_col(text, start)
            raw = lines[line - 1] if 0 < line <= len(lines) else ""
            if _MARKER.search(raw):
                continue  # explicitly marked
            findings.append(
                Finding(
                    path,
                    line,
                    col,
                    "PT-LINT-GIPS-001",
                    f"prohibited compliance language {match.group(0)!r} -- {why}. "
                    "See docs/gips-standard.md §9.2 and PORT-GIPS-J05; the one approved "
                    "form of words is §9.3.",
                )
            )
    return findings


def run(root: Path | None = None) -> list[Finding]:
    """Scan the whole repository and return every finding."""
    root = root or repo_root()
    findings: list[Finding] = []

    for path in iter_repo_files(root):
        rel = path.relative_to(root).as_posix()
        if rel in ALLOWLISTED_PATHS:
            continue
        if path.suffix.lower() not in _SUFFIXES:
            continue
        if not path.suffix and path.name not in _EXTENSIONLESS:
            continue
        text = read_text(path)
        if text is None:
            continue
        findings.extend(_check(path, text))

    return sorted(findings, key=lambda f: (f.path.as_posix(), f.line, f.column))


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 when clean, 1 when the rule fires."""
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]).resolve() if args else repo_root()
    findings = run(root)
    for finding in findings:
        print(finding.render(root))
    if findings:
        print(
            f"\ngips-language: {len(findings)} finding(s). This rule may not be silenced. "
            "The allow-list is docs/gips-standard.md, the approved disclaimer, and an "
            "explicit marker -- and a marker anywhere else is silencing the rule."
        )
        return 1
    print("gips-language: clean")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
