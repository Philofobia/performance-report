"""Offline contract tests between .github/workflows/ci.yml and the Python it
drives.

The workflow hardcodes a targets path and a literal exit code in shell text;
nothing else fails if either drifts from ``ingest/automated.py``. These tests
parse the committed workflow (PyYAML — already a pinned dependency) rather
than hardcoding the same values a second time, so a drift shows up here
instead of silently in a CI run against a real network target.

Kept separate from tests/unit/config_test.py: that file is about validating
config/*.yaml through config/load.py's loaders, not about the CI workflow
definition itself — a different subject with a different failure mode.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from ingest.automated import EXIT_TARGET_UNREACHABLE

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load_live_campaign_job() -> dict:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    jobs = workflow.get("jobs", {})
    assert "live-campaign-report" in jobs, (
        f"'live-campaign-report' job not found in {WORKFLOW_PATH} — "
        f"jobs present: {sorted(jobs)}"
    )
    return jobs["live-campaign-report"]


def _find_step(job: dict, name: str) -> dict:
    steps = job.get("steps", [])
    for step in steps:
        if step.get("name") == name:
            return step
    names = [s.get("name") for s in steps]
    raise AssertionError(
        f"step {name!r} not found in live-campaign-report; steps present: {names}"
    )


def test_campaign_step_targets_file_exists_on_disk():
    """The path passed to `--targets` in the workflow must be a real,
    committed file — a renamed/removed config/ci-targets.yaml would otherwise
    only fail inside a live CI run against the real network."""
    job = _load_live_campaign_job()
    step = _find_step(job, "Campaign against the live CI target")
    script = step.get("run", "")

    match = re.search(r"--targets\s+(\S+)", script)
    assert match, (
        "could not find a '--targets <path>' invocation in the "
        f"'Campaign against the live CI target' step's run script:\n{script}"
    )
    targets_path = match.group(1)
    resolved = REPO_ROOT / targets_path
    assert resolved.is_file(), (
        f"the workflow's --targets path {targets_path!r} "
        f"(resolved to {resolved}) does not exist on disk"
    )


def test_campaign_step_checks_the_same_exit_code_as_automated_py():
    """The shell script's `-eq 3` must track ingest.automated.EXIT_TARGET_UNREACHABLE,
    not a literal that can silently drift from the Python that defines it."""
    job = _load_live_campaign_job()
    step = _find_step(job, "Campaign against the live CI target")
    script = step.get("run", "")

    expected = f"-eq {EXIT_TARGET_UNREACHABLE}"
    assert expected in script, (
        f"expected the campaign step's shell script to test for "
        f"{expected!r} (ingest.automated.EXIT_TARGET_UNREACHABLE = "
        f"{EXIT_TARGET_UNREACHABLE}), but it was not found in:\n{script}"
    )


def test_live_campaign_report_job_needs_security_and_tests():
    """No browser minutes should be spent while the offline suite is red."""
    job = _load_live_campaign_job()
    assert job.get("needs") == "security-and-tests", (
        "expected live-campaign-report.needs == 'security-and-tests', got "
        f"{job.get('needs')!r} — browser minutes would be spent even when "
        "the offline suite is red"
    )
