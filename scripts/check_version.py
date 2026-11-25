#!/usr/bin/env python3
"""Fail a pull request that changes what ships and keeps the version.

Every version that reaches main is a release (ADR 0049): on each push to main,
``.github/workflows/release.yml`` publishes ``v<__version__>`` when it has no
GitHub release yet. So the pull request is where the number is chosen, and this
gate holds it to five rules, against the branch the pull request merges into:

1. The version is ``X.Y.Z`` and never goes down.
2. A change to what an install runs -- ``src/``, ``schemas/``, ``cpp/``,
   ``pyproject.toml`` or ``scripts/xlsx_to_csv.py`` -- raises it.
3. A pull request that adds a schema migration raises the minor version at
   least: each one is a ``pt migrate`` of every ``.port`` file after the pull
   (docs/releasing.md, "Choosing the number"). A migration that only fixes and
   changes nothing a reader sees can say so with a line ``-- release: patch``.
4. A new major version of a published output schema (``schemas/holdings-2.0``
   beside ``holdings-1.0``) raises the minor version at least while the version
   is ``0.y.z``, and the major version from ``1.0.0``: a consumer pinned to the
   old one breaks.
5. A version that rises has its own section in ``CHANGELOG.md``,
   ``## [X.Y.Z] - YYYY-MM-DD``, first after ``## [Unreleased]``, and not empty:
   it is the text of the GitHub release.

Rules 1 to 3 are rtrimble13/fafnir's, rule 4 is dorq's for its output, and rule 5
is alpha-tango's. In CI it reads the base commit from the ``pull_request`` event
(``GITHUB_EVENT_PATH``). Locally, pass it:

    python scripts/check_version.py --base origin/main

Stdlib only, so it runs before anything is installed. Exit status is 1 when the
gate fails, with a message that says which rule and what to run
(``scripts/release.py patch|minor|major``, or ``make bump``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

VERSION_FILE = "src/portable_core/__init__.py"
#: Also reads the annotated form, `__version__: Final[str] = "0.1.0"`, which
#: commits before ADR 0049 used: a base can be one of them.
VERSION_LINE = re.compile(r'^__version__(?:\s*:[^=]+)?\s*=\s*"([^"]*)"$', re.MULTILINE)
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
CHANGELOG = "CHANGELOG.md"

#: What an install of portable runs: a change here is a release.
SHIPS = ("src/", "schemas/", "cpp/")
SHIPS_FILES = ("pyproject.toml", "scripts/xlsx_to_csv.py")

MIGRATION = re.compile(r"^src/portable_core/schema/(\d{4}_[A-Za-z0-9_]+)\.sql$")
PATCH_MARK = re.compile(r"^\s*--\s*release:\s*patch\s*$", re.MULTILINE)
OUTPUT_SCHEMA = re.compile(r"^schemas/(?P<name>.+)-(?P<major>\d+)\.\d+\.json$")
SECTION = re.compile(r"^## \[(?P<name>[^\]]+)\](?P<rest>.*)$", re.MULTILINE)
DATED = re.compile(r"^ - \d{4}-\d{2}-\d{2}$")

Version = tuple[int, int, int]


def ships(path: str) -> bool:
    """Whether a change to ``path`` changes what an install of portable runs."""
    if "/__pycache__/" in path:
        return False
    return path in SHIPS_FILES or path.startswith(SHIPS)


def parse(version: str) -> Version:
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


def new_output_majors(base_schemas: list[str], added: list[str]) -> list[str]:
    """Output schemas whose added file is a new major version of one at the base.

    ``holdings-2.0.json`` beside ``holdings-1.0.json`` is one; ``lot-candidates-
    1.0.json``, a schema the base did not have, is a new command and is not.
    """
    majors: dict[str, int] = {}
    for path in base_schemas:
        found = OUTPUT_SCHEMA.match(path)
        if found:
            name, major = found["name"], int(found["major"])
            majors[name] = max(major, majors.get(name, major))
    raised = []
    for path in added:
        found = OUTPUT_SCHEMA.match(path)
        if found and found["name"] in majors and int(found["major"]) > majors[found["name"]]:
            raised.append(f"{found['name']}-{found['major']}")
    return sorted(set(raised))


def changelog_problem(changelog: str, version: str) -> str | None:
    """Why ``CHANGELOG.md`` does not give *version* its release notes, or None."""
    sections = list(SECTION.finditer(changelog))
    versions = [s for s in sections if s["name"] != "Unreleased"]
    unreleased = [n for n, s in enumerate(sections) if s["name"] == "Unreleased"]
    if not versions or unreleased not in ([], [0]):
        return "its sections must be [Unreleased], if any, then the versions, newest first"
    newest = versions[0]
    if newest["name"] != version:
        return (
            f"its newest version section is [{newest['name']}], not [{version}]. "
            f"Add ## [{version}] - YYYY-MM-DD under ## [Unreleased] "
            "(scripts/release.py does it)"
        )
    if not DATED.match(newest["rest"]):
        return f"## [{version}] needs its date: ## [{version}] - YYYY-MM-DD"
    following = sections.index(newest) + 1
    end = sections[following].start() if following < len(sections) else len(changelog)
    body = changelog[newest.end() : end]
    if not any(line.strip() for line in body.splitlines()):
        return f"## [{version}] is empty: it is the text of the GitHub release"
    return None


def evaluate(
    base_version: str,
    head_version: str,
    paths: list[str],
    added_migrations: dict[str, str],
    raised_schemas: list[str],
    changelog: str,
) -> tuple[bool, str]:
    """Return ``(passed, message)`` for a pull request.

    ``paths`` are the paths it changes; ``added_migrations`` maps each migration
    it adds (``0013_policy_allocation``) to its text; ``raised_schemas`` are the
    output schemas it gives a new major version; ``changelog`` is its
    ``CHANGELOG.md``.
    """
    if not SEMVER.match(head_version):
        return False, (
            f"version: FAILED. __version__ is {head_version!r}, not X.Y.Z "
            "(scripts/release.py sets it)."
        )
    if not SEMVER.match(base_version):
        return False, f"version: FAILED. The base's __version__ {base_version!r} is not X.Y.Z."
    base, head = parse(base_version), parse(head_version)
    if head < base:
        return False, (
            f"version: FAILED. {head_version} is below the base's {base_version}. "
            "Versions only go up."
        )

    if head > base:
        problem = changelog_problem(changelog, head_version)
        if problem:
            return (
                False,
                f"version: FAILED. {head_version} is a release, and {CHANGELOG}: {problem}.",
            )

    shipped = sorted(p for p in paths if ships(p))
    if not shipped:
        return True, (
            f"version: nothing that ships changed; {head_version} stays"
            + (" (a release of its own when merged)." if head > base else ".")
        )
    listing = "\n".join(f"  {p}" for p in shipped)
    if head == base:
        return False, (
            f"version: FAILED. This pull request changes what ships, and keeps the "
            f"version at {base_version}. Merging it publishes a release, so it needs "
            f"its own number:\n{listing}\n"
            "Run scripts/release.py patch|minor|major (make bump BUMP=...; "
            'docs/releasing.md, "Choosing the number"), and commit '
            f"{VERSION_FILE} and {CHANGELOG}."
        )

    unmarked = sorted(n for n, text in added_migrations.items() if not PATCH_MARK.search(text))
    if unmarked and head[:2] == base[:2]:
        return False, (
            f"version: FAILED. {head_version} is a patch of {base_version}, and this pull "
            f"request adds migration(s) {', '.join(unmarked)}. Each is a `pt migrate` of "
            "every .port file after the pull, so it is a minor release at least: "
            "scripts/release.py minor. A migration that only fixes, and changes "
            "nothing a reader sees, can say so with a line '-- release: patch'."
        )

    if raised_schemas:
        names = ", ".join(raised_schemas)
        if base[0] >= 1 and head[0] == base[0]:
            return False, (
                f"version: FAILED. This pull request publishes a new major version of "
                f"output schema(s) {names}, which breaks a consumer pinned to the old "
                f"one. From 1.0.0 that is a major release: scripts/release.py major."
            )
        if head[:2] == base[:2]:
            return False, (
                f"version: FAILED. {head_version} is a patch of {base_version}, and this "
                f"pull request publishes a new major version of output schema(s) {names}. "
                "A consumer pinned to the old one breaks, so it is a minor release at "
                "least while the version is 0.y.z: scripts/release.py minor."
            )

    notes = []
    if added_migrations:
        notes.append(f"adds migration(s) {', '.join(sorted(added_migrations))}")
    if raised_schemas:
        notes.append(f"raises output schema(s) {', '.join(raised_schemas)}")
    return True, (
        f"version: passed. {base_version} -> {head_version} for a change to what ships"
        + (f", and {' and '.join(notes)}" if notes else "")
        + f":\n{listing}\nMerging it publishes v{head_version}."
    )


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, encoding="utf-8"
    ).stdout


def version_at(ref: str) -> str:
    match = VERSION_LINE.search(git("show", f"{ref}:{VERSION_FILE}"))
    return match.group(1) if match else ""


def main(argv: list[str] | None = None) -> int:
    doc = __doc__ or ""
    ap = argparse.ArgumentParser(description=doc.split("\n\n")[0])
    ap.add_argument("--base", help="base commit (default: from the pull_request event)")
    ap.add_argument("--head", default="HEAD", help="head commit (default: HEAD)")
    a = ap.parse_args(argv)

    base = a.base
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if base is None and event_path:
        with Path(event_path).open(encoding="utf-8") as f:
            pr = json.load(f).get("pull_request") or {}
        base = (pr.get("base") or {}).get("sha")
    if not base:
        ap.error("no base commit: pass --base, or run on a pull_request event")

    # base...head: what this pull request changes, from where it forked. The
    # version it must beat is the base's own, which is what main will release.
    # --no-renames lists a moved file under both names, so a move out of src/
    # still counts as a change to src/.
    span = f"{base}...{a.head}"
    paths = [p for p in git("diff", "--name-only", "--no-renames", span).splitlines() if p]
    added = [p for p in git("diff", "--name-only", "--diff-filter=A", span).splitlines() if p]
    migrations = {}
    for path in added:
        match = MIGRATION.match(path)
        if match:
            migrations[match.group(1)] = git("show", f"{a.head}:{path}")
    base_schemas = git("ls-tree", "-r", "--name-only", base, "--", "schemas").splitlines()
    raised = new_output_majors(base_schemas, added)

    passed, message = evaluate(
        version_at(base),
        version_at(a.head),
        paths,
        migrations,
        raised,
        git("show", f"{a.head}:{CHANGELOG}"),
    )
    print(message)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
