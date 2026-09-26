from __future__ import annotations

import re
from pathlib import Path

import pytest


_WORKFLOWS = (
    (Path(".github/workflows/ci.yml"), "test"),
    (Path(".github/workflows/windows-build.yml"), "build"),
)


def _job_body(text: str, job_name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(job_name)}:\s*\n(?P<body>(?:^    [^\n]*\n|^      [^\n]*\n|^        [^\n]*\n|^          [^\n]*\n|^            [^\n]*\n|^              [^\n]*\n|^                [^\n]*\n)*)",
        text,
    )
    assert match is not None, f"workflow must contain job {job_name!r}"
    return match.group("body")


@pytest.mark.parametrize(("workflow_path", "heavy_job"), _WORKFLOWS)
def test_current_head_preflight_cancels_superseded_runs_before_heavy_runner(
    workflow_path: Path,
    heavy_job: str,
) -> None:
    """Current head may evict stale work; stale reruns may never evict current head."""

    text = workflow_path.read_text(encoding="utf-8")

    # The helper reads the live PR head and cancels Actions runs through the REST API.
    # Those permissions are explicit product/CI authority and must not be accidental.
    assert re.search(r"(?m)^  pull-requests:\s*read\s*$", text)
    assert re.search(r"(?m)^  actions:\s*write\s*$", text)

    assert re.search(r"(?m)^  superseded_run_admission:\s*$", text)
    assert "scripts/cancel_superseded_pr_workflow_runs.py" in text

    # Admission-only prevents stale work from starting but cannot free already-active
    # obsolete runs. The current exact head must invoke the bounded asymmetric
    # cancellation path; the helper itself rechecks the live head before cancellation.
    assert "--admission-only" not in text

    heavy = _job_body(text, heavy_job)
    assert re.search(
        r"(?m)^    needs:\s*superseded_run_admission\s*$",
        heavy,
    ), f"{heavy_job} must wait for current-head admission/cancellation"
    assert "needs.superseded_run_admission.outputs.current_head == 'true'" in heavy
