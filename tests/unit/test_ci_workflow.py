"""A CI step that can run on Windows runs one command.

GitHub runs a Windows step's script in PowerShell, and PowerShell reports only
the exit code of the script's *last* command. The test job's step ran
``coverage run -m pytest`` and then ``coverage report``, so pytest's "4
failed" on every Windows job reached the log and never reached the job's
status. bash, which Linux steps run in, stops at the first failure with ``-e``
and hid nothing -- which is why the same step was honest on one platform only.

One command per step is right in both shells. A step that needs several sets
``shell: bash``, which GitHub runs with ``-eo pipefail`` on Windows as well.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from portable_core.lint._common import repo_root

pytestmark = pytest.mark.unit

WORKFLOWS = sorted((repo_root() / ".github" / "workflows").glob("*.y*ml"))


def _windows_capable(job: dict[str, Any]) -> bool:
    """A job whose runner, or whose matrix of runners, includes Windows."""
    return "windows" in json.dumps([job.get("runs-on"), job.get("strategy")]).lower()


def _shell(container: dict[str, Any]) -> str | None:
    shell = (container.get("defaults") or {}).get("run", {}).get("shell")
    return shell if isinstance(shell, str) else None


def _commands(script: str) -> list[str]:
    return [
        line
        for line in (raw.strip() for raw in script.splitlines())
        if line and not line.startswith("#")
    ]


def _offenders(workflow: dict[str, Any]) -> list[str]:
    """Every Windows-capable step whose status would be its last command's."""
    found: list[str] = []
    for job_id, job in (workflow.get("jobs") or {}).items():
        if not _windows_capable(job):
            continue
        default = _shell(job) or _shell(workflow)
        for step in job.get("steps") or []:
            if "run" not in step or step.get("shell", default) == "bash":
                continue
            if len(_commands(str(step["run"]))) > 1:
                found.append(f"{job_id} / {step.get('name', step['run'].splitlines()[0])}")
    return found


def test_there_is_a_workflow_with_a_windows_job_to_check() -> None:
    """The test below passes vacuously if it finds nothing to look at."""
    jobs = [
        job
        for path in WORKFLOWS
        for job in (yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"] or {}).values()
    ]
    assert any(_windows_capable(job) for job in jobs)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_windows_step_runs_one_command(path: Any) -> None:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert _offenders(workflow) == [], (
        "On Windows these steps report only their last command's exit code. "
        "Split each into one step per command, or give it `shell: bash`."
    )


def test_the_step_that_hid_the_failures_is_caught() -> None:
    """The shape that went green with failing tests, so the rule is known to bite."""
    hid = yaml.safe_load(
        """
        jobs:
          test:
            runs-on: ${{ matrix.os }}
            strategy: {matrix: {os: [ubuntu-latest, windows-latest]}}
            steps:
              - name: pytest with coverage
                run: |
                  coverage run -m pytest
                  coverage report
              - name: under bash it would have stopped
                shell: bash
                run: |
                  coverage run -m pytest
                  coverage report
          linux-only:
            runs-on: ubuntu-latest
            steps:
              - run: |
                  pytest
                  coverage report
        """
    )
    assert _offenders(hid) == ["test / pytest with coverage"]
