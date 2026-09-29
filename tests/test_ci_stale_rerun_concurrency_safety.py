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
    work must use a separate lane. The top-level event number keeps lifecycle identity
    stable even when a merged closed event has an empty pull_request object. Non-PR
    runs must stay run-unique so same-ref push/dispatch work cannot pre-cancel.
    """

    text = workflow_path.read_text(encoding="utf-8")
    group = _concurrency_group_expression(text)

    assert "cancel-in-progress: true" in text
    assert "github.event.number" in group
    assert "github.event.pull_request.number" not in group
    assert "format('qualify-{0}', github.event.pull_request.head.sha)" in group
    assert "github.run_attempt != 1" in group
    assert "format('rerun-{0}', github.event.pull_request.head.sha)" in group
    assert "github.event.action == 'converted_to_draft'" in group
    assert "github.event.action == 'closed'" in group
    assert "'lifecycle'" in group

    # Non-PR events have no PR/head lifecycle identity. They must be unique per run;
    # using github.ref here lets a newer same-ref push or workflow_dispatch suppress
    # an older run before any admission or product qualification actually executes.
    assert "|| format('run-{0}', github.run_id)" in group
    assert "github.ref" not in group

    # Lifecycle must win over rerun. Otherwise a rerun of a merged closed event can
    # evaluate a missing pull_request.head.sha instead of the stable lifecycle key.
    assert group.index("github.event.action == 'converted_to_draft'") < group.index(
        "github.run_attempt != 1"
    )
