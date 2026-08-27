"""Every published JSON Schema is valid, and real command output validates.

Bootstrap §6.6: "Publish JSON Schema documents for every command's
`--format json` output under `schemas/`, versioned, and validate outputs
against them in tests."

Validating a hand-written example would prove nothing. These run the real
commands against the real fixture portfolio and validate what actually comes
out, so a change to an envelope field fails here rather than in a consumer.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012 as DRAFT

from portable_core import OUTPUT_SCHEMA_VERSION
from portable_core.lint._common import repo_root

pytestmark = pytest.mark.unit

ROOT = repo_root()
SCHEMAS = ROOT / "schemas"
SAMPLE = ROOT / "examples" / "sample.port"


def _load(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
    return loaded


def _registry() -> Registry:
    """Every published schema, resolvable by both its `$id` and its filename.

    The per-command schemas `$ref` the envelope by bare filename, which is what
    makes them readable next to each other on disk. Registering both forms
    means neither the file layout nor the `$id` has to change to keep the other
    working.
    """
    registry = Registry()
    for path in sorted(SCHEMAS.glob("*.json")):
        contents = _load(path.name)
        resource = Resource.from_contents(contents, default_specification=DRAFT)
        registry = registry.with_resources([(contents["$id"], resource), (path.name, resource)])
    return registry


def _validator(name: str) -> Draft202012Validator:
    """A validator that resolves cross-schema refs from the published files."""
    return Draft202012Validator(_load(name), registry=_registry())


def _run(*args: str) -> dict[str, Any]:
    """Invoke the real CLI and parse its stdout.

    Through `python -m` rather than the installed script so the test runs
    against the working tree on every platform.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "portable_pt", "--format", "json", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin", "NO_COLOR": "1"},
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload: dict[str, Any] = json.loads(completed.stdout)
    return payload


# ── the schemas themselves ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name", sorted(p.name for p in (repo_root() / "schemas").glob("*.json"))
)
def test_every_published_schema_is_itself_valid(name: str) -> None:
    Draft202012Validator.check_schema(_load(name))


def test_the_envelope_version_matches_the_code() -> None:
    """A schema file that drifts from the code is worse than none."""
    envelope = _load("envelope-1.0.json")
    assert envelope["$id"].endswith(f"envelope-{OUTPUT_SCHEMA_VERSION}.json")


def test_the_envelope_requires_the_disclaimer_key() -> None:
    """Present and null, never absent.

    Absent and null must not look the same to a consumer: one means "this
    command emits no return", the other means the field went missing.
    """
    assert "disclaimer" in _load("envelope-1.0.json")["required"]


def test_decimals_are_specified_as_strings_not_numbers() -> None:
    """The contract a consumer depends on.

    A JSON number cannot round-trip a decimal. If the schema said `number`,
    a generated client would parse to float and silently lose the guarantee.
    """
    decimal_def = _load("envelope-1.0.json")["$defs"]["decimal"]
    assert decimal_def["type"] == "string"


# ── real command output ──────────────────────────────────────────────────────


@pytest.mark.skipif(not SAMPLE.exists(), reason="run `make fixtures` first")
@pytest.mark.parametrize(
    ("schema", "args"),
    [
        (
            "holdings-1.0.json",
            ("--port", "examples/sample.port", "holdings", "--as-of", "2025-06-30"),
        ),
        ("tax-1.0.json", ("--port", "examples/sample.port", "tax", "--year", "2025")),
        (
            "cash-flows-1.0.json",
            (
                "--port",
                "examples/sample.port",
                "cash-flows",
                "--level",
                "portfolio",
                "--external-only",
            ),
        ),
        ("introspect-1.0.json", ("introspect",)),
    ],
)
def test_real_command_output_validates(schema: str, args: tuple[str, ...]) -> None:
    payload = _run(*args)
    errors = sorted(_validator(schema).iter_errors(payload), key=str)
    assert not errors, "\n".join(
        f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in errors[:5]
    )


@pytest.mark.skipif(not SAMPLE.exists(), reason="run `make fixtures` first")
@pytest.mark.gips
def test_the_tax_report_cannot_drop_its_wash_sale_statement() -> None:
    """The schema requires it, so removing it breaks CI rather than a filing.

    `excludes_wash_sales` is a const in the schema: flipping it to false
    requires changing the schema too, which is a review somebody has to do.
    """
    payload = _run("--port", "examples/sample.port", "tax", "--year", "2025")

    assert payload["disclaimer"]
    assert "wash sale" in payload["disclaimer"].lower()
    assert "not tax advice" in payload["disclaimer"].lower()
    assert payload["data"]["excludes_wash_sales"] is True

    errors = list(_validator("tax-1.0.json").iter_errors(payload))
    assert not errors, [e.message for e in errors]


@pytest.mark.skipif(not SAMPLE.exists(), reason="run `make fixtures` first")
def test_output_is_byte_identical_across_runs_except_for_the_clock() -> None:
    """CLAUDE.md invariant 6, asserted through the real CLI."""
    first = _run("--port", "examples/sample.port", "holdings", "--as-of", "2025-06-30")
    second = _run("--port", "examples/sample.port", "holdings", "--as-of", "2025-06-30")

    for payload in (first, second):
        payload.pop("generated_at")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
