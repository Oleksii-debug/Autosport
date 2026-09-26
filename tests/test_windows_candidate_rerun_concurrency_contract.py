from pathlib import Path
import re


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "windows-build.yml"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_windows_candidate_concurrency_does_not_partition_by_run_attempt() -> None:
    """A newer exact-head event must be able to cancel a superseded rerun for the same PR."""
    text = _workflow_text()
    match = re.search(
        r"(?ms)^concurrency:\s*\n(?P<body>(?:^[ \t]+.*\n?)+)",
        text,
    )
    assert match is not None, "Windows Candidate workflow must define top-level concurrency"

    body = match.group("body")
    assert "cancel-in-progress: true" in body
    assert "github.run_attempt" not in body
    assert "github.event.pull_request.number" in body
    assert "github.ref" in body


def test_windows_candidate_concurrency_group_is_not_head_specific() -> None:
    """Head-specific groups strand old-head reruns instead of cancelling them on synchronize."""
    text = _workflow_text()
    match = re.search(
        r"(?ms)^concurrency:\s*\n(?P<body>(?:^[ \t]+.*\n?)+)",
        text,
    )
    assert match is not None

    body = match.group("body")
    assert "pull_request.head.sha" not in body
    assert "github.sha" not in body
