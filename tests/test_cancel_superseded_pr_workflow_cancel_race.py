from __future__ import annotations

from urllib.error import HTTPError

import pytest

import scripts.cancel_superseded_pr_workflow_runs as controller_module
import scripts.cancel_superseded_pr_workflow_runs_scoped as scoped_controller
from scripts.cancel_superseded_pr_workflow_runs import (
    CancellationError,
    GitHubApi,
    _pull_request_qualification_state,
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


def _unbound_run_body(
    run_id: int,
    head_sha: str,
    *,
    head_branch: str = "stale-branch",
) -> bytes:
    return (
        f'{{"id":{run_id},"workflow_id":1,"event":"pull_request",'
        f'"head_sha":"{head_sha}","status":"queued","name":"CI",'
        f'"pull_requests":[],"head_branch":"{head_branch}",'
        '"head_repository":{"full_name":"owner/repo"}}'
    ).encode()


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


def test_base_cancel_run_id_cannot_be_redirected_by_validator_rebind(
    monkeypatch,
) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    calls: list[tuple[str, str]] = []

    def redirect_run_id(value, *, field: str):
        assert field == "run id"
        return 999 if value == 123 else value

    def fake_urlopen(request, *, timeout: int):
        assert timeout == 20
        calls.append((request.full_url, request.get_method()))
        return _FakeSuccessResponse(202, b"accepted")

    monkeypatch.setattr(controller_module, "_require_positive_int", redirect_run_id)
    monkeypatch.setattr(controller_module, "urlopen", fake_urlopen)

    api.cancel(123)

    assert calls == [
        ("https://api.github.com/repos/owner/repo/actions/runs/123/cancel", "POST"),
    ]


def test_cancel_wrapper_run_id_cannot_be_redirected_by_validator_rebind(
    monkeypatch,
) -> None:
    cancelled: list[int] = []

    class RecordingApi:
        def cancel(self, run_id: int) -> None:
            cancelled.append(run_id)

    def redirect_run_id(value, *, field: str):
        assert field == "run id"
        return 999 if value == 123 else value

    monkeypatch.setattr(scoped_controller, "_require_positive_int", redirect_run_id)

    assert scoped_controller._cancel_run_or_defer_active_conflict(
        RecordingApi(),  # type: ignore[arg-type]
        123,
    )
    assert cancelled == [123]


def test_cancel_wrapper_rejects_production_instance_shadowed_cancel_dispatch(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=1,
        workflow_name="CI",
    )
    redirected: list[int] = []
    monkeypatch.setattr(api, "cancel", lambda run_id: redirected.append(run_id))

    with pytest.raises(
        CancellationError,
        match="workflow run cancellation dispatch changed",
    ):
        scoped_controller._cancel_run_or_defer_active_conflict(api, 123)

    assert redirected == []


def test_cancel_wrapper_rejects_production_class_rebound_cancel_dispatch(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=1,
        workflow_name="CI",
    )
    redirected: list[int] = []
    monkeypatch.setattr(
        WorkflowScopedGitHubApi,
        "cancel",
        lambda _self, run_id: redirected.append(run_id),
    )

    with pytest.raises(
        CancellationError,
        match="workflow run cancellation dispatch changed",
    ):
        scoped_controller._cancel_run_or_defer_active_conflict(api, 123)

    assert redirected == []


def test_cancel_wrapper_rejects_production_api_subclass() -> None:
    class ForgedScopedApi(WorkflowScopedGitHubApi):
        pass

    api = ForgedScopedApi(
        repository="owner/repo",
        token="token",
        workflow_id=1,
        workflow_name="CI",
    )

    with pytest.raises(
        CancellationError,
        match="workflow run cancellation API type changed",
    ):
        scoped_controller._cancel_run_or_defer_active_conflict(api, 123)


def test_cancel_wrapper_calls_captured_production_cancel_directly(monkeypatch) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=1,
        workflow_name="CI",
    )
    calls: list[tuple[str, str]] = []

    def fake_urlopen(request, *, timeout: int):
        assert timeout == 20
        calls.append((request.full_url, request.get_method()))
        return _FakeSuccessResponse(202, b"accepted")

    monkeypatch.setattr(controller_module, "urlopen", fake_urlopen)

    assert scoped_controller._cancel_run_or_defer_active_conflict(api, 123)
    assert calls == [
        ("https://api.github.com/repos/owner/repo/actions/runs/123/cancel", "POST"),
    ]


def test_scoped_cancel_run_id_cannot_be_redirected_by_validator_rebind(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=1,
        workflow_name="CI",
    )
    calls: list[tuple[str, str]] = []

    def redirect_run_id(value, *, field: str):
        assert field == "run id"
        return 999 if value == 123 else value

    def fake_urlopen(request, *, timeout: int):
        assert timeout == 20
        calls.append((request.full_url, request.get_method()))
        return _FakeSuccessResponse(202, b"accepted")

    monkeypatch.setattr(controller_module, "_require_positive_int", redirect_run_id)
    monkeypatch.setattr(scoped_controller, "_require_positive_int", redirect_run_id)
    monkeypatch.setattr(controller_module, "urlopen", fake_urlopen)

    api.cancel(123)

    assert calls == [
        ("https://api.github.com/repos/owner/repo/actions/runs/123/cancel", "POST"),
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


def test_cancel_rejects_request_kwdefault_rebase_before_post(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    defaults = GitHubApi._request.__kwdefaults__
    assert defaults is not None
    invoked = {"value": False}

    def forbidden_urlopen(*_args, **_kwargs):
        invoked["value"] = True
        raise AssertionError("rebased request defaults must revoke cancel authority")

    monkeypatch.setitem(defaults, "_json_parse_int", str)
    monkeypatch.setattr(controller_module, "urlopen", forbidden_urlopen)

    with pytest.raises(
        CancellationError,
        match="cancellation request dispatch changed",
    ):
        api.cancel(123)
    assert not invoked["value"]


def test_cancel_rejects_request_kwdefault_rebase_during_post(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    defaults = GitHubApi._request.__kwdefaults__
    assert defaults is not None

    def mutating_urlopen(request, *, timeout: int):
        assert timeout == 20
        assert request.get_method() == "POST"
        monkeypatch.setitem(defaults, "_json_parse_int", str)
        return _FakeSuccessResponse(202, b"accepted")

    monkeypatch.setattr(controller_module, "urlopen", mutating_urlopen)

    with pytest.raises(
        CancellationError,
        match="cancellation request dispatch changed",
    ):
        api.cancel(123)


def test_cancel_rejects_in_place_request_code_rebind() -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    original_code = GitHubApi._request.__code__

    def forged_request(
        self,
        path,
        *,
        method="GET",
        allowed_http_errors=frozenset(),
    ):
        del self, path, method, allowed_http_errors
        return globals()["_CANCELLATION_ACCEPTED"]

    forged_code = forged_request.__code__
    assert len(forged_code.co_freevars) == len(original_code.co_freevars)

    try:
        GitHubApi._request.__code__ = forged_code
        with pytest.raises(
            CancellationError,
            match="cancellation request dispatch changed",
        ):
            api.cancel(123)
    finally:
        GitHubApi._request.__code__ = original_code


def test_cancel_conflict_rechecks_request_code_before_status_reread(
    monkeypatch,
) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    original_code = GitHubApi._request.__code__

    def forged_request(
        self,
        path,
        *,
        method="GET",
        allowed_http_errors=frozenset(),
    ):
        del self, path, method, allowed_http_errors
        return {"status": "completed"}

    forged_code = forged_request.__code__
    assert len(forged_code.co_freevars) == len(original_code.co_freevars)

    def fake_urlopen(request, *, timeout: int):
        del timeout
        if request.get_method() != "POST":
            raise AssertionError("canonical status GET must not run after code mutation")
        GitHubApi._request.__code__ = forged_code
        raise HTTPError(
            request.full_url,
            409,
            "Conflict",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr(controller_module, "urlopen", fake_urlopen)
    try:
        with pytest.raises(
            CancellationError,
            match="cancellation request dispatch changed",
        ):
            api.cancel(123)
    finally:
        GitHubApi._request.__code__ = original_code


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


def test_scoped_cancel_rejects_instance_shadowed_request_dispatch(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=1,
        workflow_name="CI",
    )
    called = False

    def forged_request(*_args, **_kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(api, "_request", forged_request)

    with pytest.raises(
        CancellationError,
        match="scoped cancellation revalidation dispatch changed",
    ):
        api.cancel(123)

    assert called is False


def test_scoped_production_api_rejects_in_place_base_cancel_code_rebind() -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=1,
        workflow_name="CI",
    )
    original_code = GitHubApi.cancel.__code__

    def make_forged_cancel():
        allowed_http_error_type = object()
        cancellation_accepted = object()
        request_impl = object()
        request_impl_code = object()

        def forged_cancel(self, run_id):
            del self, run_id
            return (
                allowed_http_error_type,
                cancellation_accepted,
                request_impl,
                request_impl_code,
            )

        return forged_cancel

    forged_code = make_forged_cancel().__code__
    assert len(forged_code.co_freevars) == len(original_code.co_freevars)

    try:
        GitHubApi.cancel.__code__ = forged_code
        with pytest.raises(
            CancellationError,
            match="canonical base cancellation authority changed",
        ):
            api.cancel(123)
    finally:
        GitHubApi.cancel.__code__ = original_code


@pytest.mark.parametrize(
    "cancel_impl",
    [GitHubApi.cancel, WorkflowScopedGitHubApi.cancel],
)
def test_cancellation_authority_closures_reject_writable_builtin_containers(
    cancel_impl,
) -> None:
    captured = tuple(
        cell.cell_contents for cell in (cancel_impl.__closure__ or ())
    )

    assert not any(type(value) in {dict, list, set} for value in captured)


def test_scoped_cancel_helper_dispatch_metadata_is_immutable_and_complete() -> None:
    closure = {
        name: cell.cell_contents
        for name, cell in zip(
            WorkflowScopedGitHubApi.cancel.__code__.co_freevars,
            WorkflowScopedGitHubApi.cancel.__closure__ or (),
            strict=True,
        )
    }
    helper_dispatch = closure["helper_dispatch"]

    assert type(helper_dispatch) is tuple
    assert tuple(name for name, _, _, _ in helper_dispatch) == (
        "_request",
        "_historical_associated_pr_number",
        "_historical_head_has_no_associated_prs",
        "_canonical_branch_head",
        "live_pr_qualification",
        "_pull_request",
    )
    assert all(
        type(entry) is tuple
        and len(entry) == 4
        and entry[2] is not None
        and type(entry[3]) is tuple
        and len(entry[3]) == 3
        for entry in helper_dispatch
    )


def test_scoped_cancel_rechecks_branch_helper_after_zero_association_roundtrip(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=1,
        workflow_name="CI",
    )
    candidate_head = "a" * 40
    api._zero_association_recovered_runs[123] = (candidate_head, "stale-branch")
    helper = WorkflowScopedGitHubApi._canonical_branch_head
    original_code = helper.__code__

    def forged_branch(self, branch):
        del self, branch
        return "b" * 40

    forged_code = forged_branch.__code__
    assert len(forged_code.co_freevars) == len(original_code.co_freevars)

    def fake_urlopen(request, *, timeout: int):
        del timeout
        url = request.full_url
        if url.endswith("/actions/runs/123"):
            return _FakeSuccessResponse(
                200,
                _unbound_run_body(123, candidate_head),
            )
        if "/commits/" in url and "/pulls?" in url:
            helper.__code__ = forged_code
            return _FakeSuccessResponse(200, b"[]")
        raise AssertionError("mutated branch revalidation helper must not execute")

    monkeypatch.setattr(controller_module, "urlopen", fake_urlopen)
    try:
        with pytest.raises(
            CancellationError,
            match="unbound workflow run branch authority could not be revalidated",
        ):
            api.cancel(123)
    finally:
        helper.__code__ = original_code


def test_scoped_cancel_rechecks_live_qualification_after_association_roundtrip(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=1,
        workflow_name="CI",
    )
    candidate_head = "a" * 40
    api._recovered_runs[123] = (7, candidate_head)
    helper = GitHubApi.live_pr_qualification
    original_code = helper.__code__

    def forged_live_qualification(self, pr_number):
        del self, pr_number
        return globals()["PullRequestQualification"](
            head_sha="b" * 40,
            integration_capable=True,
        )

    forged_code = forged_live_qualification.__code__
    assert len(forged_code.co_freevars) == len(original_code.co_freevars)

    def fake_urlopen(request, *, timeout: int):
        del timeout
        url = request.full_url
        if url.endswith("/actions/runs/123"):
            return _FakeSuccessResponse(
                200,
                _unbound_run_body(123, candidate_head),
            )
        if "/commits/" in url and "/pulls?" in url:
            helper.__code__ = forged_code
            body = (
                '[{"number":7,"head":{"sha":"' + candidate_head + '"}}]'
            ).encode()
            return _FakeSuccessResponse(200, body)
        raise AssertionError("mutated live qualification helper must not execute")

    monkeypatch.setattr(controller_module, "urlopen", fake_urlopen)
    try:
        with pytest.raises(
            CancellationError,
            match="scoped cancellation revalidation dispatch changed",
        ):
            api.cancel(123)
    finally:
        helper.__code__ = original_code


def test_scoped_cancel_rechecks_qualification_reader_after_live_roundtrip(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=1,
        workflow_name="CI",
    )
    candidate_head = "a" * 40
    api._recovered_runs[123] = (7, candidate_head)
    reader = _pull_request_qualification_state
    original_code = reader.__code__

    def make_forged_reader():
        field_class_witnesses = ()
        qualification_dict_descriptor = object()
        qualification_init = object()
        qualification_init_code = object()
        qualification_type = object()

        def forged_reader(_qualification):
            _ = (
                field_class_witnesses,
                qualification_dict_descriptor,
                qualification_init,
                qualification_init_code,
                qualification_type,
            )
            return "a" * 40, False

        return forged_reader

    forged_code = make_forged_reader().__code__
    assert len(forged_code.co_freevars) == len(original_code.co_freevars)

    def fake_urlopen(request, *, timeout: int):
        del timeout
        url = request.full_url
        if url.endswith("/actions/runs/123"):
            return _FakeSuccessResponse(
                200,
                _unbound_run_body(123, candidate_head),
            )
        if "/commits/" in url and "/pulls?" in url:
            body = (
                '[{"number":7,"head":{"sha":"' + candidate_head + '"}}]'
            ).encode()
            return _FakeSuccessResponse(200, body)
        if url.endswith("/pulls/7"):
            reader.__code__ = forged_code
            body = (
                '{"head":{"sha":"' + ("b" * 40)
                + '","repo":{"full_name":"owner/repo"}},'
                + '"base":{"repo":{"full_name":"owner/repo"}},'
                + '"state":"open","draft":false}'
            ).encode()
            return _FakeSuccessResponse(200, body)
        raise AssertionError("mutated qualification reader must not execute")

    monkeypatch.setattr(controller_module, "urlopen", fake_urlopen)
    try:
        with pytest.raises(
            CancellationError,
            match="pull request qualification reader authority changed",
        ):
            api.cancel(123)
    finally:
        reader.__code__ = original_code


def test_scoped_cancel_rechecks_base_cancel_code_after_external_revalidation(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=1,
        workflow_name="CI",
    )
    candidate_head = "a" * 40
    api._recovered_runs[123] = (7, candidate_head)
    original_code = GitHubApi.cancel.__code__

    def make_forged_cancel():
        allowed_http_error_type = object()
        cancellation_accepted = object()
        request_impl = object()
        request_impl_code = object()

        def forged_cancel(self, run_id):
            del self, run_id
            return (
                allowed_http_error_type,
                cancellation_accepted,
                request_impl,
                request_impl_code,
            )

        return forged_cancel

    forged_code = make_forged_cancel().__code__
    assert len(forged_code.co_freevars) == len(original_code.co_freevars)

    def fake_urlopen(request, *, timeout: int):
        del timeout
        url = request.full_url
        if url.endswith("/actions/runs/123"):
            return _FakeSuccessResponse(
                200,
                _unbound_run_body(123, candidate_head),
            )
        if "/commits/" in url and "/pulls?" in url:
            GitHubApi.cancel.__code__ = forged_code
            body = (
                '[{"number":7,"head":{"sha":"' + candidate_head + '"}}]'
            ).encode()
            return _FakeSuccessResponse(200, body)
        if url.endswith("/pulls/7"):
            body = (
                '{"head":{"sha":"' + ("b" * 40)
                + '","repo":{"full_name":"owner/repo"}},'
                + '"base":{"repo":{"full_name":"owner/repo"}},'
                + '"state":"open","draft":false}'
            ).encode()
            return _FakeSuccessResponse(200, body)
        raise AssertionError("mutated base cancellation transport must not execute")

    monkeypatch.setattr(controller_module, "urlopen", fake_urlopen)
    try:
        with pytest.raises(
            CancellationError,
            match="canonical base cancellation authority changed",
        ):
            api.cancel(123)
    finally:
        GitHubApi.cancel.__code__ = original_code


@pytest.mark.parametrize(
    "helper_name",
    [
        "_request",
        "_historical_associated_pr_number",
        "_historical_head_has_no_associated_prs",
        "_canonical_branch_head",
        "live_pr_qualification",
        "_pull_request",
    ],
)
def test_scoped_cancel_rejects_rebound_revalidation_helper(
    monkeypatch,
    helper_name: str,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=1,
        workflow_name="CI",
    )
    monkeypatch.setattr(
        WorkflowScopedGitHubApi,
        helper_name,
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(
        CancellationError,
        match="scoped cancellation revalidation dispatch changed",
    ):
        api.cancel(123)


def test_scoped_cancel_rejects_mutated_helper_code_identity() -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=1,
        workflow_name="CI",
    )
    helper = WorkflowScopedGitHubApi._canonical_branch_head
    original_code = helper.__code__
    replacement_code = (lambda self, branch: None).__code__
    try:
        helper.__code__ = replacement_code
        with pytest.raises(
            CancellationError,
            match="scoped cancellation revalidation dispatch changed",
        ):
            api.cancel(123)
    finally:
        helper.__code__ = original_code


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


def test_workflow_run_status_rejects_duplicate_json_object_keys(
    monkeypatch,
) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    monkeypatch.setattr(
        controller_module,
        "urlopen",
        lambda *_args, **_kwargs: _FakeSuccessResponse(
            200,
            b'{"status":"completed","status":"in_progress"}',
        ),
    )

    with pytest.raises(
        CancellationError,
        match="duplicate object key",
    ):
        api.workflow_run_status(123)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_workflow_run_status_rejects_nonstandard_json_constants(
    monkeypatch,
    constant: str,
) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    monkeypatch.setattr(
        controller_module,
        "urlopen",
        lambda *_args, **_kwargs: _FakeSuccessResponse(
            200,
            ('{"status":"completed","unexpected":' + constant + "}").encode(),
        ),
    )

    with pytest.raises(
        CancellationError,
        match="non-standard constant",
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

def test_cancel_wrapper_defers_only_frozen_active_conflict_message(monkeypatch) -> None:
    class ActiveConflictApi:
        def cancel(self, run_id: int) -> None:
            assert run_id == 321
            raise CancellationError(
                "workflow run cancellation conflicted while run remains active"
            )

    assert not scoped_controller._cancel_run_or_defer_active_conflict(
        ActiveConflictApi(),  # type: ignore[arg-type]
        321,
    )

    class FatalApi:
        def cancel(self, run_id: int) -> None:
            assert run_id == 322
            raise CancellationError("different fatal cancellation error")

    # A runtime module-global injection must not widen the one exact conflict that the
    # composed helper is allowed to defer.
    monkeypatch.setattr(
        scoped_controller,
        "_ACTIVE_CANCELLATION_CONFLICT_MESSAGE",
        "different fatal cancellation error",
        raising=False,
    )
    with pytest.raises(CancellationError, match="different fatal cancellation error"):
        scoped_controller._cancel_run_or_defer_active_conflict(
            FatalApi(),  # type: ignore[arg-type]
            322,
        )

def test_request_ignores_inflight_json_decoder_global_rebind(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")

    class RebindingResponse(_FakeSuccessResponse):
        def read(self) -> bytes:
            monkeypatch.setattr(
                controller_module.json,
                "loads",
                lambda *_args, **_kwargs: {"status": "forged"},
            )
            return b'{"status":"completed"}'

    monkeypatch.setattr(
        controller_module,
        "urlopen",
        lambda *_args, **_kwargs: RebindingResponse(200, b""),
    )

    assert api._request("/actions/runs/123") == {"status": "completed"}


def test_request_ignores_inflight_json_hook_global_rebind(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")

    class RebindingResponse(_FakeSuccessResponse):
        def read(self) -> bytes:
            monkeypatch.setattr(
                controller_module,
                "_strict_json_object",
                lambda pairs: dict(pairs),
            )
            return b'{"status":"completed","status":"in_progress"}'

    monkeypatch.setattr(
        controller_module,
        "urlopen",
        lambda *_args, **_kwargs: RebindingResponse(200, b""),
    )

    with pytest.raises(CancellationError, match="duplicate object key"):
        api._request("/actions/runs/123")


def test_request_rejects_inflight_json_hook_code_mutation(monkeypatch) -> None:
    api = GitHubApi(repository="owner/repo", token="token")
    strict_hook = controller_module._strict_json_object

    def forged_hook(pairs):
        return dict(pairs)

    forged_code = forged_hook.__code__.replace(
        co_freevars=strict_hook.__code__.co_freevars,
    )

    class MutatingResponse(_FakeSuccessResponse):
        def read(self) -> bytes:
            monkeypatch.setattr(strict_hook, "__code__", forged_code)
            return b'{"status":"completed"}'

    monkeypatch.setattr(
        controller_module,
        "urlopen",
        lambda *_args, **_kwargs: MutatingResponse(200, b""),
    )

    with pytest.raises(
        CancellationError,
        match="GitHub API JSON parser authority changed",
    ):
        api._request("/actions/runs/123")

