from __future__ import annotations

import pytest

from scripts.cancel_superseded_pr_workflow_runs import CancellationError
from scripts.cancel_superseded_pr_workflow_runs_scoped import WorkflowScopedGitHubApi


HEAD = "a" * 40


class FakeScopedApi(WorkflowScopedGitHubApi):
    def __init__(self, workflow_id: int, responses: list[object]) -> None:
        super().__init__(
            repository="Oleksii-debug/Autosport",
            token="test-token",
            workflow_id=workflow_id,
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


def _run(run_id: int, *, status: str = "queued") -> dict[str, object]:
    return {
        "id": run_id,
        "head_sha": HEAD,
        "name": "CI",
        "status": status,
        "pull_requests": [{"number": 2022}],
    }


def test_active_status_scan_is_scoped_to_exact_source_workflow_id() -> None:
    api = FakeScopedApi(
        356678400,
        [{"total_count": 1, "workflow_runs": [_run(91)]}],
    )

    runs = api._active_runs_for_status("queued")

    assert tuple(item.run_id for item in runs) == (91,)
    assert api.paths == [
        "/actions/workflows/356678400/runs?event=pull_request&status=queued&per_page=100&page=1"
    ]
    assert all(path != "/actions/runs" for path in api.paths)


def test_shrinking_active_collection_ends_on_short_page_without_failing() -> None:
    api = FakeScopedApi(
        356678400,
        [{"total_count": 200, "workflow_runs": [_run(92)]}],
    )

    runs = api._active_runs_for_status("queued")

    assert tuple(item.run_id for item in runs) == (92,)
    assert len(api.paths) == 1


def test_workflow_scope_is_positive_exact_integer() -> None:
    with pytest.raises(CancellationError, match="invalid workflow id"):
        FakeScopedApi(0, [])
    with pytest.raises(CancellationError, match="invalid workflow id"):
        FakeScopedApi(True, [])


def test_scoped_scan_keeps_strict_active_status_validation() -> None:
    api = FakeScopedApi(356678400, [])

    with pytest.raises(CancellationError, match="invalid active workflow status"):
        api._active_runs_for_status("completed")

    assert api.paths == []
