#!/usr/bin/env python3
"""Fail-closed, offline exact-SHA GitHub Actions qualification for Autosport.

Inputs are GitHub REST run/job JSON; this tool cannot authenticate their
origin. The jobs API omits per-job run_attempt in its documented response.
For reruns, provide jobs_response.run_attempt from the attempt-scoped jobs URL
(/runs/{id}/attempts/{N}/jobs), or a matching per-job run_attempt field. A successful job is insufficient: mandatory steps
must actually have executed and succeeded. Does not establish NVDA acceptance.
"""
from __future__ import annotations

import argparse
import json
import math
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
    # GitHub REST's documented workflow-job schema has run_id/head_sha but
    # no run_attempt. For attempt 1, absence is unambiguous. On a rerun,
    # require explicit attempt provenance from the attempt-scoped jobs API;
    # otherwise stale jobs from an earlier attempt could be accepted.
    response_attempt = jobs_response.get("run_attempt")
    if response_attempt is not None:
        if type(response_attempt) is not int or response_attempt != attempt:
            raise GateError(f"run {run_id}: jobs response attempt mismatch")
    # GitHub's REST list-jobs response includes total_count. A truncated
    # page containing exactly the expected jobs is NOT proof that no extra
    # (possibly failed) job exists. Reject missing/inconsistent pagination
    # metadata rather than treating the visible page as the entire run.
    jobs = jobs_response.get("jobs")
    total_count = jobs_response.get("total_count")
    if type(jobs) is not list or type(total_count) is not int or (
        total_count != len(jobs) or total_count != len(required_jobs)
    ):
        raise GateError(f"run {run_id}: missing/extra jobs or incomplete jobs page")
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
        # JSON 100.0 compares equal to integer 100 in Python, and True == 1.
        # GitHub REST run_id must be an integer, not merely numerically equal.
        if type(job.get("run_id")) is not int or job["run_id"] != run_id:
            raise GateError(f"run {run_id}: job run id mismatch")
        job_attempt = job.get("run_attempt")
        if job_attempt is not None and (
            type(job_attempt) is not int or job_attempt != attempt
        ):
            raise GateError(f"run {run_id}: job attempt mismatch")
        if attempt > 1 and response_attempt is None and job_attempt is None:
            raise GateError(f"run {run_id}: rerun job attempt not proven")
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


def _unique_json_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous evidence, including duplicate keys in nested objects."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GateError("JSON contains duplicate object member")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise GateError("JSON contains non-finite number")


def _finite_json_float(value: str) -> float:
    # parse_constant rejects literal NaN/Infinity, but Python's default
    # float parser also converts valid JSON exponent overflow (1e999) to inf.
    number = float(value)
    if not math.isfinite(number):
        raise GateError("JSON contains non-finite number")
    return number


def _load(path: str) -> Any:
    # Limit bytes before JSON parsing, not after an unbounded read. Duplicate
    # JSON members can otherwise hide failed runs/jobs/steps behind success.
    with Path(path).open("rb") as stream:
        data = stream.read(2_000_001)
    if len(data) > 2_000_000:
        raise GateError("JSON exceeds 2 MB")
    return json.loads(data, object_pairs_hook=_unique_json_members,
                      parse_constant=_reject_nonfinite_json,
                      parse_float=_finite_json_float)


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
