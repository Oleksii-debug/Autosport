from __future__ import annotations

import pytest

from scripts.cancel_superseded_pr_workflow_runs import (
    CancellationError,
    GitHubApi,
    WorkflowRun,
    select_superseded_runs,
)


HEAD_A = "a" * 40
HEAD_B = "b" * 40


def _run(run_id: int, head_sha: str) -> WorkflowRun:
    return WorkflowRun(
        run_id=run_id,
        head_sha=head_sha,
        workflow_name="CI",
        pr_numbers=(2016,),
        status="queued",
    )


def test_ineligible_pr_cleanup_includes_older_same_head_runs() -> None:
    runs = (
        _run(100, HEAD_A),
        _run(101, HEAD_B),
        _run(102, HEAD_B),
    )

    assert select_superseded_runs(
        runs,
        pr_number=2016,
        live_head_sha=HEAD_B,
        workflow_name="CI",
        current_run_id=102,
        cancel_same_head=True,
    ) == (100, 101)


def test_integration_capable_cleanup_preserves_older_same_head_run() -> None:
    runs = (
        _run(100, HEAD_A),
        _run(101, HEAD_B),
        _run(102, HEAD_B),
    )

    assert select_superseded_runs(
        runs,
        pr_number=2016,
        live_head_sha=HEAD_B,
        workflow_name="CI",
        current_run_id=102,
    ) == (100,)


class _StateApi(GitHubApi):
    def __init__(self, payload: object) -> None:
        super().__init__(repository="owner/repo", token="token")
        self._payload = payload

    def _request(self, path: str, **kwargs: object) -> object:
        assert path == "/pulls/2016"
        assert not kwargs
        return self._payload


@pytest.mark.parametrize(
    ("state", "draft", "expected"),
    (
        ("open", False, True),
        ("open", True, False),
        ("closed", False, False),
        ("closed", True, False),
    ),
)
def test_pr_integration_capability_is_exact_state_and_draft(
    state: str,
    draft: bool,
    expected: bool,
) -> None:
    api = _StateApi({"state": state, "draft": draft})
    assert api.pr_is_integration_capable(2016) is expected


@pytest.mark.parametrize(
    "payload",
    (
        {"state": "open"},
        {"state": "open", "draft": 0},
        {"state": "merged", "draft": False},
        [],
    ),
)
def test_invalid_pr_qualification_state_fails_closed(payload: object) -> None:
    api = _StateApi(payload)
    with pytest.raises(CancellationError):
        api.pr_is_integration_capable(2016)
