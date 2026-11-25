#!/usr/bin/env python3
"""Set the version a pull request releases, and tell the release workflow what to publish.

Every version that reaches main is a release (ADR 0049): merging the pull request
publishes it. This is rtrimble13/fafnir's ``scripts/release.sh``, in Python so it
runs the same on the owner's Windows machine as on CI's Linux.

Usage:
  scripts/release.py                    this checkout's version, main's, the last
                                        release, and what has changed since it
  scripts/release.py patch              0.2.0 on main -> 0.2.1 on this branch
  scripts/release.py minor              0.2.0 on main -> 0.3.0
  scripts/release.py major              0.2.0 on main -> 1.0.0
  scripts/release.py 0.4.0              set it explicitly
      --dry-run                         say what would happen and change nothing
      --date YYYY-MM-DD                 the CHANGELOG section's date (default: today)
  scripts/release.py plan               for the release workflow: the version due
  scripts/release.py notes X.Y.Z        for the release workflow: its notes

A bump runs on a branch, with the change that needs it, and counts from the
version on main (origin/main), so running it again gives the same number. It
rewrites the one line that carries the number (src/portable_core/__init__.py),
turns CHANGELOG.md's [Unreleased] section into the release's, checks the result
with scripts/check_version.py, the gate the pull request meets, and checks that
every version in the repository agrees. Commit both files with the change.
Nothing here commits, tags or pushes: when the pull request merges,
.github/workflows/release.yml checks the merge commit says this number, and
creates the tag vX.Y.Z and the GitHub release.

Stdlib only.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "src" / "portable_core" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
MAIN_REF = "origin/main"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
CONSTANT = re.compile(r'^__version__ = "([^"]*)"$', re.MULTILINE)
UNRELEASED = re.compile(r"^## \[Unreleased\][^\n]*\n", re.MULTILINE)
#: GitHub refuses a release body over 125,000 characters. Past this the notes
#: point at the CHANGELOG section instead of carrying it.
NOTES_LIMIT = 100_000


class ReleaseError(Exception):
    """Something the person running this has to fix; the message says what."""


def _gate() -> object:
    """scripts/check_version.py, the gate the pull request meets, as a module."""
    spec = importlib.util.spec_from_file_location(
        "check_version", ROOT / "scripts" / "check_version.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(*args: str, check: bool = True) -> str:
    done = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, encoding="utf-8", check=False
    )
    if check and done.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.strip() if done.returncode == 0 else ""


def version_in(text: str) -> str:
    """The version a source file says, in either form ADR 0049 has read."""
    found = re.search(r'^__version__(?:\s*:[^=]+)?\s*=\s*"([^"]*)"$', text, re.MULTILINE)
    return found.group(1) if found else ""


def version_at(ref: str) -> str:
    return version_in(git("show", f"{ref}:src/portable_core/__init__.py", check=False))


def bumped(base: str, bump: str) -> str:
    """The version *bump* gives, counted from *base*."""
    major, minor, patch = (int(part) for part in base.split("."))
    if bump == "major":
        new = f"{major + 1}.0.0"
    elif bump == "minor":
        new = f"{major}.{minor + 1}.0"
    elif bump == "patch":
        new = f"{major}.{minor}.{patch + 1}"
    else:
        new = bump.removeprefix("v")
    if not SEMVER.match(new):
        raise ReleaseError(
            f"{new!r} is not X.Y.Z. Releases are three numbers; the tag adds the v."
        )
    if tuple(int(p) for p in new.split(".")) <= (major, minor, patch):
        raise ReleaseError(
            f"{new} does not come after {base}, the version on main. Versions only go up."
        )
    return new


def with_version(source: str, new: str) -> str:
    """*source* with its one version line set to *new*."""
    if len(CONSTANT.findall(source)) != 1:
        raise ReleaseError(
            'src/portable_core/__init__.py must have exactly one `__version__ = "X.Y.Z"`'
        )
    return CONSTANT.sub(f'__version__ = "{new}"', source)


def with_section(changelog: str, new: str, earlier: str | None, on: date) -> str:
    """*changelog* with a dated section for *new*, first after [Unreleased].

    A section already there for *new* is kept. One for *earlier* -- a version
    this branch set before, above main's -- is renamed, since the branch now
    releases *new* instead. Otherwise [Unreleased] becomes the release's
    section, and an empty [Unreleased] opens above it for what comes next.
    """
    heading = f"## [{new}] - {on.isoformat()}"
    if re.search(rf"^## \[{re.escape(new)}\]", changelog, re.MULTILINE):
        return changelog
    if earlier:
        renamed, count = re.subn(
            rf"^## \[{re.escape(earlier)}\][^\n]*$",
            heading,
            changelog,
            count=1,
            flags=re.MULTILINE,
        )
        if count:
            return renamed
    found = UNRELEASED.search(changelog)
    if found is None:
        raise ReleaseError(
            "CHANGELOG.md has no ## [Unreleased] section to release. Write what this "
            "release changes there first: it is the text of the GitHub release."
        )
    return (
        changelog[: found.start()]
        + f"## [Unreleased]\n\n{heading}\n"
        + changelog[found.end() :]
    )


# ── the report ───────────────────────────────────────────────────────────────


def report() -> None:
    current = version_in(VERSION_FILE.read_text(encoding="utf-8"))
    main = version_at(MAIN_REF)
    last = git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", check=False)
    print(f"this checkout   : {current}")
    print(f"on main         : {main or f'(no {MAIN_REF}: git fetch origin main)'}")
    print(f"last release    : {last or '(none yet)'}")
    print(f"this commit     : {git('describe', '--tags', '--always', '--dirty', check=False)}")
    if last:
        count = git("rev-list", "--count", "--first-parent", f"{last}..HEAD", check=False)
        print(f"since {last:<9} : {count} commit(s) on the first parent")
        added = _migrations(last, "HEAD")
        if added:
            print(f"new migrations  : {' '.join(added)}")
    print()
    if main and current != main:
        print(f"Merging this branch publishes v{current}.")
    else:
        print("A pull request that changes what ships sets its version on its branch,")
        print("and merging it publishes that release:")
        print("    scripts/release.py {patch|minor|major|X.Y.Z}    (make bump BUMP=...)")


def _migrations(since: str, until: str) -> list[str]:
    gate = _gate()
    added = git("diff", "--name-only", "--diff-filter=A", since, until, check=False)
    return [m.group(1) for p in added.splitlines() if (m := gate.MIGRATION.match(p))]  # type: ignore[attr-defined]


# ── the bump ─────────────────────────────────────────────────────────────────


def bump(target: str, *, dry_run: bool, on: date) -> None:
    main = version_at(MAIN_REF)
    if not main:
        raise ReleaseError(f"no {MAIN_REF} to count from: git fetch origin main")
    if git("rev-parse", "--abbrev-ref", "HEAD") == "main":
        raise ReleaseError(
            "on main. A version goes through a pull request like any change: branch first."
        )
    new = bumped(main, target)
    source = VERSION_FILE.read_text(encoding="utf-8")
    changelog = CHANGELOG.read_text(encoding="utf-8")
    current = version_in(source)
    earlier = current if current not in {main, new} else None

    print(f"==> {main} on main  ->  {new} on this branch (merging publishes v{new})")
    if dry_run:
        print("    dry run: nothing will be changed")
        print("    would set src/portable_core/__init__.py and CHANGELOG.md, then run:")
        print(f"    python scripts/check_version.py --base {MAIN_REF}")
        print("    python -m pytest tests/unit/test_version.py -m 'not needs_fresh_install'")
        return

    VERSION_FILE.write_text(with_version(source, new), encoding="utf-8", newline="\n")
    CHANGELOG.write_text(
        with_section(changelog, new, earlier, on), encoding="utf-8", newline="\n"
    )

    def put_back(why: str) -> None:
        VERSION_FILE.write_text(source, encoding="utf-8", newline="\n")
        CHANGELOG.write_text(changelog, encoding="utf-8", newline="\n")
        raise ReleaseError(f"{why}; both files are as they were")

    # The gate reads committed trees, so stash the rewrite into a commit object
    # that no branch points at.
    print(f"--> Checking the version rules against {MAIN_REF} (scripts/check_version.py)")
    tree = git("stash", "create", check=False) or "HEAD"
    gate = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_version.py"), "--base", MAIN_REF,
         "--head", tree],
        cwd=ROOT,
        check=False,
    )  # fmt: skip
    if gate.returncode != 0:
        put_back(f"the version rules refuse {new}")

    # `needs_fresh_install` is deselected on purpose: those tests read what pip
    # recorded and the number a built extension was compiled with, and the
    # constant both were made from has just changed. The release workflow
    # installs before it runs them.
    print("--> Checking every version in the repository agrees")
    # Both, in this interpreter: the tests import portable, so a pytest with no
    # portable beside it (a system or pipx one) cannot run them.
    if (
        importlib.util.find_spec("pytest") is None
        or importlib.util.find_spec("portable_core") is None
    ):
        print("    SKIPPED: this interpreter cannot import both pytest and portable.")
        print("    Run it as `make bump`, or `make install` first.")
        print("    The release workflow still checks this before it tags.")
    else:
        tests = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests/unit/test_version.py",
             "-m", "not needs_fresh_install"],
            cwd=ROOT,
            check=False,
        )  # fmt: skip
        if tests.returncode != 0:
            put_back("the version consistency tests failed")

    print()
    print("Commit src/portable_core/__init__.py and CHANGELOG.md with the change that")
    print(f"needs them. Merging the pull request publishes v{new}: the release workflow")
    print("tags the merge commit and writes what to run after the pull into the notes.")
    print("Reinstall here (`make install`) so pip's record of the version catches up.")


# ── for the release workflow ─────────────────────────────────────────────────


def plan() -> None:
    """``version=`` and ``tagged=``, for ``$GITHUB_OUTPUT``."""
    version = version_in(VERSION_FILE.read_text(encoding="utf-8"))
    if not SEMVER.match(version):
        raise ReleaseError(f"__version__ {version!r} is not X.Y.Z")
    head = git("rev-parse", "HEAD")
    tagged = git("rev-parse", "-q", "--verify", f"refs/tags/v{version}^{{commit}}", check=False)
    # A tag already there (pushed by hand) must name this commit: the checks
    # the workflow runs are on this commit, not another.
    if tagged and tagged != head:
        raise ReleaseError(
            f"v{version} is tagged at {tagged}, not at {head}. Publish that tag's "
            "release by hand, or raise the version."
        )
    print(f"version={version}")
    print(f"tagged={'true' if tagged else 'false'}")


@dataclass(frozen=True)
class Changes:
    """What a release changes that the owner acts on after `git pull`."""

    previous: str | None
    reinstall: list[str]
    migrations: list[str]
    raised_schemas: list[str]


def changes_since(version: str) -> Changes:
    gate = _gate()
    previous = (
        git(
            "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", "--exclude",
            f"v{version}", "HEAD", check=False,
        )
        or None
    )  # fmt: skip
    if previous is None:
        return Changes(None, ["this is the first release"], [], [])
    changed = git("diff", "--name-only", previous, "HEAD", check=False).splitlines()
    reinstall = sorted(
        {
            p
            for p in changed
            if p in {"pyproject.toml", "requirements-dev.txt", "constraints.txt"}
        }
        | {"cpp/" for p in changed if p.startswith("cpp/")}
    )
    added = git("diff", "--name-only", "--diff-filter=A", previous, "HEAD", check=False)
    schemas = git("ls-tree", "-r", "--name-only", previous, "--", "schemas", check=False)
    return Changes(
        previous=previous,
        reinstall=reinstall,
        migrations=_migrations(previous, "HEAD"),
        raised_schemas=gate.new_output_majors(schemas.splitlines(), added.splitlines()),  # type: ignore[attr-defined]
    )


def section_of(changelog: str, version: str) -> str:
    """The body of *version*'s CHANGELOG section, without its heading."""
    found = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n", changelog, re.MULTILINE)
    if found is None:
        raise ReleaseError(f"CHANGELOG.md has no ## [{version}] section")
    following = re.compile(r"^## \[", re.MULTILINE).search(changelog, found.end())
    return changelog[found.end() : following.start() if following else len(changelog)].strip()


def notes(version: str, changes: Changes, changelog: str, repository: str) -> str:
    """The release's notes: what to run after the pull, then what changed."""
    lines = ["## Upgrading", "", "```bash", "git pull"]
    if changes.reinstall:
        lines.append(f"make install    # {', '.join(changes.reinstall)}")
    if changes.migrations:
        lines.append("pt --port FILE migrate    # every .port file; it takes a backup first")
    elif changes.previous is None:
        # The first release: a file an earlier checkout wrote may be at any
        # schema since, and pt refuses it until it is migrated.
        lines.append("pt --port FILE migrate    # each .port file an earlier checkout wrote")
    lines += [f"pt --version    # {version}", "```", ""]
    if changes.migrations:
        names = ", ".join(f"`{m}`" for m in changes.migrations)
        since = f" since {changes.previous}" if changes.previous else ""
        lines += [f"Schema migrations{since}: {names}.", ""]
    if changes.raised_schemas:
        names = ", ".join(f"`{s}.0`" for s in changes.raised_schemas)
        lines += [
            f"New major versions of output schemas: {names}. A consumer of "
            "`--format json` pinned to the old one has to move.",
            "",
        ]
    link = f"https://github.com/{repository}/blob/v{version}/CHANGELOG.md"
    body = section_of(changelog, version)
    lines += ["## What changed", ""]
    if len(body) > NOTES_LIMIT:
        lines += [
            f"This release's changelog is {len(body):,} characters, more than a GitHub "
            f"release holds. Read it in [CHANGELOG.md]({link}) at the tag.",
            "",
        ]
    else:
        lines += [body, "", f"From [CHANGELOG.md]({link}).", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    doc = __doc__ or ""
    ap = argparse.ArgumentParser(
        description=doc.split("\n\n")[0],
        epilog="See docs/releasing.md.",
    )
    ap.add_argument("action", nargs="?", help="patch | minor | major | X.Y.Z | plan | notes")
    ap.add_argument("version", nargs="?", help="for notes: the version being released")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date", type=date.fromisoformat, default=None)
    ap.add_argument("--repository", default="rtrimble13/portable")
    a = ap.parse_args(argv)
    try:
        if a.action is None:
            report()
        elif a.action == "plan":
            plan()
        elif a.action == "notes":
            if not a.version:
                ap.error("notes needs the version: scripts/release.py notes X.Y.Z")
            print(
                notes(
                    a.version,
                    changes_since(a.version),
                    CHANGELOG.read_text(encoding="utf-8"),
                    a.repository,
                )
            )
        elif a.version:
            ap.error("give one version or bump, not two")
        else:
            bump(a.action, dry_run=a.dry_run, on=a.date or date.today())  # noqa: DTZ011
    except ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
