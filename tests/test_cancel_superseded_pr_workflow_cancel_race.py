from __future__ import annotations

import pytest

import scripts.cancel_superseded_pr_workflow_runs as controller_module
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


class _FakeSuccessResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self) -> bytes:
        return self._body


def test_cancel_accepts_nonempty_202_success_response_body(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    calls: list[tuple[str, str, int]] = []

    def fake_urlopen(request, *, timeout: int):
        calls.append((request.full_url, request.get_method(), timeout))
        return _FakeSuccessResponse(202, b'{"message":"accepted"}')

    monkeypatch.setattr(controller_module, "urlopen", fake_urlopen)
    api.cancel(123)

    assert calls == [
        ("https://api.github.com/repos/owner/repo/actions/runs/123/cancel", "POST", 20),
    ]


def test_cancel_ignores_arbitrary_202_response_body(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")

    class _UnreadableSuccessResponse(_FakeSuccessResponse):
        def read(self) -> bytes:
            raise AssertionError("202 cancellation body must not be read")

    monkeypatch.setattr(
        controller_module,
        "urlopen",
        lambda *_args, **_kwargs: _UnreadableSuccessResponse(
            202,
            b"not-json-and-non-authoritative",
        ),
    )

    api.cancel(123)


def test_cancel_rejects_missing_transport_acceptance_authority(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    monkeypatch.setattr(api, "_request", lambda *_args, **_kwargs: None)

    with pytest.raises(
        CancellationError,
        match="missing HTTP 202 acceptance authority",
    ):
        api.cancel(123)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, b'{"message":"ok"}'),
        (204, b""),
    ],
)
def test_cancel_rejects_undocumented_success_status(monkeypatch, status, body) -> None:
    api = GitHubApi(repository="owner/repo", token="token")

    monkeypatch.setattr(
        controller_module,
        "urlopen",
        lambda *_args, **_kwargs: _FakeSuccessResponse(status, body),
    )

    with pytest.raises(
        CancellationError,
        match="cancellation returned unexpected HTTP status",
    ):
        api.cancel(123)


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
