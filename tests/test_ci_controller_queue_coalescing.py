from __future__ import annotations

from pathlib import Path

import pytest

from scripts.cancel_superseded_pr_workflow_runs import (
    CancellationError,
    PullRequestQualification,
    WorkflowRun,
)
import scripts.cancel_superseded_pr_workflow_runs_scoped as scoped_controller
from scripts.cancel_superseded_pr_workflow_runs_scoped import (
    WorkflowScopedGitHubApi,
    _cancel_triggering_run_if_stale_or_nonqualifying,
    _explicit_run_identity_is_current,
    _explicit_singleton_pr_for_current_run,
    _trusted_live_pr_qualification,
    _validated_event_pr_identity,
    cancel_superseded_explicit_pr_runs,
)


HEAD = "a" * 40
STALE_HEAD = "b" * 40


class FakeApi:
    def __init__(self, qualification: PullRequestQualification) -> None:
        self.qualification = qualification
        self.cancelled: list[int] = []

    def live_pr_qualification(self, pr_number: int) -> PullRequestQualification:
        assert pr_number == 2039
        return self.qualification

    def cancel(self, run_id: int) -> None:
        self.cancelled.append(run_id)


def test_stale_triggering_source_run_is_cancelled_after_live_snapshot_recheck() -> None:
    qualification = PullRequestQualification(
        head_sha=HEAD,
        integration_capable=True,
    )
    api = FakeApi(qualification)

    assert _cancel_triggering_run_if_stale_or_nonqualifying(
        api,  # type: ignore[arg-type]
        pr_number=2039,
        event_head_sha=STALE_HEAD,
        current_run_id=91,
        qualification=qualification,
    )
    assert api.cancelled == [91]


def test_trigger_boundary_qualification_read_failure_fails_closed() -> None:
    qualification = PullRequestQualification(
        head_sha=HEAD,
        integration_capable=False,
    )

    class ReadFailureApi(FakeApi):
        def live_pr_qualification(self, pr_number: int) -> PullRequestQualification:
            assert pr_number == 2039
            raise CancellationError("fixture boundary reread unavailable")

    api = ReadFailureApi(qualification)

    assert not _cancel_triggering_run_if_stale_or_nonqualifying(
        api,  # type: ignore[arg-type]
        pr_number=2039,
        event_head_sha=HEAD,
        current_run_id=91,
        qualification=qualification,
    )
    assert api.cancelled == []


def test_trigger_cancel_effect_failure_is_not_swallowed() -> None:
    qualification = PullRequestQualification(
        head_sha=HEAD,
        integration_capable=False,
    )

    class CancelFailureApi(FakeApi):
        def cancel(self, run_id: int) -> None:
            assert run_id == 91
            raise CancellationError("fixture cancel effect unknown")

    api = CancelFailureApi(qualification)

    with pytest.raises(CancellationError, match="fixture cancel effect unknown"):
        _cancel_triggering_run_if_stale_or_nonqualifying(
            api,  # type: ignore[arg-type]
            pr_number=2039,
            event_head_sha=HEAD,
            current_run_id=91,
            qualification=qualification,
        )


def test_cancel_conflict_message_in_subclass_is_not_swallowed() -> None:
    qualification = PullRequestQualification(
        head_sha=HEAD,
        integration_capable=False,
    )

    class TypedBoundaryError(CancellationError):
        pass

    class TypedFailureApi(FakeApi):
        def cancel(self, run_id: int) -> None:
            assert run_id == 91
            raise TypedBoundaryError(
                "workflow run cancellation conflicted while run remains active"
            )

    api = TypedFailureApi(qualification)

    with pytest.raises(TypedBoundaryError):
        _cancel_triggering_run_if_stale_or_nonqualifying(
            api,  # type: ignore[arg-type]
            pr_number=2039,
            event_head_sha=HEAD,
            current_run_id=91,
            qualification=qualification,
        )


def test_trigger_active_cancel_conflict_is_deferred_without_false_success() -> None:
    qualification = PullRequestQualification(
        head_sha=HEAD,
        integration_capable=False,
    )

    class ActiveConflictApi(FakeApi):
        def cancel(self, run_id: int) -> None:
            assert run_id == 91
            raise CancellationError(
                "workflow run cancellation conflicted while run remains active"
            )

    api = ActiveConflictApi(qualification)

    assert not _cancel_triggering_run_if_stale_or_nonqualifying(
        api,  # type: ignore[arg-type]
        pr_number=2039,
        event_head_sha=HEAD,
        current_run_id=91,
        qualification=qualification,
    )
    assert api.cancelled == []


def test_trigger_boundary_run_identity_change_revokes_cancellation() -> None:
    qualification = PullRequestQualification(
        head_sha=HEAD,
        integration_capable=False,
    )

    class IdentityMovedApi(FakeApi):
        def _explicit_run_identity_matches(
            self,
            *,
            run_id: int,
            expected_head_sha: str,
            pr_number: int,
        ) -> bool:
            assert run_id == 91
            assert expected_head_sha == HEAD
            assert pr_number == 2039
            return False

        def live_pr_qualification(self, pr_number: int) -> PullRequestQualification:
            raise AssertionError(
                "revoked run identity must stop before PR qualification reread"
            )

    api = IdentityMovedApi(qualification)

    assert not _cancel_triggering_run_if_stale_or_nonqualifying(
        api,  # type: ignore[arg-type]
        pr_number=2039,
        event_head_sha=HEAD,
        current_run_id=91,
        qualification=qualification,
    )
    assert api.cancelled == []


def test_trigger_boundary_run_identity_read_failure_revokes_cancellation() -> None:
    qualification = PullRequestQualification(
        head_sha=HEAD,
        integration_capable=False,
    )

    class IdentityReadFailureApi(FakeApi):
        def _explicit_run_identity_matches(self, **_kwargs) -> bool:
            raise CancellationError("fixture run identity reread unavailable")

        def live_pr_qualification(self, pr_number: int) -> PullRequestQualification:
            raise AssertionError(
                "identity reread failure must stop before PR qualification reread"
            )

    api = IdentityReadFailureApi(qualification)

    assert not _cancel_triggering_run_if_stale_or_nonqualifying(
        api,  # type: ignore[arg-type]
        pr_number=2039,
        event_head_sha=HEAD,
        current_run_id=91,
        qualification=qualification,
    )
    assert api.cancelled == []


def test_current_ready_source_run_is_preserved() -> None:
    qualification = PullRequestQualification(
        head_sha=HEAD,
        integration_capable=True,
    )
    api = FakeApi(qualification)

    assert not _cancel_triggering_run_if_stale_or_nonqualifying(
        api,  # type: ignore[arg-type]
        pr_number=2039,
        event_head_sha=HEAD,
        current_run_id=91,
        qualification=qualification,
    )
    assert api.cancelled == []


def test_snapshot_change_revokes_stale_trigger_cancellation() -> None:
    qualification = PullRequestQualification(
        head_sha=HEAD,
        integration_capable=True,
    )
    api = FakeApi(
        PullRequestQualification(
            head_sha=STALE_HEAD,
            integration_capable=True,
        )
    )

    assert not _cancel_triggering_run_if_stale_or_nonqualifying(
        api,  # type: ignore[arg-type]
        pr_number=2039,
        event_head_sha=STALE_HEAD,
        current_run_id=91,
        qualification=qualification,
    )
    assert api.cancelled == []


def test_current_run_snapshot_identity_requires_one_consistent_singleton() -> None:
    singleton = _run(91, HEAD, (2039,))
    assert _explicit_singleton_pr_for_current_run(
        (singleton,),
        workflow_name="CI",
        current_run_id=91,
        event_head_sha=HEAD,
    ) == 2039

    assert _explicit_singleton_pr_for_current_run(
        (singleton, _run(91, HEAD, (2039, 2040))),
        workflow_name="CI",
        current_run_id=91,
        event_head_sha=HEAD,
    ) is None
    assert _explicit_singleton_pr_for_current_run(
        (singleton, _run(91, HEAD, (2040,))),
        workflow_name="CI",
        current_run_id=91,
        event_head_sha=HEAD,
    ) is None
    assert _explicit_singleton_pr_for_current_run(
        (_run(92, HEAD, (2039,)),),
        workflow_name="CI",
        current_run_id=91,
        event_head_sha=HEAD,
    ) is None
    assert _explicit_singleton_pr_for_current_run(
        (_run(91, STALE_HEAD, (2039,)),),
        workflow_name="CI",
        current_run_id=91,
        event_head_sha=HEAD,
    ) is None


def test_event_pr_reference_mode_preserves_multi_reference_ambiguity() -> None:
    assert _validated_event_pr_identity(
        2039,
        reference_mode="singleton",
    ) == (2039, False)
    assert _validated_event_pr_identity(
        0,
        reference_mode="empty",
    ) == (None, False)
    assert _validated_event_pr_identity(
        0,
        reference_mode="ambiguous",
    ) == (None, True)

    for pr_number, mode in (
        (0, "singleton"),
        (2039, "empty"),
        (2039, "ambiguous"),
    ):
        with pytest.raises(CancellationError):
            _validated_event_pr_identity(
                pr_number,
                reference_mode=mode,
            )


def test_ambiguous_event_never_recovers_current_run_singleton_snapshot(
    monkeypatch,
) -> None:
    instances = []

    class FakeScopedApi:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            instances.append(self)

        def cancel_historical_unbound_runs(
            self,
            *,
            exclude_run_ids: tuple[int, ...],
        ) -> tuple[int, ...]:
            assert 91 in exclude_run_ids
            return ()

        def live_pr_qualification(self, pr_number: int):
            raise AssertionError(
                f"ambiguous event must not resolve trigger PR authority: {pr_number}"
            )

    monkeypatch.setattr(
        scoped_controller,
        "WorkflowScopedGitHubApi",
        FakeScopedApi,
    )
    monkeypatch.setattr(
        scoped_controller,
        "cancel_superseded_explicit_pr_runs",
        lambda *args, **kwargs: (),
    )
    monkeypatch.setattr(
        scoped_controller,
        "_explicit_singleton_pr_for_current_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError(
                "multi-reference event must not collapse to later singleton snapshot"
            )
        ),
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "Oleksii-debug/Autosport")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    assert scoped_controller.main(
        [
            "--pr-number",
            "0",
            "--event-pr-reference-mode",
            "ambiguous",
            "--event-head-sha",
            HEAD,
            "--workflow-name",
            "CI",
            "--workflow-id",
            "356678400",
            "--current-run-id",
            "91",
        ]
    ) == 0
    assert len(instances) == 1


def test_later_explicit_observation_revokes_stale_unbound_candidate(monkeypatch) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )

    def run_payload(*, status: str, pr_numbers: tuple[int, ...]) -> dict[str, object]:
        return {
            "id": 7007,
            "head_sha": HEAD,
            "name": "CI",
            "status": status,
            "pull_requests": [{"number": number} for number in pr_numbers],
            "head_branch": "feature/head",
            "head_repository": {"full_name": "owner/repo"},
        }

    def fake_request(
        path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ) -> object:
        assert method == "GET"
        assert not allowed_http_errors
        if "status=queued" in path:
            return {
                "total_count": 1,
                "workflow_runs": [run_payload(status="queued", pr_numbers=())],
            }
        if "status=in_progress" in path:
            return {
                "total_count": 1,
                "workflow_runs": [
                    run_payload(status="in_progress", pr_numbers=(2050,))
                ],
            }
        return {"total_count": 0, "workflow_runs": []}

    monkeypatch.setattr(api, "_request", fake_request)

    runs = api.active_runs()

    assert tuple(
        run.pr_numbers for run in runs if run.run_id == 7007
    ) == ((), (2050,))
    assert 7007 not in api._unbound_active_runs

    def stale_orphan_lookup(_head_sha: str) -> int:
        raise AssertionError("later explicit identity must revoke stale orphan cleanup")

    monkeypatch.setattr(
        api,
        "_historical_associated_pr_number",
        stale_orphan_lookup,
    )
    assert api.cancel_historical_unbound_runs() == ()


def test_later_unbound_observation_cannot_reopen_explicit_orphan_authority(monkeypatch) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )

    def run_payload(*, status: str, pr_numbers: tuple[int, ...]) -> dict[str, object]:
        return {
            "id": 7008,
            "head_sha": HEAD,
            "name": "CI",
            "status": status,
            "pull_requests": [{"number": number} for number in pr_numbers],
            "head_branch": "feature/head",
            "head_repository": {"full_name": "owner/repo"},
        }

    def fake_request(
        path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ) -> object:
        assert method == "GET"
        assert not allowed_http_errors
        if "status=queued" in path:
            return {
                "total_count": 1,
                "workflow_runs": [
                    run_payload(status="queued", pr_numbers=(2050,))
                ],
            }
        if "status=in_progress" in path:
            return {
                "total_count": 1,
                "workflow_runs": [run_payload(status="in_progress", pr_numbers=())],
            }
        return {"total_count": 0, "workflow_runs": []}

    monkeypatch.setattr(api, "_request", fake_request)

    runs = api.active_runs()

    assert tuple(
        run.pr_numbers for run in runs if run.run_id == 7008
    ) == ((2050,), ())
    assert 7008 not in api._unbound_active_runs

    def stale_orphan_lookup(_head_sha: str) -> int:
        raise AssertionError("later unbound metadata must not reopen orphan cleanup")

    monkeypatch.setattr(
        api,
        "_historical_associated_pr_number",
        stale_orphan_lookup,
    )
    assert api.cancel_historical_unbound_runs() == ()


def test_conflicting_unbound_observations_defer_orphan_cleanup(monkeypatch) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )

    def run_payload(*, status: str, head_sha: str) -> dict[str, object]:
        return {
            "id": 7009,
            "head_sha": head_sha,
            "name": "CI",
            "status": status,
            "pull_requests": [],
            "head_branch": "feature/head",
            "head_repository": {"full_name": "owner/repo"},
        }

    def fake_request(
        path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ) -> object:
        assert method == "GET"
        assert not allowed_http_errors
        if "status=queued" in path:
            return {
                "total_count": 1,
                "workflow_runs": [run_payload(status="queued", head_sha=HEAD)],
            }
        if "status=in_progress" in path:
            return {
                "total_count": 1,
                "workflow_runs": [
                    run_payload(status="in_progress", head_sha=STALE_HEAD)
                ],
            }
        return {"total_count": 0, "workflow_runs": []}

    monkeypatch.setattr(api, "_request", fake_request)

    api.active_runs()

    assert 7009 not in api._unbound_active_runs
    assert 7009 in api._conflicted_unbound_run_ids

    def stale_orphan_lookup(_head_sha: str) -> int:
        raise AssertionError("conflicting unbound identity must defer orphan cleanup")

    monkeypatch.setattr(
        api,
        "_historical_associated_pr_number",
        stale_orphan_lookup,
    )
    assert api.cancel_historical_unbound_runs() == ()


def test_repeated_active_run_scan_resets_snapshot_local_orphan_state(monkeypatch) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    phase = {"value": 0}

    def fake_request(
        path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ) -> object:
        assert method == "GET"
        assert not allowed_http_errors
        if phase["value"] == 0 and "status=queued" in path:
            return {
                "total_count": 1,
                "workflow_runs": [
                    {
                        "id": 7010,
                        "head_sha": HEAD,
                        "name": "CI",
                        "status": "queued",
                        "pull_requests": [],
                        "head_branch": "feature/head",
                        "head_repository": {"full_name": "owner/repo"},
                    }
                ],
            }
        return {"total_count": 0, "workflow_runs": []}

    monkeypatch.setattr(api, "_request", fake_request)

    api.active_runs()
    assert 7010 in api._unbound_active_runs

    phase["value"] = 1
    assert api.active_runs() == ()
    assert api._unbound_active_runs == {}
    assert api._explicit_active_run_ids == set()
    assert api._conflicted_unbound_run_ids == set()


def test_orphan_recovery_active_cancel_conflict_clears_temporary_authority() -> None:
    class OrphanConflictApi:
        def __init__(self) -> None:
            self._unbound_active_runs = {
                7011: (STALE_HEAD, "feature/stale"),
            }
            self._recovered_runs: dict[int, tuple[int, str]] = {}
            self._zero_association_recovered_runs: dict[int, tuple[str, str]] = {}

        def _historical_associated_pr_number(self, head_sha: str) -> int:
            assert head_sha == STALE_HEAD
            return 303

        def live_pr_qualification(
            self,
            pr_number: int,
        ) -> PullRequestQualification:
            assert pr_number == 303
            return PullRequestQualification(
                head_sha=HEAD,
                integration_capable=True,
            )

        def cancel(self, run_id: int) -> None:
            assert run_id == 7011
            raise CancellationError(
                "workflow run cancellation conflicted while run remains active"
            )

    api = OrphanConflictApi()

    assert WorkflowScopedGitHubApi.cancel_historical_unbound_runs(
        api,  # type: ignore[arg-type]
    ) == ()
    assert api._recovered_runs == {}
    assert api._zero_association_recovered_runs == {}


def test_explicit_run_boundary_requires_exact_workflow_head_and_singleton(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    payload: dict[str, object] = {
        "id": 7012,
        "workflow_id": 356678400,
        "event": "pull_request",
        "head_sha": HEAD,
        "name": "CI",
        "status": "queued",
        "pull_requests": [{"number": 303}],
    }

    monkeypatch.setattr(api, "_request", lambda _path: payload)

    assert api._explicit_run_identity_matches(
        run_id=7012,
        expected_head_sha=HEAD,
        pr_number=303,
    )

    payload["pull_requests"] = [{"number": 303}, {"number": 304}]
    assert not api._explicit_run_identity_matches(
        run_id=7012,
        expected_head_sha=HEAD,
        pr_number=303,
    )

    payload["pull_requests"] = [{"number": 303}]
    payload["head_sha"] = STALE_HEAD
    assert not api._explicit_run_identity_matches(
        run_id=7012,
        expected_head_sha=HEAD,
        pr_number=303,
    )

    payload["head_sha"] = HEAD
    payload["workflow_id"] = 356678489
    assert not api._explicit_run_identity_matches(
        run_id=7012,
        expected_head_sha=HEAD,
        pr_number=303,
    )


def test_explicit_run_boundary_checker_rejects_instance_dispatch_shadow(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    monkeypatch.setattr(
        api,
        "_explicit_run_identity_matches",
        lambda **_kwargs: True,
    )

    assert not _explicit_run_identity_is_current(
        api,
        run_id=7012,
        expected_head_sha=HEAD,
        pr_number=303,
    )


def test_explicit_run_boundary_checker_rejects_transitive_request_shadow(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    invoked = {"value": False}

    def forged_request(_path: str, **_kwargs):
        invoked["value"] = True
        return {
            "id": 7012,
            "workflow_id": 356678400,
            "event": "pull_request",
            "head_sha": HEAD,
            "name": "CI",
            "status": "queued",
            "pull_requests": [{"number": 303}],
        }

    monkeypatch.setattr(api, "_request", forged_request)

    assert not _explicit_run_identity_is_current(
        api,
        run_id=7012,
        expected_head_sha=HEAD,
        pr_number=303,
    )
    assert not invoked["value"]


def test_explicit_run_boundary_checker_rejects_class_request_rebind(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )

    def forged_request(self, _path: str, **_kwargs):
        raise AssertionError("class-rebound request transport must not execute")

    monkeypatch.setattr(WorkflowScopedGitHubApi, "_request", forged_request)

    assert not _explicit_run_identity_is_current(
        api,
        run_id=7012,
        expected_head_sha=HEAD,
        pr_number=303,
    )


def test_explicit_run_boundary_checker_rejects_in_place_request_code_mutation(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )

    def forged_request(
        self,
        _path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ):
        return {
            "id": 7012,
            "workflow_id": 356678400,
            "event": "pull_request",
            "head_sha": HEAD,
            "name": "CI",
            "status": "queued",
            "pull_requests": [{"number": 303}],
        }

    monkeypatch.setattr(
        scoped_controller.GitHubApi._request,
        "__code__",
        forged_request.__code__,
    )

    assert not _explicit_run_identity_is_current(
        api,
        run_id=7012,
        expected_head_sha=HEAD,
        pr_number=303,
    )


def test_explicit_run_boundary_checker_rejects_request_code_mutation_during_get(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    canonical_request = scoped_controller.GitHubApi._request

    def forged_request(
        self,
        _path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ):
        raise AssertionError("mutated request executable must not gain authority")

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            return (
                b'{"id":7012,"workflow_id":356678400,'
                b'"event":"pull_request","head_sha":"'
                + HEAD.encode("ascii")
                + b'","name":"CI","status":"queued",'
                b'"pull_requests":[{"number":303}]}'
            )

    def mutating_urlopen(_request, *, timeout: int):
        assert timeout == 20
        monkeypatch.setattr(canonical_request, "__code__", forged_request.__code__)
        return FakeResponse()

    # _request is defined in the canonical sibling module, so mutate its urlopen
    # global through the captured function's actual globals.
    monkeypatch.setitem(canonical_request.__globals__, "urlopen", mutating_urlopen)

    assert not _explicit_run_identity_is_current(
        api,
        run_id=7012,
        expected_head_sha=HEAD,
        pr_number=303,
    )


def test_live_pr_boundary_rejects_transitive_request_shadow(monkeypatch) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    invoked = {"value": False}

    def forged_request(_path: str, **_kwargs):
        invoked["value"] = True
        return {
            "number": 303,
            "state": "open",
            "draft": False,
            "head": {"sha": HEAD},
        }

    monkeypatch.setattr(api, "_request", forged_request)

    with pytest.raises(CancellationError, match="live PR qualification dispatch changed"):
        _trusted_live_pr_qualification(api, 303)
    assert not invoked["value"]


def test_controller_scheduler_coalesces_all_prs_per_source_workflow() -> None:
    text = Path(".github/workflows/pr-qualification-supersession.yml").read_text(
        encoding="utf-8"
    )
    concurrency = text.split("concurrency:", 1)[1].split("jobs:", 1)[0]

    assert "github.event.workflow_run.workflow_id" in concurrency
    assert "github.event.workflow_run.pull_requests" not in concurrency
    assert "github.event.workflow_run.head_sha" not in concurrency
    assert "github.event.workflow_run.event == 'pull_request'" in concurrency
    assert "format('non-pr-{0}', github.event.workflow_run.id)" in concurrency
    assert "cancel-in-progress: false" in concurrency
    assert "--event-pr-reference-mode" in text
    assert "pull_requests[1].number && 'ambiguous'" in text
    assert "pull_requests[0].number && 'singleton' || 'empty'" in text
    assert text.index("pull_requests[1].number && 'ambiguous'") < text.index(
        "pull_requests[0].number && 'singleton' || 'empty'"
    )


class SweepApi:
    def __init__(
        self,
        runs: tuple[WorkflowRun, ...],
        qualifications: dict[int, list[PullRequestQualification]],
    ) -> None:
        self._workflow_name = "CI"
        self._runs = runs
        self._qualifications = {
            pr_number: list(values)
            for pr_number, values in qualifications.items()
        }
        self.cancelled: list[int] = []
        self.reads: list[int] = []
        self.identity_results: dict[int, bool] = {}
        self.identity_reads: list[tuple[int, str, int]] = []

    def active_runs(self) -> tuple[WorkflowRun, ...]:
        return self._runs

    def _explicit_run_identity_matches(
        self,
        *,
        run_id: int,
        expected_head_sha: str,
        pr_number: int,
    ) -> bool:
        self.identity_reads.append((run_id, expected_head_sha, pr_number))
        return self.identity_results.get(run_id, True)

    def live_pr_qualification(self, pr_number: int) -> PullRequestQualification:
        self.reads.append(pr_number)
        values = self._qualifications[pr_number]
        if len(values) > 1:
            return values.pop(0)
        return values[0]

    def cancel(self, run_id: int) -> None:
        self.cancelled.append(run_id)


def _run(
    run_id: int,
    head_sha: str,
    pr_numbers: tuple[int, ...],
) -> WorkflowRun:
    return WorkflowRun(
        run_id=run_id,
        head_sha=head_sha,
        workflow_name="CI",
        pr_numbers=pr_numbers,
        status="queued",
    )


def test_workflow_wide_sweep_cancels_stale_runs_for_multiple_ready_prs() -> None:
    head_one = "1" * 40
    head_two = "2" * 40
    stale_one = "3" * 40
    stale_two = "4" * 40
    one = PullRequestQualification(head_sha=head_one, integration_capable=True)
    two = PullRequestQualification(head_sha=head_two, integration_capable=True)
    api = SweepApi(
        (
            _run(10, stale_one, (101,)),
            _run(11, head_one, (101,)),
            _run(20, stale_two, (202,)),
            _run(21, head_two, (202,)),
        ),
        {
            101: [one, one],
            202: [two, two],
        },
    )

    assert cancel_superseded_explicit_pr_runs(
        api,  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=11,
    ) == (10, 20)
    assert api.cancelled == [10, 20]


def test_workflow_wide_sweep_active_cancel_conflict_does_not_starve_other_prs() -> None:
    head_one = "1" * 40
    head_two = "2" * 40
    one = PullRequestQualification(head_sha=head_one, integration_capable=True)
    two = PullRequestQualification(head_sha=head_two, integration_capable=True)

    class ConflictSweepApi(SweepApi):
        def cancel(self, run_id: int) -> None:
            if run_id == 10:
                raise CancellationError(
                    "workflow run cancellation conflicted while run remains active"
                )
            super().cancel(run_id)

    api = ConflictSweepApi(
        (
            _run(10, "3" * 40, (101,)),
            _run(20, "4" * 40, (202,)),
        ),
        {
            101: [one, one],
            202: [two, two],
        },
    )

    assert cancel_superseded_explicit_pr_runs(
        api,  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=99,
    ) == (20,)
    assert api.cancelled == [20]
    assert api.reads == [101, 101, 202, 202]


def test_boundary_run_identity_change_revokes_only_that_sweep_candidate() -> None:
    one = PullRequestQualification(
        head_sha="1" * 40,
        integration_capable=True,
    )
    two = PullRequestQualification(
        head_sha="2" * 40,
        integration_capable=True,
    )
    api = SweepApi(
        (
            _run(10, "3" * 40, (101,)),
            _run(20, "4" * 40, (202,)),
        ),
        {
            101: [one],
            202: [two, two],
        },
    )
    api.identity_results[10] = False

    assert cancel_superseded_explicit_pr_runs(
        api,  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=99,
    ) == (20,)
    assert api.cancelled == [20]
    assert api.identity_reads == [
        (10, "3" * 40, 101),
        (20, "4" * 40, 202),
    ]
    # The revoked run never consumes a final PR-qualification reread. The independent
    # second group still receives both its initial and immediate pre-POST checks.
    assert api.reads == [101, 202, 202]


def test_boundary_run_identity_read_failure_does_not_starve_other_pr_group() -> None:
    one = PullRequestQualification(
        head_sha="1" * 40,
        integration_capable=True,
    )
    two = PullRequestQualification(
        head_sha="2" * 40,
        integration_capable=True,
    )

    class IdentityReadFailureApi(SweepApi):
        def _explicit_run_identity_matches(
            self,
            *,
            run_id: int,
            expected_head_sha: str,
            pr_number: int,
        ) -> bool:
            self.identity_reads.append((run_id, expected_head_sha, pr_number))
            if run_id == 10:
                raise CancellationError("fixture run identity reread unavailable")
            return True

    api = IdentityReadFailureApi(
        (
            _run(10, "3" * 40, (101,)),
            _run(20, "4" * 40, (202,)),
        ),
        {
            101: [one],
            202: [two, two],
        },
    )

    assert cancel_superseded_explicit_pr_runs(
        api,  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=99,
    ) == (20,)
    assert api.cancelled == [20]
    assert api.identity_reads == [
        (10, "3" * 40, 101),
        (20, "4" * 40, 202),
    ]
    assert api.reads == [101, 202, 202]


def test_current_trigger_only_group_needs_no_sweep_qualification_read() -> None:
    api = SweepApi(
        (_run(29, "5" * 40, (303,)),),
        {},
    )

    assert cancel_superseded_explicit_pr_runs(
        api,  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=29,
    ) == ()
    assert api.cancelled == []
    assert api.reads == []


def test_workflow_wide_sweep_cancels_same_head_for_nonqualifying_pr() -> None:
    head = "5" * 40
    qualification = PullRequestQualification(
        head_sha=head,
        integration_capable=False,
    )
    api = SweepApi(
        (_run(30, head, (303,)),),
        {303: [qualification, qualification]},
    )

    assert cancel_superseded_explicit_pr_runs(
        api,  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=99,
    ) == (30,)
    assert api.cancelled == [30]


def test_workflow_wide_sweep_does_not_infer_pr_from_ambiguous_run() -> None:
    api = SweepApi(
        (_run(40, "6" * 40, (401, 402)),),
        {},
    )

    assert cancel_superseded_explicit_pr_runs(
        api,  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=99,
    ) == ()
    assert api.cancelled == []
    assert api.reads == []


def test_workflow_wide_sweep_never_cancels_multi_reference_run_via_singleton_pr() -> None:
    qualification = PullRequestQualification(
        head_sha="7" * 40,
        integration_capable=True,
    )
    api = SweepApi(
        (
            _run(41, "8" * 40, (401,)),
            _run(42, "9" * 40, (401, 402)),
        ),
        {401: [qualification, qualification]},
    )

    assert cancel_superseded_explicit_pr_runs(
        api,  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=99,
    ) == (41,)
    assert api.cancelled == [41]
    assert api.reads == [401, 401]


@pytest.mark.parametrize(
    "observations",
    [
        (
            _run(49, "8" * 40, (401,)),
            _run(49, "8" * 40, (401, 402)),
        ),
        (
            _run(49, "8" * 40, (401,)),
            _run(49, "8" * 40, (402,)),
        ),
        (
            _run(49, "8" * 40, ()),
            _run(49, "8" * 40, (401,)),
        ),
        (
            _run(49, "8" * 40, (401,)),
            _run(49, "9" * 40, (401,)),
        ),
    ],
)
def test_conflicting_same_run_observations_never_authorize_sweep_cancel(
    observations: tuple[WorkflowRun, WorkflowRun],
) -> None:
    one = PullRequestQualification(
        head_sha="7" * 40,
        integration_capable=True,
    )
    two = PullRequestQualification(
        head_sha="6" * 40,
        integration_capable=True,
    )
    api = SweepApi(
        observations,
        {
            401: [one, one],
            402: [two, two],
        },
    )

    assert cancel_superseded_explicit_pr_runs(
        api,  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=99,
    ) == ()
    assert api.cancelled == []


def test_consistent_duplicate_run_observations_authorize_only_one_cancel() -> None:
    qualification = PullRequestQualification(
        head_sha="7" * 40,
        integration_capable=True,
    )
    api = SweepApi(
        (
            _run(49, "8" * 40, (401,)),
            _run(49, "8" * 40, (401,)),
        ),
        {401: [qualification, qualification]},
    )

    assert cancel_superseded_explicit_pr_runs(
        api,  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=99,
    ) == (49,)
    assert api.cancelled == [49]


def test_one_pr_authority_move_does_not_block_other_pr_group() -> None:
    one_initial = PullRequestQualification(
        head_sha="7" * 40,
        integration_capable=True,
    )
    one_moved = PullRequestQualification(
        head_sha="8" * 40,
        integration_capable=True,
    )
    two = PullRequestQualification(
        head_sha="9" * 40,
        integration_capable=True,
    )
    api = SweepApi(
        (
            _run(50, "a" * 40, (501,)),
            _run(60, "b" * 40, (601,)),
        ),
        {
            501: [one_initial, one_moved],
            601: [two, two],
        },
    )

    assert cancel_superseded_explicit_pr_runs(
        api,  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=99,
    ) == (60,)
    assert api.cancelled == [60]


def test_one_pr_initial_qualification_failure_does_not_block_other_group() -> None:
    two = PullRequestQualification(
        head_sha="9" * 40,
        integration_capable=True,
    )

    class InitialFailureApi(SweepApi):
        def live_pr_qualification(self, pr_number: int) -> PullRequestQualification:
            self.reads.append(pr_number)
            if pr_number == 501:
                raise CancellationError("fixture qualification unavailable")
            return self._qualifications[pr_number][0]

    api = InitialFailureApi(
        (
            _run(50, "a" * 40, (501,)),
            _run(60, "b" * 40, (601,)),
        ),
        {601: [two]},
    )

    assert cancel_superseded_explicit_pr_runs(
        api,  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=99,
    ) == (60,)
    assert api.cancelled == [60]
    assert api.reads == [501, 601, 601]


def test_one_pr_qualification_reread_failure_does_not_block_other_group() -> None:
    one = PullRequestQualification(
        head_sha="7" * 40,
        integration_capable=True,
    )
    two = PullRequestQualification(
        head_sha="9" * 40,
        integration_capable=True,
    )

    class RereadFailureApi(SweepApi):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._read_counts: dict[int, int] = {}

        def live_pr_qualification(self, pr_number: int) -> PullRequestQualification:
            self.reads.append(pr_number)
            count = self._read_counts.get(pr_number, 0)
            self._read_counts[pr_number] = count + 1
            if pr_number == 501 and count == 1:
                raise CancellationError("fixture qualification reread unavailable")
            return self._qualifications[pr_number][0]

    api = RereadFailureApi(
        (
            _run(50, "a" * 40, (501,)),
            _run(60, "b" * 40, (601,)),
        ),
        {
            501: [one],
            601: [two],
        },
    )

    assert cancel_superseded_explicit_pr_runs(
        api,  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=99,
    ) == (60,)
    assert api.cancelled == [60]
    assert api.reads == [501, 501, 601, 601]


def test_controller_main_uses_workflow_wide_sweep_and_trigger_boundary() -> None:
    text = Path("scripts/cancel_superseded_pr_workflow_runs_scoped.py").read_text(
        encoding="utf-8"
    )
    assert "cancel_superseded_explicit_pr_runs(" in text
    assert "runs = api.active_runs()" in text
    assert text.index("sweep_cancelled = cancel_superseded_explicit_pr_runs(") < text.index(
        "trigger_qualification = _trusted_live_pr_qualification("
    )
    assert "_cancel_triggering_run_if_stale_or_nonqualifying(" in text

def test_workflow_wide_sweep_preserves_sealed_scoped_cancel_boundary() -> None:
    text = Path("scripts/cancel_superseded_pr_workflow_runs_scoped.py").read_text(
        encoding="utf-8"
    )

    assert "cancel = _build_cancel(" in text
    assert "canonical base cancellation authority changed" in text
    assert "scoped cancellation revalidation dispatch changed" in text
    assert "base_cancel(self, run_id)" in text
    assert "_trusted_live_pr_qualification(" in text
    sweep = text.split("def cancel_superseded_explicit_pr_runs(", 1)[1].split(
        "def _cancel_triggering_run_if_stale_or_nonqualifying(", 1
    )[0]
    assert sweep.index("current_qualification = _trusted_live_pr_qualification(") < sweep.index(
        "_cancel_run_or_defer_active_conflict(api, run_id)"
    )
