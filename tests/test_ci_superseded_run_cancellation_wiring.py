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
    """PR-controlled admission must never receive Actions write authority.

    Fresh-vs-rerun exact-head concurrency preserves the bidirectional safety property;
    the lightweight admission job then prevents a stale rerun from allocating the
    expensive CI/Windows runner. Historical runs created from pre-admission workflow
    versions require trusted operational cleanup rather than a write-capable PR token.
    """

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
    assert "github.run_attempt" in group
    assert "github.event.pull_request.head.sha" in group
    assert "cancel-in-progress: true" in group

    heavy = _job_body(text, heavy_job)
    assert re.search(
        r"(?m)^    needs:\s*superseded_run_admission\s*$",
        heavy,
    ), f"{heavy_job} must wait for current-head admission"
    assert "needs.superseded_run_admission.outputs.current_head == 'true'" in heavy
