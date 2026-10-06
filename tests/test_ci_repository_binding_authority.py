from __future__ import annotations

import pytest

import scripts.cancel_superseded_pr_workflow_runs as base_controller
import scripts.cancel_superseded_pr_workflow_runs_scoped as scoped_controller
from scripts.cancel_superseded_pr_workflow_runs import CancellationError
from scripts.cancel_superseded_pr_workflow_runs_scoped import (
    WorkflowScopedGitHubApi,
    _explicit_run_identity_is_current,
    _trusted_live_pr_qualification,
)


HEAD = "a" * 40


def test_scoped_authority_readers_reject_production_api_subclass() -> None:
    class ForgedScopedApi(WorkflowScopedGitHubApi):
        pass

    api = ForgedScopedApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )

    assert (
        _explicit_run_identity_is_current(
            api,
            run_id=123,
            expected_head_sha=HEAD,
            pr_number=303,
        )
        is False
    )
    with pytest.raises(
        CancellationError,
        match="live PR qualification API type changed",
    ):
        _trusted_live_pr_qualification(api, 303)


def test_repository_coordinate_rejects_direct_instance_rebind() -> None:
    api = base_controller.GitHubApi(repository="owner/repo", token="token")

    with pytest.raises(AttributeError):
        api._repository = "other/repo"  # type: ignore[misc]

    assert api._repository == "owner/repo"


def test_base_live_qualification_rejects_repository_drift_during_read(
    monkeypatch,
) -> None:
    api = base_controller.GitHubApi(repository="owner/repo", token="token")

    def drifting_pull_request(pr_number: int):
        assert pr_number == 2008
        api.__dict__["_GitHubApi__repository"] = "other/repo"
        return {
            "state": "open",
            "draft": False,
            "head": {
                "sha": HEAD,
                "repo": {"full_name": "other/repo"},
            },
            "base": {"repo": {"full_name": "other/repo"}},
        }

    monkeypatch.setattr(api, "_pull_request", drifting_pull_request)

    with pytest.raises(
        CancellationError,
        match="GitHub API repository binding changed",
    ):
        api.live_pr_qualification(2008)


def test_scoped_live_qualification_rejects_repository_drift_during_read(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    canonical_request = scoped_controller.GitHubApi._request

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            return (
                b'{"state":"open","draft":false,'
                b'"head":{"sha":"'
                + HEAD.encode("ascii")
                + b'","repo":{"full_name":"other/repo"}},'
                b'"base":{"repo":{"full_name":"other/repo"}}}'
            )

    def drifting_urlopen(request, *, timeout: int):
        assert timeout == 20
        assert request.full_url == "https://api.github.com/repos/owner/repo/pulls/303"
        api.__dict__["_GitHubApi__repository"] = "other/repo"
        return FakeResponse()

    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        drifting_urlopen,
    )

    with pytest.raises(
        CancellationError,
        match="GitHub API repository binding changed",
    ):
        _trusted_live_pr_qualification(api, 303)



def test_base_live_qualification_ignores_repository_property_class_shadow(
    monkeypatch,
) -> None:
    api = base_controller.GitHubApi(repository="owner/repo", token="token")
    canonical_request = base_controller.GitHubApi._request
    requested: list[str] = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            return (
                b'{"state":"open","draft":false,'
                b'"head":{"sha":"'
                + HEAD.encode("ascii")
                + b'","repo":{"full_name":"owner/repo"}},'
                b'"base":{"repo":{"full_name":"owner/repo"}}}'
            )

    def canonical_urlopen(request, *, timeout: int):
        assert timeout == 20
        requested.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(
        base_controller.GitHubApi,
        "_repository",
        "other/repo",
    )
    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        canonical_urlopen,
    )

    assert api.live_pr_qualification(2008) == (HEAD, True)
    assert requested == ["https://api.github.com/repos/owner/repo/pulls/2008"]


def test_scoped_live_qualification_ignores_repository_property_class_shadow(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    canonical_request = scoped_controller.GitHubApi._request
    requested: list[str] = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            return (
                b'{"state":"open","draft":false,'
                b'"head":{"sha":"'
                + HEAD.encode("ascii")
                + b'","repo":{"full_name":"owner/repo"}},'
                b'"base":{"repo":{"full_name":"owner/repo"}}}'
            )

    def canonical_urlopen(request, *, timeout: int):
        assert timeout == 20
        requested.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(
        base_controller.GitHubApi,
        "_repository",
        "other/repo",
    )
    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        canonical_urlopen,
    )

    assert _trusted_live_pr_qualification(api, 303) == (HEAD, True)
    assert requested == ["https://api.github.com/repos/owner/repo/pulls/303"]


def test_scoped_active_run_enumeration_rejects_workflow_binding_drift(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )

    def drifting_request(
        path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ):
        assert method == "GET"
        assert not allowed_http_errors
        assert "/actions/workflows/356678400/runs?" in path
        api.__dict__["_WorkflowScopedGitHubApi__workflow_id"] = 999
        return {"total_count": 0, "workflow_runs": []}

    monkeypatch.setattr(api, "_request", drifting_request)

    with pytest.raises(CancellationError, match="source workflow binding changed"):
        api._active_runs_for_status("queued")


def test_explicit_run_identity_rejects_workflow_binding_drift_during_get(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    canonical_request = scoped_controller.GitHubApi._request

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            return (
                b'{"id":77,"workflow_id":356678400,"event":"pull_request",'
                b'"head_sha":"' + HEAD.encode("ascii") + b'",'
                b'"name":"CI","status":"queued",'
                b'"pull_requests":[{"number":303}]}'
            )

    def drifting_urlopen(request, *, timeout: int):
        assert timeout == 20
        assert request.full_url == (
            "https://api.github.com/repos/owner/repo/actions/runs/77"
        )
        api.__dict__["_WorkflowScopedGitHubApi__workflow_id"] = 999
        return FakeResponse()

    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        drifting_urlopen,
    )

    assert not _explicit_run_identity_is_current(
        api,
        run_id=77,
        expected_head_sha=HEAD,
        pr_number=303,
    )


def test_unbound_cancel_rejects_foreign_workflow_run_at_effect_boundary(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    api._zero_association_recovered_runs[77] = (HEAD, "feature/stale")
    canonical_request = scoped_controller.GitHubApi._request

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            return (
                b'{"id":77,"workflow_id":999999,"event":"pull_request",'
                b'"head_sha":"' + HEAD.encode("ascii") + b'",'
                b'"name":"Other Workflow","status":"queued",'
                b'"pull_requests":[]}'
            )

    requested: list[str] = []

    def foreign_run_urlopen(request, *, timeout: int):
        assert timeout == 20
        requested.append(request.full_url)
        return FakeResponse()

    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        foreign_run_urlopen,
    )

    with pytest.raises(
        CancellationError,
        match="unbound workflow run identity changed",
    ):
        api.cancel(77)

    assert requested == [
        "https://api.github.com/repos/owner/repo/actions/runs/77"
    ]


def test_unbound_cancel_rejects_reappeared_pr_reference_at_effect_boundary(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    api._recovered_runs[78] = (303, HEAD)
    canonical_request = scoped_controller.GitHubApi._request

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            return (
                b'{"id":78,"workflow_id":356678400,"event":"pull_request",'
                b'"head_sha":"' + HEAD.encode("ascii") + b'",'
                b'"name":"CI","status":"in_progress",'
                b'"pull_requests":[{"number":303}]}'
            )

    def rebound_identity_urlopen(request, *, timeout: int):
        assert timeout == 20
        assert request.full_url == (
            "https://api.github.com/repos/owner/repo/actions/runs/78"
        )
        return FakeResponse()

    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        rebound_identity_urlopen,
    )

    with pytest.raises(
        CancellationError,
        match="unbound workflow run identity changed",
    ):
        api.cancel(78)



def test_zero_association_cancel_rejects_changed_head_branch_at_effect_boundary(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    api._zero_association_recovered_runs[79] = (HEAD, "feature/original")
    canonical_request = scoped_controller.GitHubApi._request

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            return (
                b'{"id":79,"workflow_id":356678400,"event":"pull_request",'
                b'"head_sha":"' + HEAD.encode("ascii") + b'",'
                b'"head_branch":"feature/other",'
                b'"head_repository":{"full_name":"owner/repo"},'
                b'"name":"CI","status":"queued","pull_requests":[]}'
            )

    def changed_branch_urlopen(request, *, timeout: int):
        assert timeout == 20
        assert request.full_url == (
            "https://api.github.com/repos/owner/repo/actions/runs/79"
        )
        return FakeResponse()

    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        changed_branch_urlopen,
    )

    with pytest.raises(
        CancellationError,
        match="unbound workflow run identity changed",
    ):
        api.cancel(79)


def test_zero_association_cancel_rejects_foreign_head_repository_at_effect_boundary(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    api._zero_association_recovered_runs[80] = (HEAD, "feature/original")
    canonical_request = scoped_controller.GitHubApi._request

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            return (
                b'{"id":80,"workflow_id":356678400,"event":"pull_request",'
                b'"head_sha":"' + HEAD.encode("ascii") + b'",'
                b'"head_branch":"feature/original",'
                b'"head_repository":{"full_name":"other/repo"},'
                b'"name":"CI","status":"queued","pull_requests":[]}'
            )

    def foreign_repository_urlopen(request, *, timeout: int):
        assert timeout == 20
        assert request.full_url == (
            "https://api.github.com/repos/owner/repo/actions/runs/80"
        )
        return FakeResponse()

    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        foreign_repository_urlopen,
    )

    with pytest.raises(
        CancellationError,
        match="unbound workflow run identity changed",
    ):
        api.cancel(80)

def test_scoped_workflow_id_cannot_be_redirected_by_validator_rebind(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        scoped_controller,
        "_require_positive_int",
        lambda value, *, field: 999,
    )

    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )

    assert object.__getattribute__(
        api,
        "_WorkflowScopedGitHubApi__workflow_id",
    ) == 356678400


def test_singleton_event_pr_identity_cannot_be_redirected_by_validator_rebind(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        scoped_controller,
        "_require_positive_int",
        lambda value, *, field: 999,
    )

    assert scoped_controller._validated_event_pr_identity(
        303,
        reference_mode="singleton",
    ) == (303, False)


def test_explicit_sweep_current_run_cannot_be_reclassified_by_validator_rebind(
    monkeypatch,
) -> None:
    current_head = "c" * 40
    cancelled: list[int] = []

    class FixtureApi:
        _WorkflowScopedGitHubApi__workflow_name = "CI"

    run = base_controller.WorkflowRun(
        run_id=123,
        head_sha=current_head,
        workflow_name="CI",
        pr_numbers=(303,),
        status="queued",
    )

    monkeypatch.setattr(
        scoped_controller,
        "_require_positive_int",
        lambda value, *, field: 999 if field == "current run id" else value,
    )
    monkeypatch.setattr(
        scoped_controller,
        "_trusted_live_pr_qualification",
        lambda *_args, **_kwargs: (current_head, False),
    )
    monkeypatch.setattr(
        scoped_controller,
        "_explicit_run_identity_is_current",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        scoped_controller,
        "_cancel_run_or_defer_active_conflict",
        lambda _api, run_id: cancelled.append(run_id) is None or True,
    )

    assert scoped_controller.cancel_superseded_explicit_pr_runs(
        FixtureApi(),  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=123,
        runs=(run,),
    ) == ()
    assert cancelled == []


def test_trigger_current_run_coordinate_cannot_be_redirected_by_validator_rebind(
    monkeypatch,
) -> None:
    event_head = "d" * 40
    live_head = "e" * 40
    checked: list[int] = []
    cancelled: list[int] = []

    monkeypatch.setattr(
        scoped_controller,
        "_require_positive_int",
        lambda value, *, field: 999 if field == "current run id" else value,
    )

    def check_identity(_api, *, run_id, expected_head_sha, pr_number):
        assert expected_head_sha == event_head
        assert pr_number == 303
        checked.append(run_id)
        return True

    def read_qualification(_api, pr_number):
        assert pr_number == 303
        return (live_head, True)

    def cancel_effect(_api, run_id):
        cancelled.append(run_id)
        return True

    assert scoped_controller._cancel_triggering_run_if_stale_or_nonqualifying(
        object(),  # type: ignore[arg-type]
        pr_number=303,
        event_head_sha=event_head,
        current_run_id=123,
        qualification=(live_head, True),
        _identity_checker=check_identity,
        _identity_checker_code=check_identity.__code__,
        _qualification_reader=read_qualification,
        _qualification_reader_code=read_qualification.__code__,
        _cancel_effect=cancel_effect,
        _cancel_effect_code=cancel_effect.__code__,
    )
    assert checked == [123]
    assert cancelled == [123]

def test_commit_association_rejects_repository_drift_before_next_page(
    monkeypatch,
) -> None:
    api = base_controller.GitHubApi(repository="owner/repo", token="token")
    requested_urls: list[str] = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            api.__dict__["_GitHubApi__repository"] = "other/repo"
            rows = ",".join(
                (
                    '{"number":2008,"head":{"sha":"' + HEAD + '"}}'
                    for _ in range(100)
                )
            )
            return ("[" + rows + "]").encode("ascii")

    def drifting_urlopen(request, *, timeout: int):
        assert timeout == 20
        requested_urls.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(base_controller, "urlopen", drifting_urlopen)

    with pytest.raises(
        CancellationError,
        match="GitHub API repository binding changed",
    ):
        api.associated_pr_number(HEAD)

    assert len(requested_urls) == 1
    assert requested_urls[0].startswith(
        "https://api.github.com/repos/owner/repo/commits/"
    )

