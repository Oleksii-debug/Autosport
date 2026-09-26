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
def test_pr_head_preflight_is_read_only_and_blocks_stale_heavy_work(
    workflow_path: Path,
    heavy_job: str,
) -> None:
    """PR-controlled admission must never receive Actions write authority."""

    text = workflow_path.read_text(encoding="utf-8")

    assert re.search(r"(?m)^  pull-requests:\s*read\s*$", text)
    assert not re.search(r"(?m)^  actions:\s*write\s*$", text)

    assert re.search(r"(?m)^  superseded_run_admission:\s*$", text)
    assert "scripts/cancel_superseded_pr_workflow_runs.py" in text
    assert "--admission-only" in text

    concurrency = re.search(
        r"(?ms)^concurrency:\s*\n(?P<body>(?:^[ \t]+[^\n]*\n?)+)",
        text,
    )
    assert concurrency is not None
    group = concurrency.group("body")
    assert "cancel-in-progress: true" in group
    assert "github.run_id" in group, (
        "workflow must isolate every PR run before job-level live-head admission"
    )

    heavy = _job_body(text, heavy_job)
    assert re.search(
        r"(?m)^    needs:\s*superseded_run_admission\s*$",
        heavy,
    ), f"{heavy_job} must wait for current-head admission"
    assert "needs.superseded_run_admission.outputs.current_head == 'true'" in heavy


@pytest.mark.parametrize(("workflow_path", "_heavy_job"), _WORKFLOWS)
def test_closed_pr_lifecycle_never_requires_head_checkout(
    workflow_path: Path,
    _heavy_job: str,
) -> None:
    """Closing/deleting a source branch must not turn harmless cleanup into red CI."""

    text = workflow_path.read_text(encoding="utf-8")
    assert "github.event_name != 'pull_request' || github.event.action == 'closed'" in text
    assert "github.event_name == 'pull_request' && github.event.action != 'closed'" in text
