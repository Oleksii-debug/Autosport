from __future__ import annotations

import pytest

from scripts.cancel_superseded_pr_workflow_runs import GitHubApi
from scripts.cancel_superseded_pr_workflow_runs_scoped import WorkflowScopedGitHubApi


CURRENT_HEAD = "a" * 40
STALE_HEAD = "b" * 40


def _associated_pr(number: int, *, head_sha: str) -> dict[str, object]:
    return {"number": number, "head": {"sha": head_sha}}


def _live_pr(head_sha: str, *, state: str = "open", draft: bool = False) -> dict[str, object]:
    return {
        "head": {
            "sha": head_sha,
            "repo": {"full_name": "Oleksii-debug/Autosport"},
        },
        "base": {"repo": {"full_name": "Oleksii-debug/Autosport"}},
        "state": state,
        "draft": draft,
    }


def _run(run_id: int, *, head_sha: str, pull_requests: list[dict[str, object]]) -> dict[str, object]:
    return {
        "id": run_id,
        "head_sha": head_sha,
        "name": "CI",
        "status": "queued",
        "pull_requests": pull_requests,
    }


class FakeApi(WorkflowScopedGitHubApi):
    def __init__(self, responses: list[object]) -> None:
        super().__init__(
            repository="Oleksii-debug/Autosport",
            token="test-token",
            workflow_id=356678400,
            workflow_name="CI",
        )
        self.responses = list(responses)
        self.paths: list[str] = []

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ) -> object:
        assert method == "GET"
        assert not allowed_http_errors
        self.paths.append(path)
        if not self.responses:
            raise AssertionError("unexpected API request")
        return self.responses.pop(0)


def test_unbound_stale_run_is_cancelled_only_after_unique_association_and_boundary_rechecks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeApi(
        [
            {
                "total_count": 1,
                "workflow_runs": [_run(91, head_sha=STALE_HEAD, pull_requests=[])],
            },
            [_associated_pr(77, head_sha=CURRENT_HEAD)],
            _live_pr(CURRENT_HEAD),
            [_associated_pr(77, head_sha=CURRENT_HEAD)],
            _live_pr(CURRENT_HEAD),
        ]
    )
    api._active_runs_for_status("queued")

    cancelled: list[int] = []
    monkeypatch.setattr(
        GitHubApi,
        "cancel",
        lambda self, run_id: cancelled.append(run_id),
    )

    assert api.cancel_historical_unbound_runs() == (91,)
    assert cancelled == [91]
    assert api.paths == [
        "/actions/workflows/356678400/runs?event=pull_request&status=queued&per_page=100&page=1",
        f"/commits/{STALE_HEAD}/pulls?per_page=100&page=1",
        "/pulls/77",
        f"/commits/{STALE_HEAD}/pulls?per_page=100&page=1",
        "/pulls/77",
    ]


def test_unbound_same_head_ready_run_is_preserved() -> None:
    api = FakeApi(
        [
            {
                "total_count": 1,
                "workflow_runs": [_run(92, head_sha=CURRENT_HEAD, pull_requests=[])],
            },
            [_associated_pr(77, head_sha=CURRENT_HEAD)],
            _live_pr(CURRENT_HEAD),
        ]
    )
    api._active_runs_for_status("queued")

    assert api.cancel_historical_unbound_runs() == ()
    assert api.paths[-2:] == [
        f"/commits/{CURRENT_HEAD}/pulls?per_page=100&page=1",
        "/pulls/77",
    ]


@pytest.mark.parametrize(
    "associated",
    (
        [],
        [
            _associated_pr(77, head_sha=CURRENT_HEAD),
            _associated_pr(88, head_sha=CURRENT_HEAD),
        ],
    ),
)
def test_unbound_cleanup_fails_closed_for_missing_or_ambiguous_association(
    associated: list[dict[str, object]],
) -> None:
    api = FakeApi(
        [
            {
                "total_count": 1,
                "workflow_runs": [_run(93, head_sha=STALE_HEAD, pull_requests=[])],
            },
            associated,
        ]
    )
    api._active_runs_for_status("queued")

    assert api.cancel_historical_unbound_runs() == ()


def test_unbound_cleanup_never_recancels_normal_supersession_result() -> None:
    api = FakeApi(
        [
            {
                "total_count": 1,
                "workflow_runs": [_run(94, head_sha=STALE_HEAD, pull_requests=[])],
            }
        ]
    )
    api._active_runs_for_status("queued")

    assert api.cancel_historical_unbound_runs(exclude_run_ids=(94,)) == ()
    assert api.paths == [
        "/actions/workflows/356678400/runs?event=pull_request&status=queued&per_page=100&page=1"
    ]
