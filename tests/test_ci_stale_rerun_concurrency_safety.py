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
    """A later obsolete rerun must never cancel current exact-head qualification.

    GitHub evaluates workflow concurrency before job-level admission checks. Therefore a
    single last-writer-wins PR/ref group with ``cancel-in-progress: true`` is unsafe:
    manually rerunning an obsolete head after a newer head starts can evict the newer
    exact-head qualification. Reruns must retain an attempt/head discriminator (or an
    equivalent head discriminator) while stale-work cleanup is performed asymmetrically.
    """

    text = workflow_path.read_text(encoding="utf-8")
    group = _concurrency_group_expression(text)

    assert "cancel-in-progress: true" in text
    assert "github.event.pull_request.number" in group or "github.ref" in group

    has_stale_rerun_isolation = any(
        token in group
        for token in (
            "github.run_attempt",
            "github.sha",
            "github.event.pull_request.head.sha",
        )
    )
    assert has_stale_rerun_isolation, (
        f"{workflow_path} uses a symmetric PR/ref concurrency group: {group!r}; "
        "a later rerun of an obsolete head could cancel the current exact-head run"
    )
