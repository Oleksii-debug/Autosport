#!/usr/bin/env python3
"""Fail-closed, offline exact-SHA GitHub Actions qualification for Autosport.

Inputs are independently fetched GitHub REST run/job JSON; this tool cannot
authenticate their origin. A successful job is insufficient: mandatory steps
must actually have executed and succeeded. Does not establish NVDA acceptance.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_CI = frozenset({"test (ubuntu-latest, 3.11)", "test (ubuntu-latest, 3.12)",
                 "test (windows-latest, 3.11)", "test (windows-latest, 3.12)",
                 "superseded_run_admission"})
_WINDOWS = frozenset({"build", "superseded_run_admission"})
# These are the actual required steps in main's ci.yml/windows-build.yml at
# the 2026-10-24 review. Update together with workflow and regression fixtures.
_CI_REQUIRED_STEPS = frozenset({
    "Verify exact pristine source checkout before CI",
    "Run python -m pip install -e '.[test]'",
    "Run python -m pytest -v tests",
    "Run python -m autosport demo",
})
_WINDOWS_REQUIRED_STEPS = frozenset({
    "Verify exact pristine source checkout before build",
    "Run ./scripts/build_windows_candidate.ps1",
    "Materialize verified independent package extraction",
    "Qualify fixture-only certificate-backed release/update signature",
    "Execute packaged NVDA evidence contract",
    "Prove packaged workspace recovery reachability",
    "Execute packaged walk-forward evaluation",
    "Execute packaged retrospective forecast-origin evaluation",
    "Execute packaged evidence export/verify contract",
    "Audit packaged research demo path",
    "External UIA fresh-extraction gate",
    "Verify independent stage-neutral release extraction",
})
_ADMISSION_STEPS = frozenset({
    "Checkout exact PR head for read-only admission helper",
})
_NON_PR_ADMISSION = "Admit non-PR events without PR checkout"
_CI_ADMISSION = "Reject stale PR head before heavy matrix allocation"
_WINDOWS_ADMISSION = "Reject stale PR rerun before Windows runner allocation"


class GateError(ValueError):
    """Evidence is insufficient for an exact-SHA qualification."""


def _object(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise GateError(f"{label}: expected object")
    return value


def _positive_id(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise GateError(f"{label}: invalid positive integer")
    return value


def _required_steps(job: dict[str, Any], required: frozenset[str], label: str) -> None:
    steps = job.get("steps")
    if type(steps) is not list or not steps:
        raise GateError(f"{label}: missing step evidence")
    seen: set[str] = set()
    for step in steps:
        step = _object(step, f"{label} step")
        name = step.get("name")
        if type(name) is not str or not name:
            raise GateError(f"{label}: invalid step name")
        if name in seen and name in required:
            raise GateError(f"{label}: duplicate required step {name}")
        seen.add(name)
        if name in required and (step.get("status") != "completed" or
                                 step.get("conclusion") != "success"):
            raise GateError(f"{label}: required step not successful: {name}")
    missing = required - seen
    if missing:
        raise GateError(f"{label}: missing required steps: {', '.join(sorted(missing))}")


def _admission_steps(job: dict[str, Any], event: str, workflow: str) -> None:
    """Require the actual admission path selected by the workflow event.

    The CI and Windows workflows skip PR-only admission for push/dispatch.
    Treating those skipped steps as mandatory would reject a valid main build.
    """
    pr_only = _ADMISSION_STEPS | frozenset({
        _CI_ADMISSION if workflow == "CI" else _WINDOWS_ADMISSION})
    if event == "pull_request":
        required = pr_only
        forbidden = frozenset({_NON_PR_ADMISSION})
    else:
        required = frozenset({_NON_PR_ADMISSION})
        forbidden = pr_only
    _required_steps(job, required, "superseded_run_admission")
    for step in job["steps"]:
        if step["name"] in forbidden and step.get("conclusion") != "skipped":
            raise GateError("superseded_run_admission: wrong event admission path")


def check_workflow(run: Any, jobs_response: Any, *, expected_sha: str,
                   expected_name: str, required_jobs: frozenset[str]) -> dict[str, Any]:
    if type(expected_sha) is not str or not _SHA.fullmatch(expected_sha):
        raise GateError("invalid expected SHA")
    run = _object(run, "run")
    jobs_response = _object(jobs_response, "jobs response")
    run_id = _positive_id(run.get("id"), "run id")
    attempt = _positive_id(run.get("run_attempt"), "run attempt")
    if run.get("name") != expected_name or run.get("head_sha") != expected_sha:
        raise GateError(f"run {run_id}: workflow name/SHA mismatch")
    event = run.get("event")
    allowed_events = ("pull_request", "push") if expected_name == "CI" else (
        "pull_request", "push", "workflow_dispatch")
    if event not in allowed_events:
        raise GateError(f"run {run_id}: unsupported workflow event")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise GateError(f"run {run_id}: workflow not successful")
    jobs = jobs_response.get("jobs")
    if type(jobs) is not list or len(jobs) != len(required_jobs):
        raise GateError(f"run {run_id}: missing/extra jobs")
    seen: set[str] = set()
    ids: set[int] = set()
    for index, raw in enumerate(jobs):
        job = _object(raw, f"run {run_id} job {index}")
        name = job.get("name")
        job_id = _positive_id(job.get("id"), "job id")
        if type(name) is not str or name not in required_jobs or name in seen or job_id in ids:
            raise GateError(f"run {run_id}: duplicate or unknown job")
        seen.add(name)
        ids.add(job_id)
        if job.get("run_id") != run_id or job.get("run_attempt") != attempt:
            raise GateError(f"run {run_id}: job run/attempt mismatch")
        if job.get("head_sha") != expected_sha:
            raise GateError(f"run {run_id}: job SHA mismatch")
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            raise GateError(f"run {run_id}: job not successful: {name}")
        if name == "superseded_run_admission":
            _admission_steps(job, event, expected_name)
        elif name == "build":
            _required_steps(job, _WINDOWS_REQUIRED_STEPS, name)
        else:
            _required_steps(job, _CI_REQUIRED_STEPS, name)
    if seen != required_jobs:
        raise GateError(f"run {run_id}: incomplete matrix")
    return {"run_id": run_id, "run_attempt": attempt, "sha": expected_sha,
            "workflow": expected_name, "executed_successful_jobs": sorted(seen)}


def qualify(ci_run: Any, ci_jobs: Any, windows_run: Any, windows_jobs: Any,
            expected_sha: str) -> dict[str, Any]:
    ci = check_workflow(ci_run, ci_jobs, expected_sha=expected_sha,
                        expected_name="CI", required_jobs=_CI)
    windows = check_workflow(windows_run, windows_jobs, expected_sha=expected_sha,
                             expected_name="Windows candidate", required_jobs=_WINDOWS)
    if ci["run_id"] == windows["run_id"]:
        raise GateError("CI and Windows must be different runs")
    return {"status": "PASS", "sha": expected_sha, "ci": ci, "windows": windows,
            "limitations": ["Input JSON authenticity is not verified",
                            "Workflow step names are pinned to 2026-10-24 source",
                            "Not independent review, postmerge readback, or human NVDA verification"]}


def _load(path: str) -> Any:
    data = Path(path).read_bytes()
    if len(data) > 2_000_000:
        raise GateError("JSON exceeds 2 MB")
    return json.loads(data)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sha", required=True)
    for arg in ("ci-run", "ci-jobs", "windows-run", "windows-jobs"):
        p.add_argument("--" + arg, required=True)
    a = p.parse_args(argv)
    try:
        result = qualify(_load(a.ci_run), _load(a.ci_jobs),
                         _load(a.windows_run), _load(a.windows_jobs), a.sha)
    except (GateError, OSError, ValueError, UnicodeError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
