from __future__ import annotations

import re
from pathlib import Path

import pytest


_WORKFLOWS = (
    Path(".github/workflows/ci.yml"),
    Path(".github/workflows/windows-build.yml"),
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
    """Every PR workflow run must reach admission before cancellation can occur.

    GitHub evaluates workflow concurrency before job-level admission checks. Head or
    run-attempt identity is not enough: two distinct fresh lifecycle events for the
    same PR/head both have attempt 1, while a stale rerun can still collide with a
    useful run under a head-derived group. A per-run scheduler key is the only local
    proof that no PR event can pre-cancel another before live-head admission runs.
    """

    text = workflow_path.read_text(encoding="utf-8")
    group = _concurrency_group_expression(text)

    assert "cancel-in-progress: true" in text
    assert "github.event.pull_request.number" in group or "github.ref" in group
    assert "github.run_id" in group, (
        f"{workflow_path} does not isolate each PR workflow run before admission: "
        f"{group!r}; distinct PR lifecycle events could cancel useful qualification"
    )
