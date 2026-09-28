from __future__ import annotations

import re
from pathlib import Path

import pytest


_WORKFLOWS = (
    Path(".github/workflows/ci.yml"),
    Path(".github/workflows/windows-build.yml"),
    Path(".github/workflows/endurance.yml"),
)


def _concurrency_group_expression(workflow_text: str) -> str:
    """Return the top-level concurrency group expression without parsing all YAML."""

    match = re.search(
        r"(?ms)^concurrency:\s*\n(?P<body>(?:^[ \t]+[^\n]*\n?)+)",
        workflow_text,
    )
    assert match is not None, "qualification workflow must declare top-level concurrency"
    body = match.group("body")
    group = re.search(r"(?m)^[ \t]+group:\s*(?P<value>.+?)\s*$", body)
    assert group is not None, "qualification workflow concurrency must declare group"
    return group.group("value")


@pytest.mark.parametrize("workflow_path", _WORKFLOWS)
def test_stale_rerun_cannot_share_symmetric_pr_group_with_current_head(
    workflow_path: Path,
) -> None:
    """Server-side coalescing must preserve stale-head and lifecycle isolation.

    GitHub evaluates workflow concurrency before job-level live admission. Fresh
    qualification may therefore coalesce only when the exact event head SHA matches;
    a stale rerun must retain that exact-head identity too, and draft/closed lifecycle
    work must use a separate lane. The read-only admission job remains the final live
    head/state authority before heavy work can allocate.
    """

    text = workflow_path.read_text(encoding="utf-8")
    group = _concurrency_group_expression(text)

    assert "cancel-in-progress: true" in text
    assert "github.event.pull_request.number" in group
    assert "format('qualify-{0}', github.event.pull_request.head.sha)" in group
    assert "github.run_attempt != 1" in group
    assert "format('rerun-{0}', github.event.pull_request.head.sha)" in group
    assert "github.event.action == 'converted_to_draft'" in group
    assert "github.event.action == 'closed'" in group
    assert "'lifecycle'" in group
    assert "github.run_id" not in group
