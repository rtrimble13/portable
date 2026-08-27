"""Tests for the two project lint rules.

Both rules run in `make lint` and in CI, and neither may be silenced. A lint
rule with no test is a lint rule that has silently stopped firing, so these
assert the rules catch what they must, allow exactly what they must, and stay
clean against the repository as it actually stands.

GIPS acceptance test: `test_no_prohibited_gips_language` (PORT-GIPS-J05).
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest

from portable_core.disclaimer import GIPS_DISCLAIMER, WRAP_KWARGS
from portable_core.lint import gips_language, no_float
from portable_core.lint._common import repo_root

FIXTURES = Path(__file__).parent.parent / "fixtures" / "lint"
ROOT = repo_root()

pytestmark = pytest.mark.unit


def _stage(tmp_path: Path, fixture: str, as_name: str) -> Path:
    """Copy a `.fixture` file into a temp tree under a real, scannable name."""
    dest = tmp_path / as_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / fixture, dest)
    return dest


# ── PORT-GIPS-J05: compliance-language rule ──────────────────────────────────


@pytest.mark.gips
def test_no_prohibited_gips_language() -> None:
    """PORT-GIPS-J05 -- the repository itself carries no compliance claim.

    This is the acceptance test named in docs/gips-standard.md. It is the one
    that matters: the fixtures below prove the rule *works*, this proves the
    repository is *clean*.
    """
    findings = gips_language.run(ROOT)
    assert findings == [], "\n".join(f.render(ROOT) for f in findings)


@pytest.mark.gips
def test_gips_rule_rejects_every_prohibited_phrase(tmp_path: Path) -> None:
    """Every pattern in the rule's table must fire against the fixture.

    The expected phrases are derived from `gips_language.PROHIBITED` rather
    than spelled out here, for two reasons: it asserts the stronger property
    (no pattern is dead), and it keeps this file from becoming another place
    the prohibited wording lives. The wording belongs in exactly two places --
    docs/gips-standard.md, and the fixture that exists to be rejected.
    """
    _stage(tmp_path, "prohibited_gips_language.md.fixture", "docs/report.md")
    findings = gips_language.run(tmp_path)
    caught = " ".join(f.message for f in findings)

    unfired = [
        pattern.pattern
        for pattern, _why in gips_language._COMPILED
        if not pattern.search(caught)
    ]
    assert not unfired, f"patterns that never fired: {unfired}"
    assert len(findings) >= len(gips_language.PROHIBITED)


@pytest.mark.gips
def test_gips_marker_suppresses_exactly_one_line_and_not_the_file(tmp_path: Path) -> None:
    """A marker suppresses its own line. It does not exempt the file."""
    _stage(tmp_path, "marker_suppresses_one_line.md.fixture", "docs/notes.md")
    findings = gips_language.run(tmp_path)

    assert len(findings) == 1, [f.render(tmp_path) for f in findings]
    assert findings[0].line == 4, "the unmarked line, not the marked one"


@pytest.mark.gips
def test_approved_disclaimer_is_allowed_wherever_it_appears(tmp_path: Path) -> None:
    """The one approved form of words must not trip the rule that protects it.

    The disclaimer names one of the prohibited constructions in the course of
    denying it. The negation is the entire point of the text, so the rule
    allow-lists the canonical string itself, matched with separators relaxed so
    that wrapping or indenting it at a call site does not defeat the match.
    """
    wrapped = textwrap.fill(
        GIPS_DISCLAIMER,
        width=72,
        initial_indent="> ",
        subsequent_indent="> ",
        **WRAP_KWARGS,  # type: ignore[arg-type]
    )
    (tmp_path / "footer.md").write_text(f"# Report footer\n\n{wrapped}\n", encoding="utf-8")
    assert gips_language.run(tmp_path) == []

    # ...and as adjacent Python string literals, which is how it is defined.
    literal = "\n".join(
        f'    "{chunk} "'
        for chunk in textwrap.wrap(GIPS_DISCLAIMER, width=64, **WRAP_KWARGS)  # type: ignore[arg-type]
    )
    (tmp_path / "footer.py").write_text(f"TEXT = (\n{literal}\n)\n", encoding="utf-8")
    assert gips_language.run(tmp_path) == []


@pytest.mark.gips
def test_disclaimer_allowlist_does_not_excuse_a_partial_quotation(tmp_path: Path) -> None:
    """Quoting a fragment of the disclaimer does not inherit its exemption.

    The allow-list is the whole canonical text, in order. Lifting the clause
    that names a prohibited construction, without the sentence that denies it,
    is exactly the misuse the rule exists to catch.
    """
    fragment = GIPS_DISCLAIMER[GIPS_DISCLAIMER.index("This is not") :].split(".")[0]
    (tmp_path / "blurb.md").write_text(f"Our methodology: {fragment}.\n", encoding="utf-8")
    assert len(gips_language.run(tmp_path)) == 1


@pytest.mark.gips
def test_gips_standard_document_is_the_only_allowlisted_path() -> None:
    """Widening the path allow-list is silencing the rule. Pin it."""
    assert set(gips_language.ALLOWLISTED_PATHS) == {"docs/gips-standard.md"}


# ── no-float rule ────────────────────────────────────────────────────────────


def test_no_float_rule_is_clean_against_the_repository() -> None:
    """CLAUDE.md invariant 1 -- no binary floating point in a money path."""
    findings = no_float.run(ROOT)
    assert findings == [], "\n".join(f.render(ROOT) for f in findings)


def test_no_float_rejects_literals_annotations_and_casts(tmp_path: Path) -> None:
    _stage(tmp_path, "float_in_money_path.py.fixture", "src/portable_core/services/bad.py")
    findings = no_float.run(tmp_path)

    codes = {f.code for f in findings}
    assert codes == {"PT-LINT-FLOAT-003"} or "PT-LINT-FLOAT-001" in codes
    # 0.0725, two `float` annotations, float() cast, 1.5
    assert len(findings) >= 5, [f.render(tmp_path) for f in findings]


def test_no_float_marker_requires_a_reason(tmp_path: Path) -> None:
    """A bare marker is itself a finding: silencing needs a stated reason."""
    _stage(tmp_path, "float_marker.py.fixture", "src/portable_core/cli/timeouts.py")
    findings = no_float.run(tmp_path)

    by_line = {f.line: f.code for f in findings}
    assert 2 not in by_line, "a marker with a reason suppresses its line"
    assert by_line[3] == "PT-LINT-FLOAT-002", "a bare marker is a finding"
    assert by_line[4] == "PT-LINT-FLOAT-001", "an unmarked float is a finding"


def test_no_float_marker_is_rejected_inside_a_money_critical_module(tmp_path: Path) -> None:
    """ "Do not silence it" is operational: the marker has no force in the core.

    This is the difference between a rule that is arguable at the edges of the
    codebase and absolute at its centre.
    """
    _stage(tmp_path, "float_marker.py.fixture", "src/portable_core/services/rates.py")
    findings = no_float.run(tmp_path)

    codes = {f.line: f.code for f in findings}
    assert codes[2] == "PT-LINT-FLOAT-003", "marker not permitted in a money-critical module"
    assert codes[3] == "PT-LINT-FLOAT-003"


@pytest.mark.parametrize("kind", ["REAL", "NUMERIC", "DOUBLE PRECISION", "FLOAT"])
def test_no_float_rejects_floating_sql_column_types(tmp_path: Path, kind: str) -> None:
    """NUMERIC is included deliberately: SQLite's affinity makes it REAL.

    ADR 0005. This is the bug wearing a disguise, and it is the reason every
    money column in the schema is declared TEXT.
    """
    _stage(tmp_path, "real_column.sql.fixture", "src/portable_core/schema/0099_bad.sql")
    findings = no_float.run(tmp_path)

    assert {f.code for f in findings} == {"PT-LINT-FLOAT-010"}
    assert any(kind.split()[0] in f.message for f in findings)


def test_money_critical_packages_are_pinned() -> None:
    """Shrinking this list weakens the rule where it matters most."""
    assert set(no_float.MONEY_CRITICAL) >= {
        "src/portable_core/domain",
        "src/portable_core/services",
        "src/portable_core/persistence",
        "src/portable_core/schema",
        "src/portable_core/formatters",
        "src/portable_core/providers",
        "src/portable_pt",
    }


@pytest.mark.gips
def test_disclaimer_wrapping_must_not_break_hyphenated_words() -> None:
    """Default wrapping splits `asset-owner-wide` and defeats the allow-list.

    That failure mode is invisible until the rule fires on a correctly-worded
    report, so the constraint is pinned here rather than left as a comment.
    """
    assert WRAP_KWARGS["break_on_hyphens"] is False

    naive = textwrap.fill(GIPS_DISCLAIMER, width=72)
    correct = textwrap.fill(GIPS_DISCLAIMER, width=72, **WRAP_KWARGS)  # type: ignore[arg-type]
    assert gips_language._DISCLAIMER_RE.search(naive) is None
    assert gips_language._DISCLAIMER_RE.search(correct) is not None
