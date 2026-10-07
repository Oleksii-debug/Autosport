from __future__ import annotations

import json
from urllib.error import HTTPError

import pytest

import scripts.cancel_superseded_pr_workflow_runs as controller_module
from scripts.cancel_superseded_pr_workflow_runs import (
    CancellationError,
    GitHubApi,
    _AllowedHttpError,
)
from scripts.cancel_superseded_pr_workflow_runs_scoped import WorkflowScopedGitHubApi


CURRENT_HEAD = "a" * 40
STALE_HEAD = "b" * 40
OTHER_STALE_HEAD = "c" * 40


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


def _run(
    run_id: int,
    *,
    head_sha: str,
    pull_requests: list[dict[str, object]],
    head_branch: str = "feature/example",
) -> dict[str, object]:
    return {
        "id": run_id,
        "head_sha": head_sha,
        "head_branch": head_branch,
        "head_repository": {"full_name": "Oleksii-debug/Autosport"},
        "name": "CI",
        "status": "queued",
        "pull_requests": pull_requests,
    }



class _FakeSuccessResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        return False

    def read(self) -> bytes:
        return self._body


def _sealed_api(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[object],
) -> WorkflowScopedGitHubApi:
    api = WorkflowScopedGitHubApi(
        repository="Oleksii-debug/Autosport",
        token="test-token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    queue = list(responses)
    observed_runs: dict[int, dict[str, object]] = {}
    api.paths = []
    api.cancelled = []
    prefix = "https://api.github.com/repos/Oleksii-debug/Autosport"

    def fake_urlopen(request, *, timeout: int):
        del timeout
        url = request.full_url
        method = request.get_method()
        assert url.startswith(prefix)
        path = url[len(prefix) :]
        if method == "POST":
            parts = path.split("/")
            assert parts[:3] == ["", "actions", "runs"]
            assert parts[-1] == "cancel"
            api.cancelled.append(int(parts[3]))
            return _FakeSuccessResponse(202, b'{"message":"accepted"}')
        assert method == "GET"
        api.paths.append(path)
        if path.startswith("/actions/runs/"):
            run_id = int(path.split("/")[3])
            observed = observed_runs.get(run_id)
            if observed is None:
                raise AssertionError("exact-run reread has no scan observation")
            payload = dict(observed)
            payload["workflow_id"] = 356678400
            payload["event"] = "pull_request"
            return _FakeSuccessResponse(
                200,
                json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            )
        if not queue:
            raise AssertionError("unexpected API request")
        response = queue.pop(0)
        if (
            path.startswith("/actions/workflows/")
            and isinstance(response, dict)
            and isinstance(response.get("workflow_runs"), list)
        ):
            for observed in response["workflow_runs"]:
                if isinstance(observed, dict) and type(observed.get("id")) is int:
                    observed_runs[observed["id"]] = dict(observed)
        if isinstance(response, _AllowedHttpError):
            raise HTTPError(
                url,
                response.status_code,
                "fake HTTP error",
                hdrs=None,
                fp=None,
            )
        return _FakeSuccessResponse(
            200,
            json.dumps(response, separators=(",", ":")).encode("utf-8"),
        )

    monkeypatch.setattr(controller_module, "urlopen", fake_urlopen)
    return api


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
        assert allowed_http_errors in (frozenset(), frozenset({404}))
        self.paths.append(path)
        if not self.responses:
            raise AssertionError("unexpected API request")
        response = self.responses.pop(0)
        if isinstance(response, _AllowedHttpError):
            assert response.status_code in allowed_http_errors
        return response


def test_unbound_stale_run_is_cancelled_only_after_unique_association_and_boundary_rechecks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _sealed_api(monkeypatch, 
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


    assert api.cancel_historical_unbound_runs() == (91,)
    assert api.cancelled == [91]
    assert api.paths == [
        "/actions/workflows/356678400/runs?event=pull_request&status=queued&per_page=100&page=1",
        f"/commits/{STALE_HEAD}/pulls?per_page=100&page=1",
        "/pulls/77",
        "/actions/runs/91",
        f"/commits/{STALE_HEAD}/pulls?per_page=100&page=1",
        "/pulls/77",
    ]


def test_unbound_same_head_ready_run_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _sealed_api(
        monkeypatch,
        [
            {
                "total_count": 1,
                "workflow_runs": [_run(92, head_sha=CURRENT_HEAD, pull_requests=[])],
            },
            [_associated_pr(77, head_sha=CURRENT_HEAD)],
            _live_pr(CURRENT_HEAD),
        ],
    )
    api._active_runs_for_status("queued")

    assert api.cancel_historical_unbound_runs() == ()
    assert api.paths[-2:] == [
        f"/commits/{CURRENT_HEAD}/pulls?per_page=100&page=1",
        "/pulls/77",
    ]


def test_unbound_cleanup_preserves_ambiguous_association_without_branch_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _sealed_api(
        monkeypatch,
        [
            {
                "total_count": 1,
                "workflow_runs": [_run(93, head_sha=STALE_HEAD, pull_requests=[])],
            },
            [
                _associated_pr(77, head_sha=CURRENT_HEAD),
                _associated_pr(88, head_sha=CURRENT_HEAD),
            ],
        ],
    )
    api._active_runs_for_status("queued")

    assert api.cancel_historical_unbound_runs() == ()
    assert api.paths[-1] == f"/commits/{STALE_HEAD}/pulls?per_page=100&page=1"


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


def test_zero_association_run_is_cancelled_when_canonical_branch_has_advanced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _sealed_api(monkeypatch, 
        [
            {
                "total_count": 1,
                "workflow_runs": [
                    _run(
                        95,
                        head_sha=STALE_HEAD,
                        head_branch="fix/stale",
                        pull_requests=[],
                    )
                ],
            },
            [],
            [],
            {
                "object": {"sha": CURRENT_HEAD},
            },
            [],
            {
                "object": {"sha": CURRENT_HEAD},
            },
        ]
    )
    api._active_runs_for_status("queued")


    assert api.cancel_historical_unbound_runs() == (95,)
    assert api.cancelled == [95]


def test_zero_association_run_is_cancelled_when_canonical_branch_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _sealed_api(monkeypatch, 
        [
            {
                "total_count": 1,
                "workflow_runs": [
                    _run(
                        98,
                        head_sha=STALE_HEAD,
                        head_branch="fix/deleted",
                        pull_requests=[],
                    )
                ],
            },
            [],
            [],
            _AllowedHttpError(status_code=404),
            [],
            _AllowedHttpError(status_code=404),
        ]
    )
    api._active_runs_for_status("queued")


    assert api.cancel_historical_unbound_runs() == (98,)
    assert api.cancelled == [98]


def test_zero_association_branch_reappearance_at_boundary_preserves_candidate_without_aborting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _sealed_api(monkeypatch, 
        [
            {
                "total_count": 1,
                "workflow_runs": [
                    _run(
                        99,
                        head_sha=STALE_HEAD,
                        head_branch="fix/race",
                        pull_requests=[],
                    )
                ],
            },
            [],
            [],
            {"object": {"sha": CURRENT_HEAD}},
            [],
            {"object": {"sha": STALE_HEAD}},
        ]
    )
    api._active_runs_for_status("queued")


    assert api.cancel_historical_unbound_runs() == ()
    assert api.cancelled == []


def test_unique_association_boundary_race_skips_only_changed_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _sealed_api(monkeypatch, 
        [
            {
                "total_count": 2,
                "workflow_runs": [
                    _run(101, head_sha=STALE_HEAD, pull_requests=[]),
                    _run(102, head_sha=OTHER_STALE_HEAD, pull_requests=[]),
                ],
            },
            [_associated_pr(77, head_sha=CURRENT_HEAD)],
            _live_pr(CURRENT_HEAD),
            [],
            [_associated_pr(88, head_sha=CURRENT_HEAD)],
            _live_pr(CURRENT_HEAD),
            [_associated_pr(88, head_sha=CURRENT_HEAD)],
            _live_pr(CURRENT_HEAD),
        ]
    )
    api._active_runs_for_status("queued")


    assert api.cancel_historical_unbound_runs() == (102,)
    assert api.cancelled == [102]


def test_zero_association_boundary_race_does_not_block_later_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _sealed_api(monkeypatch, 
        [
            {
                "total_count": 2,
                "workflow_runs": [
                    _run(
                        103,
                        head_sha=STALE_HEAD,
                        head_branch="fix/race-a",
                        pull_requests=[],
                    ),
                    _run(104, head_sha=OTHER_STALE_HEAD, pull_requests=[]),
                ],
            },
            [],
            [],
            {"object": {"sha": CURRENT_HEAD}},
            [],
            {"object": {"sha": STALE_HEAD}},
            [_associated_pr(88, head_sha=CURRENT_HEAD)],
            _live_pr(CURRENT_HEAD),
            [_associated_pr(88, head_sha=CURRENT_HEAD)],
            _live_pr(CURRENT_HEAD),
        ]
    )
    api._active_runs_for_status("queued")


    assert api.cancel_historical_unbound_runs() == (104,)
    assert api.cancelled == [104]


def test_zero_association_same_head_branch_is_preserved() -> None:
    api = FakeApi(
        [
            {
                "total_count": 1,
                "workflow_runs": [
                    _run(
                        96,
                        head_sha=CURRENT_HEAD,
                        head_branch="fix/current",
                        pull_requests=[],
                    )
                ],
            },
            [],
            [],
            {
                "object": {"sha": CURRENT_HEAD},
            },
        ]
    )
    api._active_runs_for_status("queued")

    assert api.cancel_historical_unbound_runs() == ()


def test_zero_association_foreign_repository_run_never_gets_branch_fallback() -> None:
    run = _run(
        97,
        head_sha=STALE_HEAD,
        head_branch="fix/foreign",
        pull_requests=[],
    )
    run["head_repository"] = {"full_name": "someone/fork"}
    api = FakeApi(
        [
            {
                "total_count": 1,
                "workflow_runs": [run],
            },
            [],
        ]
    )
    api._active_runs_for_status("queued")

    assert api.cancel_historical_unbound_runs() == ()
