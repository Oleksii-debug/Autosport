from __future__ import annotations

from urllib.error import HTTPError

import pytest

import scripts.cancel_superseded_pr_workflow_runs as controller_module
from scripts.cancel_superseded_pr_workflow_runs import (
    CancellationError,
    GitHubApi,
)
from scripts.cancel_superseded_pr_workflow_runs_scoped import WorkflowScopedGitHubApi


def test_cancel_conflict_is_benign_after_run_completed(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    calls: list[tuple[str, str]] = []

    def fake_urlopen(request, *, timeout: int):
        del timeout
        calls.append((request.full_url, request.get_method()))
        if request.get_method() == "POST":
            raise HTTPError(
                request.full_url,
                409,
                "Conflict",
                hdrs=None,
                fp=None,
            )
        return _FakeSuccessResponse(200, b'{"status":"completed"}')

    monkeypatch.setattr(controller_module, "urlopen", fake_urlopen)
    api.cancel(123)

    assert calls == [
        ("https://api.github.com/repos/owner/repo/actions/runs/123/cancel", "POST"),
        ("https://api.github.com/repos/owner/repo/actions/runs/123", "GET"),
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


def test_cancel_rejects_instance_shadowed_request_dispatch(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    monkeypatch.setattr(api, "_request", lambda *_args, **_kwargs: None)

    with pytest.raises(
        CancellationError,
        match="cancellation request dispatch changed",
    ):
        api.cancel(123)


def test_cancel_rejects_class_mutated_request_dispatch(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    monkeypatch.setattr(
        GitHubApi,
        "_request",
        lambda *_args, **_kwargs: controller_module._CANCELLATION_ACCEPTED,
    )

    with pytest.raises(
        CancellationError,
        match="cancellation request dispatch changed",
    ):
        api.cancel(123)


def test_cancel_rejects_coordinated_request_and_witness_rebind(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    forged_acceptance = object()

    def hostile_request(*_args, **_kwargs):
        return forged_acceptance

    monkeypatch.setattr(GitHubApi, "_request", hostile_request)
    monkeypatch.setattr(
        controller_module,
        "_CANONICAL_REQUEST",
        hostile_request,
        raising=False,
    )
    monkeypatch.setattr(
        controller_module,
        "_CANCELLATION_ACCEPTED",
        forged_acceptance,
    )

    with pytest.raises(
        CancellationError,
        match="cancellation request dispatch changed",
    ):
        api.cancel(123)


def test_cancel_rejects_subclass_request_override_even_with_internal_witness() -> None:
    class _ForgedRequestApi(GitHubApi):
        def _request(self, *_args, **_kwargs):
            return controller_module._CANCELLATION_ACCEPTED

    api = _ForgedRequestApi(repository="owner/repo", token="token")

    with pytest.raises(
        CancellationError,
        match="cancellation request dispatch changed",
    ):
        api.cancel(123)


def test_scoped_production_api_inherits_canonical_cancel_transport_dispatch(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=1,
        workflow_name="CI",
    )
    monkeypatch.setattr(
        controller_module,
        "urlopen",
        lambda *_args, **_kwargs: _FakeSuccessResponse(202, b"accepted"),
    )

    api.cancel(123)


def test_scoped_production_api_uses_captured_base_cancel_after_class_mutation(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=1,
        workflow_name="CI",
    )
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(GitHubApi, "cancel", lambda *_args, **_kwargs: None)

    def fake_urlopen(request, *, timeout: int):
        del timeout
        calls.append((request.full_url, request.get_method()))
        return _FakeSuccessResponse(202, b"accepted")

    monkeypatch.setattr(controller_module, "urlopen", fake_urlopen)

    api.cancel(123)

    assert calls == [
        ("https://api.github.com/repos/owner/repo/actions/runs/123/cancel", "POST"),
    ]


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

    def fake_urlopen(request, *, timeout: int):
        del timeout
        if request.get_method() == "POST":
            raise HTTPError(
                request.full_url,
                409,
                "Conflict",
                hdrs=None,
                fp=None,
            )
        return _FakeSuccessResponse(200, b'{"status":"in_progress"}')

    monkeypatch.setattr(controller_module, "urlopen", fake_urlopen)
    with pytest.raises(
        CancellationError,
        match="cancellation conflicted while run remains active",
    ):
        api.cancel(123)


def test_cancel_conflict_status_reread_ignores_shadowed_helper(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")

    def fake_urlopen(request, *, timeout: int):
        del timeout
        if request.get_method() == "POST":
            raise HTTPError(
                request.full_url,
                409,
                "Conflict",
                hdrs=None,
                fp=None,
            )
        return _FakeSuccessResponse(200, b'{"status":"in_progress"}')

    monkeypatch.setattr(controller_module, "urlopen", fake_urlopen)
    monkeypatch.setattr(api, "workflow_run_status", lambda _run_id: "completed")

    with pytest.raises(
        CancellationError,
        match="cancellation conflicted while run remains active",
    ):
        api.cancel(123)


@pytest.mark.parametrize("status", [201, 202, 204])
def test_cancel_conflict_rejects_non_200_status_reread(
    monkeypatch,
    status: int,
) -> None:
    api = GitHubApi(repository="owner/repo", token="token")

    def fake_urlopen(request, *, timeout: int):
        del timeout
        if request.get_method() == "POST":
            raise HTTPError(
                request.full_url,
                409,
                "Conflict",
                hdrs=None,
                fp=None,
            )
        return _FakeSuccessResponse(status, b'{"status":"completed"}')

    monkeypatch.setattr(controller_module, "urlopen", fake_urlopen)

    with pytest.raises(
        CancellationError,
        match="GitHub API GET returned unexpected HTTP status",
    ):
        api.cancel(123)


def test_workflow_run_status_requires_exact_http_200(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    monkeypatch.setattr(
        controller_module,
        "urlopen",
        lambda *_args, **_kwargs: _FakeSuccessResponse(
            202,
            b'{"status":"completed"}',
        ),
    )

    with pytest.raises(
        CancellationError,
        match="GitHub API GET returned unexpected HTTP status",
    ):
        api.workflow_run_status(123)


def test_workflow_run_status_rejects_unknown_state(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    monkeypatch.setattr(
        api,
        "_request",
        lambda *_args, **_kwargs: {"status": "mystery"},
    )

    with pytest.raises(CancellationError, match="invalid workflow-run status"):
        api.workflow_run_status(123)
