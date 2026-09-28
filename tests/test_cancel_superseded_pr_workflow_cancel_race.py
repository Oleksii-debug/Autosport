from __future__ import annotations

import pytest

from scripts.cancel_superseded_pr_workflow_runs import (
    CancellationError,
    GitHubApi,
    _AllowedHttpError,
)


def test_cancel_conflict_is_benign_after_run_completed(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    calls: list[tuple[str, str, frozenset[int]]] = []

    def fake_request(
        path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ) -> object:
        calls.append((path, method, allowed_http_errors))
        if method == "POST":
            assert allowed_http_errors == frozenset({409})
            return _AllowedHttpError(409)
        assert path == "/actions/runs/123"
        return {"status": "completed"}

    monkeypatch.setattr(api, "_request", fake_request)
    api.cancel(123)

    assert calls == [
        ("/actions/runs/123/cancel", "POST", frozenset({409})),
        ("/actions/runs/123", "GET", frozenset()),
    ]


def test_cancel_conflict_fails_closed_while_run_remains_active(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")

    def fake_request(
        path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ) -> object:
        del path, allowed_http_errors
        if method == "POST":
            return _AllowedHttpError(409)
        return {"status": "in_progress"}

    monkeypatch.setattr(api, "_request", fake_request)
    with pytest.raises(
        CancellationError,
        match="cancellation conflicted while run remains active",
    ):
        api.cancel(123)


def test_workflow_run_status_rejects_unknown_state(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    monkeypatch.setattr(
        api,
        "_request",
        lambda *_args, **_kwargs: {"status": "mystery"},
    )

    with pytest.raises(CancellationError, match="invalid workflow-run status"):
        api.workflow_run_status(123)
