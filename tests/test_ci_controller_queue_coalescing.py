from __future__ import annotations

from pathlib import Path
import json

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


def _scoped_main_args() -> list[str]:
    return [
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


def test_scoped_main_canonical_production_graph_reaches_argument_validation(
    monkeypatch,
    capsys,
) -> None:
    assert (
        scoped_controller.WorkflowScopedGitHubApi.__dict__["cancel"]
        is not scoped_controller.GitHubApi.__dict__["cancel"]
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    args = _scoped_main_args()
    args[args.index("--event-head-sha") + 1] = "not-a-sha"

    assert scoped_controller.main(args) == 2
    error = capsys.readouterr().err
    assert "invalid event head sha" in error
    assert "controller orchestration authority changed" not in error


def test_main_rejects_preentry_scoped_api_rebind(monkeypatch) -> None:
    forged_calls: list[str] = []

    class ForgedScopedApi:
        def __init__(self, **_kwargs) -> None:
            forged_calls.append("init")

    monkeypatch.setattr(
        scoped_controller,
        "WorkflowScopedGitHubApi",
        ForgedScopedApi,
    )

    assert scoped_controller.main(_scoped_main_args()) == 2
    assert forged_calls == []


def test_main_rejects_preentry_sweep_dispatch_rebind(monkeypatch) -> None:
    forged_calls: list[str] = []

    def forged_sweep(*_args, **_kwargs) -> tuple[int, ...]:
        forged_calls.append("sweep")
        return ()

    monkeypatch.setattr(
        scoped_controller,
        "cancel_superseded_explicit_pr_runs",
        forged_sweep,
    )

    assert scoped_controller.main(_scoped_main_args()) == 2
    assert forged_calls == []


def test_main_rejects_preentry_event_identity_rebind(monkeypatch) -> None:
    forged_calls: list[str] = []

    def forged_identity(*_args, **_kwargs) -> tuple[int, bool]:
        forged_calls.append("identity")
        return (2039, False)

    monkeypatch.setattr(
        scoped_controller,
        "_validated_event_pr_identity",
        forged_identity,
    )

    assert scoped_controller.main(_scoped_main_args()) == 2
    assert forged_calls == []


def test_main_rejects_preentry_active_run_method_rebind(monkeypatch) -> None:
    forged_calls: list[str] = []

    def forged_active_runs(_self) -> tuple[WorkflowRun, ...]:
        forged_calls.append("active")
        return ()

    monkeypatch.setattr(
        WorkflowScopedGitHubApi,
        "active_runs",
        forged_active_runs,
    )

    assert scoped_controller.main(_scoped_main_args()) == 2
    assert forged_calls == []


def test_active_runs_rejects_preentry_status_reader_rebind(monkeypatch) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    forged_calls: list[str] = []

    def forged_status_reader(_self, status: str) -> tuple[WorkflowRun, ...]:
        forged_calls.append(status)
        return ()

    monkeypatch.setattr(
        WorkflowScopedGitHubApi,
        "_active_runs_for_status",
        forged_status_reader,
    )

    with pytest.raises(
        CancellationError,
        match="active workflow reader authority changed",
    ):
        api.active_runs()
    assert forged_calls == []


def test_active_runs_rejects_preentry_recovery_reader_rebind(monkeypatch) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    forged_calls: list[int] = []

    def forged_recovery(_self, run: WorkflowRun) -> WorkflowRun:
        forged_calls.append(run.run_id)
        return run

    monkeypatch.setattr(
        WorkflowScopedGitHubApi,
        "_recover_candidate_run_reference",
        forged_recovery,
    )

    with pytest.raises(
        CancellationError,
        match="active workflow reader authority changed",
    ):
        api.active_runs()
    assert forged_calls == []


def test_active_runs_rejects_status_reader_kwdefaults_rebase(monkeypatch) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    status_reader = WorkflowScopedGitHubApi._active_runs_for_status
    defaults = status_reader.__kwdefaults__
    assert defaults is not None
    forged_calls: list[str] = []

    def forged_parser(_payload) -> WorkflowRun:
        forged_calls.append("parser")
        return WorkflowRun(
            run_id=999,
            head_sha=STALE_HEAD,
            workflow_name="CI",
            pr_numbers=(303,),
            status="queued",
        )

    monkeypatch.setitem(defaults, "_run_parser", forged_parser)
    monkeypatch.setitem(defaults, "_run_parser_code", forged_parser.__code__)

    with pytest.raises(
        CancellationError,
        match="active workflow reader authority changed",
    ):
        api.active_runs()
    assert forged_calls == []


def test_active_runs_rejects_inflight_status_reader_rebind(monkeypatch) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    canonical_status_reader = WorkflowScopedGitHubApi._active_runs_for_status
    forged_calls: list[str] = []

    def forged_status_reader(_self, status: str) -> tuple[WorkflowRun, ...]:
        forged_calls.append(status)
        return ()

    def fake_request(
        path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ) -> object:
        assert method == "GET"
        assert not allowed_http_errors
        assert "status=queued" in path
        monkeypatch.setattr(
            WorkflowScopedGitHubApi,
            "_active_runs_for_status",
            forged_status_reader,
        )
        return {"total_count": 0, "workflow_runs": []}

    monkeypatch.setattr(api, "_request", fake_request)

    with pytest.raises(
        CancellationError,
        match="active workflow reader authority changed",
    ):
        api.active_runs()
    assert forged_calls == []
    assert canonical_status_reader is not forged_status_reader


def test_active_runs_public_entrypoint_has_no_mutable_default_authority() -> None:
    assert WorkflowScopedGitHubApi.active_runs.__defaults__ is None
    assert WorkflowScopedGitHubApi.active_runs.__kwdefaults__ is None


def test_cancel_rejects_historical_association_kwdefaults_rebase(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    helper = WorkflowScopedGitHubApi._historical_associated_pr_number
    defaults = helper.__kwdefaults__
    assert defaults is not None
    forged_calls: list[object] = []

    def forged_encoder(params) -> str:
        forged_calls.append(params)
        return "per_page=100&page=2"

    monkeypatch.setitem(defaults, "_encode_query", forged_encoder)
    monkeypatch.setitem(defaults, "_encode_query_code", forged_encoder.__code__)

    with pytest.raises(
        CancellationError,
        match="scoped cancellation revalidation dispatch changed",
    ):
        api.cancel(7012)
    assert forged_calls == []


def test_cancel_rejects_canonical_branch_kwdefaults_rebase(monkeypatch) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    helper = WorkflowScopedGitHubApi._canonical_branch_head
    defaults = helper.__kwdefaults__
    assert defaults is not None
    forged_calls: list[str] = []

    def forged_quote(branch: str, *, safe: str = "/") -> str:
        forged_calls.append(branch)
        return "attacker%2Fbranch"

    monkeypatch.setitem(defaults, "_encode_branch", forged_quote)
    monkeypatch.setitem(defaults, "_encode_branch_code", forged_quote.__code__)

    with pytest.raises(
        CancellationError,
        match="scoped cancellation revalidation dispatch changed",
    ):
        api.cancel(7012)
    assert forged_calls == []


def test_main_rejects_preentry_orphan_method_rebind(monkeypatch) -> None:
    forged_calls: list[str] = []

    def forged_orphan(_self, **_kwargs) -> tuple[int, ...]:
        forged_calls.append("orphan")
        return ()

    monkeypatch.setattr(
        WorkflowScopedGitHubApi,
        "cancel_historical_unbound_runs",
        forged_orphan,
    )

    assert scoped_controller.main(_scoped_main_args()) == 2
    assert forged_calls == []


def test_main_rejects_preentry_orphan_effect_rebind(
    monkeypatch,
    capsys,
) -> None:
    forged_calls: list[int] = []

    def forged_effect(_api, run_id: int) -> bool:
        forged_calls.append(run_id)
        return True

    monkeypatch.setattr(
        scoped_controller,
        "_cancel_run_or_defer_active_conflict",
        forged_effect,
    )
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    assert scoped_controller.main(_scoped_main_args()) == 2
    assert "controller orchestration authority changed" in capsys.readouterr().err
    assert forged_calls == []


def test_main_rejects_preentry_inherited_request_shadow(monkeypatch) -> None:
    forged_calls: list[str] = []

    def forged_request(_self, path: str, **_kwargs) -> object:
        forged_calls.append(path)
        return {}

    monkeypatch.setattr(
        WorkflowScopedGitHubApi,
        "_request",
        forged_request,
    )

    assert scoped_controller.main(_scoped_main_args()) == 2
    assert forged_calls == []


def test_main_rejects_preentry_inherited_cancel_shadow(monkeypatch) -> None:
    forged_calls: list[int] = []

    def forged_cancel(_self, run_id: int) -> None:
        forged_calls.append(run_id)

    monkeypatch.setattr(
        WorkflowScopedGitHubApi,
        "cancel",
        forged_cancel,
    )

    assert scoped_controller.main(_scoped_main_args()) == 2
    assert forged_calls == []


def test_main_rejects_preentry_base_urlopen_rebind(monkeypatch) -> None:
    forged_calls: list[object] = []
    base_request_globals = scoped_controller.GitHubApi._request.__globals__

    def forged_urlopen(request: object, *, timeout: int) -> object:
        forged_calls.extend((request, timeout))
        raise AssertionError("forged transport must not execute")

    monkeypatch.setitem(
        base_request_globals,
        "urlopen",
        forged_urlopen,
    )

    assert scoped_controller.main(_scoped_main_args()) == 2
    assert forged_calls == []


def test_main_rejects_preentry_argument_parser_rebind_scoped(
    monkeypatch,
) -> None:
    forged_calls: list[str] = []

    class ForgedParser:
        def __init__(self, *_args, **_kwargs) -> None:
            forged_calls.append("parser")

    monkeypatch.setattr(
        scoped_controller.argparse,
        "ArgumentParser",
        ForgedParser,
    )

    assert scoped_controller.main(_scoped_main_args()) == 2
    assert forged_calls == []


def test_main_rejects_preentry_os_module_rebind_scoped(monkeypatch) -> None:
    forged_calls: list[str] = []

    class ForgedOs:
        @property
        def environ(self):
            forged_calls.append("environ")
            return {}

    monkeypatch.setattr(scoped_controller, "os", ForgedOs())

    assert scoped_controller.main(_scoped_main_args()) == 2
    assert forged_calls == []


def test_scoped_main_closure_owns_canonical_orphan_effect() -> None:
    closure = {
        name: cell.cell_contents
        for name, cell in zip(
            scoped_controller.main.__code__.co_freevars,
            scoped_controller.main.__closure__ or (),
        )
    }
    effect = scoped_controller._cancel_run_or_defer_active_conflict

    assert closure["orphan_effect_impl"] is effect
    assert closure["orphan_effect_code"] is effect.__code__


def test_main_rejects_preentry_sweep_code_mutation(monkeypatch) -> None:
    target = scoped_controller.cancel_superseded_explicit_pr_runs

    def forged_sweep(*_args, **_kwargs) -> tuple[int, ...]:
        return ()

    monkeypatch.setattr(target, "__code__", forged_sweep.__code__)

    assert scoped_controller.main(_scoped_main_args()) == 2

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


def test_repeated_active_run_scan_resets_snapshot_local_recovery_authority(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    # Recovery maps authorize a stronger final-boundary path than the raw moving
    # snapshot. They must never survive into a later enumeration generation.
    api._recovered_runs[7018] = (303, STALE_HEAD)
    api._zero_association_recovered_runs[7019] = (
        STALE_HEAD,
        "feature/stale",
    )

    def fake_request(
        path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ) -> object:
        assert method == "GET"
        assert not allowed_http_errors
        assert "/actions/workflows/356678400/runs?" in path
        return {"total_count": 0, "workflow_runs": []}

    monkeypatch.setattr(api, "_request", fake_request)

    assert api.active_runs() == ()
    assert api._recovered_runs == {}
    assert api._zero_association_recovered_runs == {}
    assert api._unbound_active_runs == {}
    assert api._explicit_active_run_ids == set()
    assert api._conflicted_unbound_run_ids == set()


def test_failed_active_run_scan_discards_partial_snapshot_authority(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )

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
                    {
                        "id": 7020,
                        "head_sha": STALE_HEAD,
                        "name": "CI",
                        "status": "queued",
                        "pull_requests": [],
                        "head_branch": "feature/stale",
                        "head_repository": {"full_name": "owner/repo"},
                    }
                ],
            }
        if "status=in_progress" in path:
            raise CancellationError("synthetic second-status failure")
        return {"total_count": 0, "workflow_runs": []}

    monkeypatch.setattr(api, "_request", fake_request)

    with pytest.raises(CancellationError, match="synthetic second-status failure"):
        api.active_runs()

    assert api._recovered_runs == {}
    assert api._zero_association_recovered_runs == {}
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


def test_orphan_recovery_cancel_effect_global_rebind_during_reread_cannot_redirect(
    monkeypatch,
) -> None:
    forged_calls: list[int] = []

    def forged_cancel(_api, run_id: int) -> bool:
        forged_calls.append(run_id)
        return True

    class OrphanRebindApi:
        def __init__(self) -> None:
            self._unbound_active_runs = {
                7013: (STALE_HEAD, "feature/stale"),
            }
            self._recovered_runs: dict[int, tuple[int, str]] = {}
            self._zero_association_recovered_runs: dict[int, tuple[str, str]] = {}
            self.cancelled: list[int] = []

        def _historical_associated_pr_number(self, head_sha: str) -> int:
            assert head_sha == STALE_HEAD
            return 303

        def live_pr_qualification(
            self,
            pr_number: int,
        ) -> PullRequestQualification:
            assert pr_number == 303
            monkeypatch.setattr(
                scoped_controller,
                "_cancel_run_or_defer_active_conflict",
                forged_cancel,
            )
            return PullRequestQualification(
                head_sha=HEAD,
                integration_capable=True,
            )

        def cancel(self, run_id: int) -> None:
            self.cancelled.append(run_id)

    api = OrphanRebindApi()

    assert WorkflowScopedGitHubApi.cancel_historical_unbound_runs(
        api,  # type: ignore[arg-type]
    ) == (7013,)
    assert api.cancelled == [7013]
    assert forged_calls == []
    assert api._recovered_runs == {}
    assert api._zero_association_recovered_runs == {}


def test_orphan_recovery_qualification_global_rebind_cannot_starve_cleanup(
    monkeypatch,
) -> None:
    forged_calls: list[int] = []

    def forged_qualification(_api, pr_number: int):
        forged_calls.append(pr_number)
        return (STALE_HEAD, True)

    class OrphanQualificationRebindApi:
        def __init__(self) -> None:
            self._unbound_active_runs = {
                7017: (STALE_HEAD, "feature/stale"),
            }
            self._recovered_runs: dict[int, tuple[int, str]] = {}
            self._zero_association_recovered_runs: dict[int, tuple[str, str]] = {}
            self.cancelled: list[int] = []

        def _historical_associated_pr_number(self, head_sha: str) -> int:
            assert head_sha == STALE_HEAD
            monkeypatch.setattr(
                scoped_controller,
                "_trusted_live_pr_qualification",
                forged_qualification,
            )
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
            self.cancelled.append(run_id)

    api = OrphanQualificationRebindApi()

    assert WorkflowScopedGitHubApi.cancel_historical_unbound_runs(
        api,  # type: ignore[arg-type]
    ) == (7017,)
    assert api.cancelled == [7017]
    assert forged_calls == []
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


def test_explicit_run_boundary_rejects_request_kwdefault_rebase_before_get(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    canonical_request = scoped_controller.GitHubApi._request
    defaults = canonical_request.__kwdefaults__
    assert defaults is not None
    invoked = {"value": False}

    def forbidden_urlopen(_request, *, timeout: int):
        assert timeout == 20
        invoked["value"] = True
        raise AssertionError("mutated request defaults must revoke identity read authority")

    monkeypatch.setitem(defaults, "_json_parse_int", str)
    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        forbidden_urlopen,
    )

    assert not _explicit_run_identity_is_current(
        api,
        run_id=7012,
        expected_head_sha=HEAD,
        pr_number=303,
    )
    assert not invoked["value"]


def test_live_pr_boundary_rejects_request_kwdefault_rebase_before_get(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    canonical_request = scoped_controller.GitHubApi._request
    defaults = canonical_request.__kwdefaults__
    assert defaults is not None
    invoked = {"value": False}

    def forbidden_urlopen(_request, *, timeout: int):
        assert timeout == 20
        invoked["value"] = True
        raise AssertionError(
            "mutated request defaults must revoke qualification read authority"
        )

    monkeypatch.setitem(defaults, "_json_parse_int", str)
    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        forbidden_urlopen,
    )

    with pytest.raises(CancellationError, match="live PR qualification dispatch changed"):
        _trusted_live_pr_qualification(api, 303)
    assert not invoked["value"]


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


def test_live_pr_boundary_bypasses_transient_nested_request_shadow(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    canonical_request = scoped_controller.GitHubApi._request
    canonical_pull_request = scoped_controller.GitHubApi._pull_request
    canonical_validator = canonical_pull_request.__globals__["_require_positive_int"]
    forged_invoked = {"value": False}

    def forged_request(
        _path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ):
        forged_invoked["value"] = True
        # Restore canonical instance dispatch before the outer reader performs its
        # post-read witness. The old nested helper path therefore cannot detect that
        # these forged bytes supplied its qualification.
        del api.__dict__["_request"]
        return {
            "state": "open",
            "draft": False,
            "head": {
                "sha": STALE_HEAD,
                "repo": {"full_name": "owner/repo"},
            },
            "base": {"repo": {"full_name": "owner/repo"}},
        }

    def arm_transient_shadow(value, *, field: str):
        result = canonical_validator(value, field=field)
        if field == "pull request number":
            api._request = forged_request
        return result

    monkeypatch.setitem(
        canonical_pull_request.__globals__,
        "_require_positive_int",
        arm_transient_shadow,
    )

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

    def canonical_urlopen(_request, *, timeout: int):
        assert timeout == 20
        return FakeResponse()

    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        canonical_urlopen,
    )

    assert _trusted_live_pr_qualification(api, 303) == (HEAD, True)
    assert not forged_invoked["value"]


def test_live_pr_boundary_coordinated_class_rebind_cannot_forge_head(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    canonical_request = scoped_controller.GitHubApi._request

    class ForgedQualification:
        def __init__(self, head_sha: str, integration_capable: bool) -> None:
            del head_sha, integration_capable
            self.head_sha = STALE_HEAD
            self.integration_capable = True

    forged_init = ForgedQualification.__dict__["__init__"]
    authority_globals = scoped_controller._pull_request_qualification_state.__globals__
    monkeypatch.setattr(
        scoped_controller,
        "PullRequestQualification",
        ForgedQualification,
        raising=False,
    )
    monkeypatch.setitem(
        authority_globals,
        "PullRequestQualification",
        ForgedQualification,
    )
    # Recreate every legacy mutable witness binding from the V5 falsifier.
    # The composition-time reader must ignore all of them.
    monkeypatch.setitem(
        authority_globals,
        "_PULL_REQUEST_QUALIFICATION_TYPE",
        ForgedQualification,
    )
    monkeypatch.setitem(
        authority_globals,
        "_PULL_REQUEST_QUALIFICATION_DICT_DESCRIPTOR",
        ForgedQualification.__dict__["__dict__"],
    )
    monkeypatch.setitem(
        authority_globals,
        "_PULL_REQUEST_QUALIFICATION_INIT",
        forged_init,
    )
    monkeypatch.setitem(
        authority_globals,
        "_PULL_REQUEST_QUALIFICATION_INIT_CODE",
        forged_init.__code__,
    )
    monkeypatch.setitem(
        authority_globals,
        "_PULL_REQUEST_QUALIFICATION_FIELD_CLASS_WITNESSES",
        tuple(
            (
                name,
                name in ForgedQualification.__dict__,
                ForgedQualification.__dict__.get(name),
            )
            for name in ("head_sha", "integration_capable")
        ),
    )

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

    def canonical_urlopen(_request, *, timeout: int):
        assert timeout == 20
        return FakeResponse()

    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        canonical_urlopen,
    )

    trusted = _trusted_live_pr_qualification(api, 303)
    assert trusted == (HEAD, True)
    assert scoped_controller._pull_request_qualification_state(trusted) == (
        HEAD,
        True,
    )


def test_live_pr_boundary_does_not_trust_rebound_positive_int_helper(
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

    def forged_positive_int(_value, *, field: str) -> int:
        del field
        return 999

    def canonical_urlopen(request, *, timeout: int):
        assert timeout == 20
        requested.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(
        scoped_controller,
        "_require_positive_int",
        forged_positive_int,
    )
    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        canonical_urlopen,
    )

    assert _trusted_live_pr_qualification(api, 303) == (HEAD, True)
    assert requested == ["https://api.github.com/repos/owner/repo/pulls/303"]


def test_live_pr_boundary_does_not_trust_rebound_sha_helper(
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
                + b'","repo":{"full_name":"owner/repo"}},'
                b'"base":{"repo":{"full_name":"owner/repo"}}}'
            )

    def forged_sha(_value, *, field: str) -> str:
        del field
        return STALE_HEAD

    def canonical_urlopen(_request, *, timeout: int):
        assert timeout == 20
        monkeypatch.setattr(scoped_controller, "_require_sha", forged_sha)
        return FakeResponse()

    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        canonical_urlopen,
    )

    assert _trusted_live_pr_qualification(api, 303) == (HEAD, True)


def test_controller_scheduler_coalesces_all_prs_per_source_workflow() -> None:
    text = Path(".github/workflows/pr-qualification-supersession.yml").read_text(
        encoding="utf-8"
    )
    concurrency = text.split("concurrency:", 1)[1].split("jobs:", 1)[0]

    assert "github.event.workflow_run.workflow_id" in concurrency
    assert "github.event.workflow_run.pull_requests" not in concurrency
    assert "github.event.workflow_run.head_sha" not in concurrency
    assert "github.event.workflow_run.event == 'pull_request'" in concurrency
    assert "format('non-pr-{0}', github.event.workflow_run.id)" not in concurrency
    assert "github.event.workflow_run.id" not in concurrency
    assert "'non-pr'" in concurrency
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


def test_rebound_selector_cannot_starve_canonical_pr_group(monkeypatch) -> None:
    qualification = PullRequestQualification(
        head_sha="7" * 40,
        integration_capable=True,
    )
    api = SweepApi(
        (
            _run(50, "8" * 40, (501,)),
            _run(51, "7" * 40, (501,)),
            _run(60, "9" * 40, (601,)),
        ),
        {501: [qualification]},
    )

    # A mutable module-global selector must neither widen nor silently starve this
    # PR group. Production sweep owns the composition-time canonical selector.
    monkeypatch.setattr(
        scoped_controller,
        "select_superseded_runs",
        lambda *_args, **_kwargs: (51,),
    )

    assert cancel_superseded_explicit_pr_runs(
        api,  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=51,
    ) == (50,)
    assert api.cancelled == [50]
    assert api.identity_reads == [(50, "8" * 40, 501)]


def test_rebound_selector_cannot_replace_canonical_group_with_another_pr(monkeypatch) -> None:
    qualification = PullRequestQualification(
        head_sha="7" * 40,
        integration_capable=True,
    )
    api = SweepApi(
        (
            _run(50, "8" * 40, (501,)),
            _run(60, "9" * 40, (601,)),
        ),
        {501: [qualification]},
    )

    monkeypatch.setattr(
        scoped_controller,
        "select_superseded_runs",
        lambda *_args, **_kwargs: (60,),
    )

    assert cancel_superseded_explicit_pr_runs(
        api,  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=99,
    ) == (50,)
    assert api.cancelled == [50]
    assert api.identity_reads == [(50, "8" * 40, 501)]


def test_sweep_rejects_in_place_selector_code_drift(monkeypatch) -> None:
    qualification = PullRequestQualification(
        head_sha="7" * 40,
        integration_capable=True,
    )
    api = SweepApi(
        (_run(50, "8" * 40, (501,)),),
        {501: [qualification]},
    )
    canonical_selector = scoped_controller.select_superseded_runs

    def forged_selector(*_args, **_kwargs):
        return ()

    monkeypatch.setattr(canonical_selector, "__code__", forged_selector.__code__)

    with pytest.raises(CancellationError, match="canonical decision authority changed"):
        cancel_superseded_explicit_pr_runs(
            api,  # type: ignore[arg-type]
            workflow_name="CI",
            current_run_id=99,
        )
    assert api.cancelled == []


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
    assert "sweep_impl = cancel_superseded_explicit_pr_runs" in text
    assert "runs = api.active_runs()" in text
    assert text.index("sweep_cancelled = sweep_impl(") < text.index(
        "trigger_qualification = trusted_qualification_impl("
    )
    assert "trigger_impl = _cancel_triggering_run_if_stale_or_nonqualifying" in text
    assert "trigger_impl(" in text

def test_workflow_wide_sweep_preserves_sealed_scoped_cancel_boundary() -> None:
    text = Path("scripts/cancel_superseded_pr_workflow_runs_scoped.py").read_text(
        encoding="utf-8"
    )

    assert "cancel = _build_cancel(" in text
    assert "canonical base cancellation authority changed" in text
    assert "scoped cancellation revalidation dispatch changed" in text
    assert "base_cancel(self, run_id)" in text
    assert "_production_qualification_reader=_trusted_live_pr_qualification" in text
    assert "_production_identity_checker=_explicit_run_identity_is_current" in text
    sweep = text.split("def cancel_superseded_explicit_pr_runs(", 1)[1].split(
        "def _cancel_triggering_run_if_stale_or_nonqualifying(", 1
    )[0]
    assert sweep.index("current_qualification = _qualification_reader(") < sweep.index(
        "_cancel_effect(api, run_id)"
    )
    assert "qualification = _qualification_reader(api, pr_number)" in sweep
    assert "if not _identity_checker(" in sweep
    assert "_cancel_effect_code=_cancel_run_or_defer_active_conflict.__code__" in sweep

def test_trigger_ready_tuple_cannot_be_relabelled_nonqualifying_by_adapter_rebind(monkeypatch) -> None:
    ready_head = "7" * 40
    forged_calls = {"state": 0, "cancel": 0}

    def forged_state(_qualification):
        forged_calls["state"] += 1
        return (ready_head, False)

    monkeypatch.setattr(
        scoped_controller,
        "_pull_request_qualification_state",
        forged_state,
    )
    monkeypatch.setattr(
        scoped_controller,
        "_explicit_run_identity_is_current",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        scoped_controller,
        "_trusted_live_pr_qualification",
        lambda *_args, **_kwargs: (ready_head, True),
    )

    def forged_cancel(_api, _run_id):
        forged_calls["cancel"] += 1
        return True

    monkeypatch.setattr(
        scoped_controller,
        "_cancel_run_or_defer_active_conflict",
        forged_cancel,
    )

    api = SweepApi((), {})
    assert not _cancel_triggering_run_if_stale_or_nonqualifying(
        api,  # type: ignore[arg-type]
        pr_number=303,
        event_head_sha=ready_head,
        current_run_id=7012,
        qualification=(ready_head, True),
    )
    assert forged_calls == {"state": 0, "cancel": 0}


def test_sweep_cancel_effect_global_rebind_during_final_reread_cannot_redirect(
    monkeypatch,
) -> None:
    current_head = "6" * 40
    stale_head = "5" * 40
    qualification = PullRequestQualification(
        head_sha=current_head,
        integration_capable=True,
    )
    forged_calls: list[int] = []

    def forged_cancel(_api, run_id: int) -> bool:
        forged_calls.append(run_id)
        return True

    class RebindingSweepApi(SweepApi):
        def live_pr_qualification(self, pr_number: int) -> PullRequestQualification:
            result = super().live_pr_qualification(pr_number)
            if len(self.reads) == 2:
                monkeypatch.setattr(
                    scoped_controller,
                    "_cancel_run_or_defer_active_conflict",
                    forged_cancel,
                )
            return result

    api = RebindingSweepApi(
        (_run(50, stale_head, (501,)),),
        {501: [qualification, qualification]},
    )

    assert cancel_superseded_explicit_pr_runs(
        api,  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=99,
    ) == (50,)
    assert api.cancelled == [50]
    assert forged_calls == []


def test_trigger_cancel_effect_global_rebind_during_final_reread_cannot_redirect(
    monkeypatch,
) -> None:
    qualification = PullRequestQualification(
        head_sha=HEAD,
        integration_capable=False,
    )
    forged_calls: list[int] = []

    def forged_cancel(_api, run_id: int) -> bool:
        forged_calls.append(run_id)
        return True

    class RebindingTriggerApi(FakeApi):
        def live_pr_qualification(self, pr_number: int) -> PullRequestQualification:
            result = super().live_pr_qualification(pr_number)
            monkeypatch.setattr(
                scoped_controller,
                "_cancel_run_or_defer_active_conflict",
                forged_cancel,
            )
            return result

    api = RebindingTriggerApi(qualification)

    assert _cancel_triggering_run_if_stale_or_nonqualifying(
        api,  # type: ignore[arg-type]
        pr_number=2039,
        event_head_sha=HEAD,
        current_run_id=91,
        qualification=qualification,
    )
    assert api.cancelled == [91]
    assert forged_calls == []


def test_sweep_rejects_captured_cancel_effect_code_drift() -> None:
    qualification = PullRequestQualification(
        head_sha="6" * 40,
        integration_capable=True,
    )
    api = SweepApi(
        (_run(50, "5" * 40, (501,)),),
        {501: [qualification, qualification]},
    )

    def forged_cancel(_api, _run_id: int) -> bool:
        return True

    canonical_effect = (
        cancel_superseded_explicit_pr_runs.__kwdefaults__["_cancel_effect"]
    )
    with pytest.raises(
        CancellationError,
        match="canonical cancel effect authority changed",
    ):
        cancel_superseded_explicit_pr_runs(
            api,  # type: ignore[arg-type]
            workflow_name="CI",
            current_run_id=99,
            _cancel_effect=canonical_effect,
            _cancel_effect_code=forged_cancel.__code__,
        )
    assert api.cancelled == []


def test_sweep_trusted_tuple_head_cannot_be_rewritten_by_sha_helper_rebind(monkeypatch) -> None:
    current_head = "8" * 40
    forged_head = "9" * 40
    cancel_calls = {"count": 0}

    monkeypatch.setattr(
        scoped_controller,
        "_trusted_live_pr_qualification",
        lambda *_args, **_kwargs: (current_head, True),
    )
    monkeypatch.setattr(
        scoped_controller,
        "_explicit_run_identity_is_current",
        lambda *_args, **_kwargs: True,
    )

    def forged_sha(value, *, field: str):
        if field == "pull request head sha":
            return forged_head
        return value

    monkeypatch.setattr(scoped_controller, "_require_sha", forged_sha)

    def forged_cancel(_api, _run_id):
        cancel_calls["count"] += 1
        return True

    monkeypatch.setattr(
        scoped_controller,
        "_cancel_run_or_defer_active_conflict",
        forged_cancel,
    )

    api = SweepApi((_run(50, current_head, (501,)),), {})
    assert cancel_superseded_explicit_pr_runs(
        api,  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=99,
    ) == ()
    assert cancel_calls == {"count": 0}


def test_trigger_trusted_tuple_head_cannot_be_rewritten_by_sha_helper_rebind(monkeypatch) -> None:
    ready_head = "7" * 40
    forged_head = "8" * 40
    cancel_calls = {"count": 0}

    def forged_sha(value, *, field: str):
        if field == "pull request head sha":
            return forged_head
        return value

    monkeypatch.setattr(scoped_controller, "_require_sha", forged_sha)
    monkeypatch.setattr(
        scoped_controller,
        "_explicit_run_identity_is_current",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        scoped_controller,
        "_trusted_live_pr_qualification",
        lambda *_args, **_kwargs: (ready_head, True),
    )

    def forged_cancel(_api, _run_id):
        cancel_calls["count"] += 1
        return True

    monkeypatch.setattr(
        scoped_controller,
        "_cancel_run_or_defer_active_conflict",
        forged_cancel,
    )

    api = SweepApi((), {})
    assert not _cancel_triggering_run_if_stale_or_nonqualifying(
        api,  # type: ignore[arg-type]
        pr_number=303,
        event_head_sha=ready_head,
        current_run_id=7012,
        qualification=(ready_head, True),
    )
    assert cancel_calls == {"count": 0}


def test_explicit_run_identity_ignores_mutable_coordinate_helpers(monkeypatch) -> None:
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
                b'{"id":7012,"workflow_id":356678400,'
                b'"event":"pull_request","head_sha":"'
                + HEAD.encode("ascii")
                + b'","name":"CI","status":"queued",'
                b'"pull_requests":[{"number":303}]}'
            )

    def canonical_urlopen(request, *, timeout: int):
        assert timeout == 20
        requested.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(
        scoped_controller,
        "_require_positive_int",
        lambda _value, *, field: 999999,
    )
    monkeypatch.setattr(
        scoped_controller,
        "_require_sha",
        lambda _value, *, field: "b" * 40,
    )
    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        canonical_urlopen,
    )

    assert _explicit_run_identity_is_current(
        api,
        run_id=7012,
        expected_head_sha=HEAD,
        pr_number=303,
    )
    assert requested == [
        "https://api.github.com/repos/owner/repo/actions/runs/7012"
    ]


def test_explicit_run_boundary_scoped_type_rebind_cannot_reopen_dynamic_request_shadow(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    forged_invoked = {"value": False}

    def forged_request(_path: str, **_kwargs):
        forged_invoked["value"] = True
        return {
            "id": 7012,
            "workflow_id": 356678400,
            "event": "pull_request",
            "head_sha": HEAD,
            "name": "CI",
            "status": "queued",
            "pull_requests": [{"number": 303}],
        }

    class ReboundScopedType:
        pass

    monkeypatch.setattr(
        scoped_controller,
        "WorkflowScopedGitHubApi",
        ReboundScopedType,
    )
    monkeypatch.setattr(api, "_request", forged_request)

    assert not _explicit_run_identity_is_current(
        api,
        run_id=7012,
        expected_head_sha=HEAD,
        pr_number=303,
    )
    assert not forged_invoked["value"]


def test_live_pr_boundary_scoped_type_rebind_cannot_reopen_nested_request_shadow(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    forged_invoked = {"value": False}

    def forged_request(_path: str, **_kwargs):
        forged_invoked["value"] = True
        return {
            "state": "open",
            "draft": False,
            "head": {
                "sha": STALE_HEAD,
                "repo": {"full_name": "owner/repo"},
            },
            "base": {"repo": {"full_name": "owner/repo"}},
        }

    class ReboundScopedType:
        pass

    monkeypatch.setattr(
        scoped_controller,
        "WorkflowScopedGitHubApi",
        ReboundScopedType,
    )
    monkeypatch.setattr(api, "_request", forged_request)

    with pytest.raises(CancellationError, match="live PR qualification dispatch changed"):
        _trusted_live_pr_qualification(api, 303)
    assert not forged_invoked["value"]

def test_historical_association_rejects_urlencode_default_rebase(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    encoder = scoped_controller.urlencode
    defaults = encoder.__defaults__
    assert defaults is not None and defaults

    def forged_quote_via(string, safe, encoding=None, errors=None):
        del safe, encoding, errors
        return "page" if string == "per_page" else "2"

    monkeypatch.setattr(
        encoder,
        "__defaults__",
        defaults[:-1] + (forged_quote_via,),
    )

    with pytest.raises(
        CancellationError,
        match="historical association authority is unavailable",
    ):
        api._historical_associated_pr_number(HEAD)


def test_zero_association_rejects_urlencode_default_rebase(monkeypatch) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    encoder = scoped_controller.urlencode
    defaults = encoder.__defaults__
    assert defaults is not None and defaults

    def forged_quote_via(string, safe, encoding=None, errors=None):
        del string, safe, encoding, errors
        return "forged"

    monkeypatch.setattr(
        encoder,
        "__defaults__",
        defaults[:-1] + (forged_quote_via,),
    )

    with pytest.raises(
        CancellationError,
        match="historical association authority is unavailable",
    ):
        api._historical_head_has_no_associated_prs(HEAD)


def test_active_run_scan_rejects_urlencode_default_rebase(monkeypatch) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    encoder = scoped_controller.urlencode
    defaults = encoder.__defaults__
    assert defaults is not None and defaults
    requested: list[str] = []

    def forged_quote_via(string, safe, encoding=None, errors=None):
        del string, safe, encoding, errors
        return "forged"

    def forbidden_request(path: str, **_kwargs):
        requested.append(path)
        raise AssertionError("rebased query encoder must not reach transport")

    monkeypatch.setattr(
        encoder,
        "__defaults__",
        defaults[:-1] + (forged_quote_via,),
    )
    monkeypatch.setattr(api, "_request", forbidden_request)

    with pytest.raises(
        CancellationError,
        match="active workflow pagination authority is unavailable",
    ):
        api._active_runs_for_status("queued")
    assert requested == []


def test_historical_association_rejects_inflight_request_default_rebase(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    request_impl = scoped_controller.GitHubApi._request
    defaults = request_impl.__kwdefaults__
    assert defaults is not None
    requested: list[str] = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            item = (
                b'{"number":303,"head":{"sha":"'
                + HEAD.encode("ascii")
                + b'"}}'
            )
            return b"[" + b",".join(item for _ in range(100)) + b"]"

    def mutating_urlopen(request, *, timeout: int):
        assert timeout == 20
        requested.append(request.full_url)
        monkeypatch.setitem(defaults, "_json_parse_int", str)
        return FakeResponse()

    monkeypatch.setitem(
        request_impl.__globals__,
        "urlopen",
        mutating_urlopen,
    )

    with pytest.raises(
        CancellationError,
        match="historical association request dispatch changed",
    ):
        api._historical_associated_pr_number(HEAD)

    assert len(requested) == 1


def test_zero_association_rejects_inflight_request_default_rebase(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    request_impl = scoped_controller.GitHubApi._request
    defaults = request_impl.__kwdefaults__
    assert defaults is not None
    requested: list[str] = []

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            item = b'{"number":303}'
            return b"[" + b",".join(item for _ in range(100)) + b"]"

    def mutating_urlopen(request, *, timeout: int):
        assert timeout == 20
        requested.append(request.full_url)
        monkeypatch.setitem(defaults, "_json_parse_int", str)
        return FakeResponse()

    monkeypatch.setitem(
        request_impl.__globals__,
        "urlopen",
        mutating_urlopen,
    )

    with pytest.raises(
        CancellationError,
        match="historical association request dispatch changed",
    ):
        api._historical_head_has_no_associated_prs(HEAD)

    assert len(requested) == 1


def test_historical_association_pr_identity_ignores_rebound_compat_validators(
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
                b'[{"number":303,"head":{"sha":"'
                + HEAD.encode("ascii")
                + b'"}}]'
            )

    def canonical_urlopen(request, *, timeout: int):
        assert timeout == 20
        requested.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(
        scoped_controller,
        "_require_positive_int",
        lambda _value, *, field: 999,
    )
    monkeypatch.setattr(
        scoped_controller,
        "_require_sha",
        lambda _value, *, field: STALE_HEAD,
    )
    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        canonical_urlopen,
    )

    assert api._historical_associated_pr_number(HEAD) == 303
    assert requested == [
        "https://api.github.com/repos/owner/repo/commits/"
        + HEAD
        + "/pulls?per_page=100&page=1"
    ]


def test_historical_association_pagination_bound_cannot_be_rebound_to_hide_ambiguity(
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
    item_303 = (
        b'{"number":303,"head":{"sha":"'
        + HEAD.encode("ascii")
        + b'"}}'
    )
    page_one = b"[" + b",".join([item_303] * 100) + b"]"
    page_two = (
        b'[{"number":304,"head":{"sha":"'
        + HEAD.encode("ascii")
        + b'"}}]'
    )

    class FakeResponse:
        status = 200

        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            return self.body

    def canonical_urlopen(request, *, timeout: int):
        assert timeout == 20
        requested.append(request.full_url)
        if "page=1" in request.full_url:
            return FakeResponse(page_one)
        if "page=2" in request.full_url:
            return FakeResponse(page_two)
        raise AssertionError(request.full_url)

    # GitHub caps per_page at 100. If a mutable module global can widen the local
    # comparison to 101 after composition, a real 100-row first page looks terminal
    # and a second associated PR can be hidden.
    monkeypatch.setattr(scoped_controller, "_PULLS_PER_PAGE", 101)
    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        canonical_urlopen,
    )

    with pytest.raises(scoped_controller._HistoricalAssociationAmbiguous):
        api._historical_associated_pr_number(HEAD)

    assert requested == [
        "https://api.github.com/repos/owner/repo/commits/"
        + HEAD
        + "/pulls?per_page=100&page=1",
        "https://api.github.com/repos/owner/repo/commits/"
        + HEAD
        + "/pulls?per_page=100&page=2",
    ]


def test_zero_association_commit_coordinate_ignores_rebound_sha_helper(
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
            return b"[]"

    def canonical_urlopen(request, *, timeout: int):
        assert timeout == 20
        requested.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(
        scoped_controller,
        "_require_sha",
        lambda _value, *, field: STALE_HEAD,
    )
    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        canonical_urlopen,
    )

    assert api._historical_head_has_no_associated_prs(HEAD)
    assert requested == [
        "https://api.github.com/repos/owner/repo/commits/"
        + HEAD
        + "/pulls?per_page=100&page=1"
    ]


def test_canonical_branch_head_rejects_quote_default_rebase(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    encoder = scoped_controller.quote
    defaults = encoder.__defaults__
    assert defaults is not None and defaults
    invoked = {"value": False}
    canonical_request = scoped_controller.GitHubApi._request

    def forbidden_urlopen(*_args, **_kwargs):
        invoked["value"] = True
        raise AssertionError("rebased branch encoder must not reach transport")

    monkeypatch.setattr(
        encoder,
        "__defaults__",
        defaults[:-1] + ("ignore",),
    )
    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        forbidden_urlopen,
    )

    with pytest.raises(
        CancellationError,
        match="invalid canonical head branch",
    ):
        api._canonical_branch_head("feature/original")
    assert not invoked["value"]


def test_canonical_branch_head_rejects_quote_default_rebase_during_read(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    encoder = scoped_controller.quote
    defaults = encoder.__defaults__
    assert defaults is not None and defaults
    canonical_request = scoped_controller.GitHubApi._request

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            return (
                b'{"object":{"sha":"'
                + STALE_HEAD.encode("ascii")
                + b'"}}'
            )

    def mutating_urlopen(request, *, timeout: int):
        assert timeout == 20
        assert request.full_url.endswith(
            "/git/ref/heads/feature%2Foriginal"
        )
        monkeypatch.setattr(
            encoder,
            "__defaults__",
            defaults[:-1] + ("ignore",),
        )
        return FakeResponse()

    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        mutating_urlopen,
    )

    with pytest.raises(
        CancellationError,
        match="canonical branch request dispatch changed",
    ):
        api._canonical_branch_head("feature/original")


def test_canonical_branch_head_ignores_rebound_sha_and_quote_helpers(
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
                b'{"object":{"sha":"'
                + STALE_HEAD.encode("ascii")
                + b'"}}'
            )

    def canonical_urlopen(request, *, timeout: int):
        assert timeout == 20
        requested.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(
        scoped_controller,
        "_require_sha",
        lambda _value, *, field: HEAD,
    )
    monkeypatch.setattr(
        scoped_controller,
        "quote",
        lambda _value, *, safe="": "attacker%2Fredirect",
    )
    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        canonical_urlopen,
    )

    assert api._canonical_branch_head("feature/original") == STALE_HEAD
    assert requested == [
        "https://api.github.com/repos/owner/repo/git/ref/heads/feature%2Foriginal"
    ]

def test_active_run_scan_rejects_parse_run_kwdefault_rebase(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    parser = scoped_controller.parse_run
    defaults = parser.__kwdefaults__
    assert defaults is not None
    requested: list[str] = []

    def forged_positive_int(_value, *, field: str) -> int:
        del field
        return 999

    def forbidden_request(path: str, **_kwargs):
        requested.append(path)
        raise AssertionError("rebased run parser must not reach transport")

    monkeypatch.setitem(defaults, "_positive_int", forged_positive_int)
    monkeypatch.setitem(defaults, "_positive_int_code", forged_positive_int.__code__)
    monkeypatch.setattr(api, "_request", forbidden_request)

    with pytest.raises(
        CancellationError,
        match="active workflow pagination authority is unavailable",
    ):
        api._active_runs_for_status("queued")
    assert requested == []


def test_active_run_pagination_bound_cannot_be_rebound_to_hide_second_page(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    requested: list[str] = []

    def run_payload(run_id: int) -> dict[str, object]:
        return {
            "id": run_id,
            "head_sha": HEAD,
            "name": "CI",
            "status": "queued",
            "pull_requests": [{"number": 303}],
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
        requested.append(path)
        if "page=1" in path:
            return {
                "total_count": 101,
                "workflow_runs": [run_payload(run_id) for run_id in range(1, 101)],
            }
        if "page=2" in path:
            return {
                "total_count": 101,
                "workflow_runs": [run_payload(101)],
            }
        raise AssertionError(path)

    monkeypatch.setattr(api, "_request", fake_request)
    monkeypatch.setattr(scoped_controller, "_RUNS_PER_PAGE", 101)
    monkeypatch.setattr(
        scoped_controller,
        "urlencode",
        lambda _params: "event=attacker&per_page=999&page=1",
    )

    runs = api._active_runs_for_status("queued")

    assert tuple(run.run_id for run in runs) == tuple(range(1, 102))
    assert requested == [
        "/actions/workflows/356678400/runs?"
        "event=pull_request&status=queued&per_page=100&page=1",
        "/actions/workflows/356678400/runs?"
        "event=pull_request&status=queued&per_page=100&page=2",
    ]

def test_active_run_pagination_overlap_cannot_fake_snapshot_completion(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    requested: list[str] = []

    def run_payload(run_id: int) -> dict[str, object]:
        return {
            "id": run_id,
            "head_sha": HEAD,
            "name": "CI",
            "status": "queued",
            "pull_requests": [{"number": 303}],
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
        requested.append(path)
        if "page=1" in path:
            return {
                "total_count": 200,
                "workflow_runs": [run_payload(run_id) for run_id in range(1, 101)],
            }
        if "page=2" in path:
            # Fifty rows overlap page 1 after concurrent queue growth. Counting raw
            # observations would reach total_count=200 here and incorrectly stop,
            # leaving 151..200 invisible to this sweep.
            return {
                "total_count": 200,
                "workflow_runs": [run_payload(run_id) for run_id in range(51, 151)],
            }
        if "page=3" in path:
            return {
                "total_count": 200,
                "workflow_runs": [run_payload(run_id) for run_id in range(151, 201)],
            }
        raise AssertionError(path)

    monkeypatch.setattr(api, "_request", fake_request)

    runs = api._active_runs_for_status("queued")

    assert {run.run_id for run in runs} == set(range(1, 201))
    assert requested == [
        "/actions/workflows/356678400/runs?"
        "event=pull_request&status=queued&per_page=100&page=1",
        "/actions/workflows/356678400/runs?"
        "event=pull_request&status=queued&per_page=100&page=2",
        "/actions/workflows/356678400/runs?"
        "event=pull_request&status=queued&per_page=100&page=3",
    ]


def test_active_run_reader_stops_after_full_page_with_no_unique_progress(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    requested: list[str] = []

    def run_payload(run_id: int) -> dict[str, object]:
        return {
            "id": run_id,
            "head_sha": HEAD,
            "name": "CI",
            "status": "queued",
            "pull_requests": [{"number": 303}],
            "head_branch": "feature/head",
            "head_repository": {"full_name": "owner/repo"},
        }

    first_page = [run_payload(run_id) for run_id in range(1, 101)]

    def fake_request(
        path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ) -> object:
        assert method == "GET"
        assert not allowed_http_errors
        requested.append(path)
        if "page=1" in path:
            return {"total_count": 200, "workflow_runs": first_page}
        if "page=2" in path:
            return {"total_count": 200, "workflow_runs": list(first_page)}
        raise AssertionError("duplicate full page must terminate moving snapshot")

    monkeypatch.setattr(api, "_request", fake_request)

    runs = api._active_runs_for_status("queued")

    assert {run.run_id for run in runs} == set(range(1, 101))
    assert requested == [
        "/actions/workflows/356678400/runs?"
        "event=pull_request&status=queued&per_page=100&page=1",
        "/actions/workflows/356678400/runs?"
        "event=pull_request&status=queued&per_page=100&page=2",
    ]


def test_active_run_reader_rejects_provider_page_larger_than_requested_bound(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )

    def run_payload(run_id: int) -> dict[str, object]:
        return {
            "id": run_id,
            "head_sha": HEAD,
            "name": "CI",
            "status": "queued",
            "pull_requests": [{"number": 303}],
            "head_branch": "feature/head",
            "head_repository": {"full_name": "owner/repo"},
        }

    def fake_request(
        path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ) -> object:
        assert "per_page=100&page=1" in path
        assert method == "GET"
        assert not allowed_http_errors
        return {
            "total_count": 101,
            "workflow_runs": [run_payload(run_id) for run_id in range(1, 102)],
        }

    monkeypatch.setattr(api, "_request", fake_request)

    with pytest.raises(CancellationError, match="invalid workflow-runs page size"):
        api._active_runs_for_status("queued")


def test_active_run_reader_rejects_total_count_smaller_than_page(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )

    def run_payload(run_id: int) -> dict[str, object]:
        return {
            "id": run_id,
            "head_sha": HEAD,
            "name": "CI",
            "status": "queued",
            "pull_requests": [{"number": 303}],
            "head_branch": "feature/head",
            "head_repository": {"full_name": "owner/repo"},
        }

    def fake_request(
        path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ) -> object:
        assert "per_page=100&page=1" in path
        assert method == "GET"
        assert not allowed_http_errors
        return {
            "total_count": 1,
            "workflow_runs": [run_payload(1), run_payload(2)],
        }

    monkeypatch.setattr(api, "_request", fake_request)

    with pytest.raises(CancellationError, match="invalid workflow-runs total_count"):
        api._active_runs_for_status("queued")


def test_sweep_decision_helpers_are_captured_before_active_run_callback_rebind(
    monkeypatch,
) -> None:
    cancelled: list[int] = []
    forged_calls: list[str] = []

    class FixtureApi:
        _WorkflowScopedGitHubApi__workflow_name = "CI"

        def active_runs(self) -> tuple[WorkflowRun, ...]:
            monkeypatch.setattr(
                scoped_controller,
                "_trusted_live_pr_qualification",
                lambda *_args, **_kwargs: forged_calls.append("qualification")
                or (STALE_HEAD, False),
            )
            monkeypatch.setattr(
                scoped_controller,
                "_explicit_run_identity_is_current",
                lambda *_args, **_kwargs: forged_calls.append("identity") or False,
            )
            return (
                WorkflowRun(
                    run_id=7014,
                    head_sha=STALE_HEAD,
                    workflow_name="CI",
                    pr_numbers=(303,),
                    status="queued",
                ),
            )

        def cancel(self, run_id: int) -> None:
            cancelled.append(run_id)

    def trusted_qualification(_api, pr_number: int):
        assert pr_number == 303
        return (HEAD, True)

    def trusted_identity(
        _api,
        *,
        run_id: int,
        expected_head_sha: str,
        pr_number: int,
    ) -> bool:
        assert run_id == 7014
        assert expected_head_sha == STALE_HEAD
        assert pr_number == 303
        return True

    monkeypatch.setattr(
        scoped_controller,
        "_trusted_live_pr_qualification",
        trusted_qualification,
    )
    monkeypatch.setattr(
        scoped_controller,
        "_explicit_run_identity_is_current",
        trusted_identity,
    )

    assert cancel_superseded_explicit_pr_runs(
        FixtureApi(),  # type: ignore[arg-type]
        workflow_name="CI",
        current_run_id=9999,
    ) == (7014,)
    assert cancelled == [7014]
    assert forged_calls == []


def test_trigger_decision_helpers_survive_inflight_identity_global_rebind(
    monkeypatch,
) -> None:
    cancelled: list[int] = []
    forged_calls: list[str] = []

    class FixtureApi:
        def cancel(self, run_id: int) -> None:
            cancelled.append(run_id)

    def trusted_qualification(_api, pr_number: int):
        assert pr_number == 303
        return (HEAD, True)

    def trusted_identity(
        _api,
        *,
        run_id: int,
        expected_head_sha: str,
        pr_number: int,
    ) -> bool:
        assert run_id == 7015
        assert expected_head_sha == STALE_HEAD
        assert pr_number == 303
        monkeypatch.setattr(
            scoped_controller,
            "_trusted_live_pr_qualification",
            lambda *_args, **_kwargs: forged_calls.append("qualification")
            or (STALE_HEAD, False),
        )
        monkeypatch.setattr(
            scoped_controller,
            "_explicit_run_identity_is_current",
            lambda *_args, **_kwargs: forged_calls.append("identity") or False,
        )
        return True

    monkeypatch.setattr(
        scoped_controller,
        "_trusted_live_pr_qualification",
        trusted_qualification,
    )
    monkeypatch.setattr(
        scoped_controller,
        "_explicit_run_identity_is_current",
        trusted_identity,
    )

    assert _cancel_triggering_run_if_stale_or_nonqualifying(
        FixtureApi(),  # type: ignore[arg-type]
        pr_number=303,
        event_head_sha=STALE_HEAD,
        current_run_id=7015,
        qualification=(HEAD, True),
    )
    assert cancelled == [7015]
    assert forged_calls == []

def test_production_sweep_ignores_preentry_decision_global_rebind(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    canonical_request = scoped_controller.GitHubApi._request
    cancelled: list[int] = []
    forged_calls: list[str] = []

    class FakeResponse:
        status = 200

        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            return self.body

    def canonical_urlopen(request, *, timeout: int):
        assert timeout == 20
        url = request.full_url
        if url.endswith("/pulls/303"):
            return FakeResponse(
                (
                    '{"state":"open","draft":false,'
                    '"head":{"sha":"' + HEAD + '","repo":{"full_name":"owner/repo"}},'
                    '"base":{"repo":{"full_name":"owner/repo"}}}'
                ).encode()
            )
        if url.endswith("/actions/runs/7014"):
            return FakeResponse(
                (
                    '{"id":7014,"workflow_id":356678400,'
                    '"event":"pull_request","head_sha":"' + STALE_HEAD + '",'
                    '"name":"CI","status":"queued",'
                    '"pull_requests":[{"number":303}]}'
                ).encode()
            )
        raise AssertionError(url)

    def forged_qualification(*_args, **_kwargs):
        forged_calls.append("qualification")
        return (STALE_HEAD, False)

    def forged_identity(*_args, **_kwargs):
        forged_calls.append("identity")
        return False

    def fake_cancel(_api, run_id: int) -> bool:
        cancelled.append(run_id)
        return True

    monkeypatch.setattr(
        scoped_controller,
        "_trusted_live_pr_qualification",
        forged_qualification,
    )
    monkeypatch.setattr(
        scoped_controller,
        "_explicit_run_identity_is_current",
        forged_identity,
    )
    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        canonical_urlopen,
    )

    assert cancel_superseded_explicit_pr_runs(
        api,
        workflow_name="CI",
        current_run_id=9999,
        runs=(
            WorkflowRun(
                run_id=7014,
                head_sha=STALE_HEAD,
                workflow_name="CI",
                pr_numbers=(303,),
                status="queued",
            ),
        ),
        _cancel_effect=fake_cancel,
        _cancel_effect_code=fake_cancel.__code__,
    ) == (7014,)
    assert cancelled == [7014]
    assert forged_calls == []


def test_production_trigger_ignores_preentry_decision_global_rebind(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    canonical_request = scoped_controller.GitHubApi._request
    cancelled: list[int] = []
    forged_calls: list[str] = []

    class FakeResponse:
        status = 200

        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            return self.body

    def canonical_urlopen(request, *, timeout: int):
        assert timeout == 20
        url = request.full_url
        if url.endswith("/actions/runs/7015"):
            return FakeResponse(
                (
                    '{"id":7015,"workflow_id":356678400,'
                    '"event":"pull_request","head_sha":"' + STALE_HEAD + '",'
                    '"name":"CI","status":"queued",'
                    '"pull_requests":[{"number":303}]}'
                ).encode()
            )
        if url.endswith("/pulls/303"):
            return FakeResponse(
                (
                    '{"state":"open","draft":false,'
                    '"head":{"sha":"' + HEAD + '","repo":{"full_name":"owner/repo"}},'
                    '"base":{"repo":{"full_name":"owner/repo"}}}'
                ).encode()
            )
        raise AssertionError(url)

    def forged_qualification(*_args, **_kwargs):
        forged_calls.append("qualification")
        return (STALE_HEAD, False)

    def forged_identity(*_args, **_kwargs):
        forged_calls.append("identity")
        return False

    def fake_cancel(_api, run_id: int) -> bool:
        cancelled.append(run_id)
        return True

    monkeypatch.setattr(
        scoped_controller,
        "_trusted_live_pr_qualification",
        forged_qualification,
    )
    monkeypatch.setattr(
        scoped_controller,
        "_explicit_run_identity_is_current",
        forged_identity,
    )
    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        canonical_urlopen,
    )

    assert _cancel_triggering_run_if_stale_or_nonqualifying(
        api,
        pr_number=303,
        event_head_sha=STALE_HEAD,
        current_run_id=7015,
        qualification=(HEAD, True),
        _cancel_effect=fake_cancel,
        _cancel_effect_code=fake_cancel.__code__,
    )
    assert cancelled == [7015]
    assert forged_calls == []



def test_event_head_coordinate_does_not_use_rebound_sha_helper(
    monkeypatch,
) -> None:
    forged_calls: list[tuple[object, str]] = []

    def forged_sha(value, *, field: str) -> str:
        forged_calls.append((value, field))
        return HEAD

    monkeypatch.setattr(scoped_controller, "_require_sha", forged_sha)

    runs = (
        WorkflowRun(
            run_id=7016,
            head_sha=STALE_HEAD,
            workflow_name="CI",
            pr_numbers=(303,),
            status="queued",
        ),
    )
    assert (
        _explicit_singleton_pr_for_current_run(
            runs,
            workflow_name="CI",
            current_run_id=7016,
            event_head_sha=STALE_HEAD,
        )
        == 303
    )

    cancelled: list[int] = []

    class FixtureApi:
        def cancel(self, run_id: int) -> None:
            cancelled.append(run_id)

    def trusted_qualification(_api, pr_number: int):
        assert pr_number == 303
        return (HEAD, True)

    def trusted_identity(
        _api,
        *,
        run_id: int,
        expected_head_sha: str,
        pr_number: int,
    ) -> bool:
        assert run_id == 7016
        assert expected_head_sha == STALE_HEAD
        assert pr_number == 303
        return True

    def cancel_effect(_api, run_id: int) -> bool:
        cancelled.append(run_id)
        return True

    assert _cancel_triggering_run_if_stale_or_nonqualifying(
        FixtureApi(),  # type: ignore[arg-type]
        pr_number=303,
        event_head_sha=STALE_HEAD,
        current_run_id=7016,
        qualification=(HEAD, True),
        _cancel_effect=cancel_effect,
        _cancel_effect_code=cancel_effect.__code__,
        _qualification_reader=trusted_qualification,
        _qualification_reader_code=trusted_qualification.__code__,
        _identity_checker=trusted_identity,
        _identity_checker_code=trusted_identity.__code__,
    )
    assert cancelled == [7016]
    assert forged_calls == []


def test_controller_event_head_validation_is_primitive_not_global() -> None:
    source = Path("scripts/cancel_superseded_pr_workflow_runs_scoped.py").read_text(
        encoding="utf-8"
    )
    snapshot = source.split("def _explicit_singleton_pr_for_current_run(", 1)[1].split(
        "def _validated_event_pr_identity(", 1
    )[0]
    trigger = source.split(
        "def _cancel_triggering_run_if_stale_or_nonqualifying(", 1
    )[1].split("def main(", 1)[0]
    main_source = source.split("def main(", 1)[1]

    assert '_require_sha(event_head_sha, field="event head sha")' not in snapshot
    assert '_require_sha(event_head_sha, field="event head sha")' not in trigger
    assert '_require_sha(args.event_head_sha, field="event head sha")' not in main_source

def test_scoped_active_run_scan_rejects_inflight_request_kwdefault_rebase(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    canonical_request = scoped_controller.GitHubApi._request
    defaults = canonical_request.__kwdefaults__
    assert defaults is not None

    class MutatingResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            # The current request already owns its bound parser arguments. The
            # mutation is aimed at a later page/status request and must therefore
            # be detected by the active-snapshot post-read authority witness.
            monkeypatch.setitem(defaults, "_json_parse_int", str)
            return b'{"total_count":0,"workflow_runs":[]}'

    def mutating_urlopen(_request, *, timeout: int):
        assert timeout == 20
        return MutatingResponse()

    monkeypatch.setitem(
        canonical_request.__globals__,
        "urlopen",
        mutating_urlopen,
    )

    with pytest.raises(
        CancellationError,
        match="active workflow snapshot authority changed",
    ):
        api._active_runs_for_status("queued")


def test_scoped_active_run_scan_rejects_inflight_request_dispatch_rebind(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    forged_calls: list[str] = []

    def forged_request(*_args, **_kwargs):
        forged_calls.append("request")
        return {"total_count": 0, "workflow_runs": []}

    def first_request(
        path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ) -> object:
        assert method == "GET"
        assert not allowed_http_errors
        assert "status=queued" in path
        monkeypatch.setattr(api, "_request", forged_request)
        return {
            "total_count": 101,
            "workflow_runs": [
                {
                    "id": run_id,
                    "head_sha": HEAD,
                    "name": "CI",
                    "status": "queued",
                    "pull_requests": [{"number": 303}],
                    "head_branch": "feature/head",
                    "head_repository": {"full_name": "owner/repo"},
                }
                for run_id in range(1, 101)
            ],
        }

    monkeypatch.setattr(api, "_request", first_request)

    with pytest.raises(
        CancellationError,
        match="active workflow snapshot authority changed",
    ):
        api._active_runs_for_status("queued")

    assert forged_calls == []

def test_scoped_active_run_scan_rejects_inflight_workflow_run_constructor_mutation(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    workflow_run_init = WorkflowRun.__dict__["__init__"]

    def forged_init(
        self,
        run_id,
        head_sha,
        workflow_name,
        pr_numbers,
        status,
    ) -> None:
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "head_sha", head_sha)
        object.__setattr__(self, "workflow_name", workflow_name)
        object.__setattr__(self, "pr_numbers", (999,))
        object.__setattr__(self, "status", status)

    def first_request(
        path: str,
        *,
        method: str = "GET",
        allowed_http_errors: frozenset[int] = frozenset(),
    ) -> object:
        assert method == "GET"
        assert not allowed_http_errors
        assert "status=queued" in path
        forged_code = forged_init.__code__.replace(
            co_freevars=workflow_run_init.__code__.co_freevars,
        )
        monkeypatch.setattr(workflow_run_init, "__code__", forged_code)
        return {
            "total_count": 1,
            "workflow_runs": [
                {
                    "id": 71,
                    "head_sha": HEAD,
                    "name": "CI",
                    "status": "queued",
                    "pull_requests": [{"number": 303}],
                    "head_branch": "feature/head",
                    "head_repository": {"full_name": "owner/repo"},
                }
            ],
        }

    monkeypatch.setattr(api, "_request", first_request)

    with pytest.raises(
        CancellationError,
        match="workflow run parser authority changed",
    ):
        api._active_runs_for_status("queued")


def test_scoped_main_orchestration_roots_are_not_caller_injectable() -> None:
    forged_calls: list[str] = []

    class ForgedScopedApi:
        def __init__(self, **_kwargs) -> None:
            forged_calls.append("api")

        def active_runs(self):
            forged_calls.append("active")
            return ()

        def cancel_historical_unbound_runs(self, **_kwargs):
            forged_calls.append("orphan")
            return ()

    def forged_sweep(*_args, **_kwargs):
        forged_calls.append("sweep")
        return ()

    def forged_trigger(*_args, **_kwargs):
        forged_calls.append("trigger")
        return False

    def forged_snapshot(*_args, **_kwargs):
        forged_calls.append("snapshot")
        return None

    def forged_qualification(*_args, **_kwargs):
        forged_calls.append("qualification")
        return (HEAD, True)

    def forged_event(*_args, **_kwargs):
        forged_calls.append("event")
        return (2039, False)

    forged_globals = {
        "WorkflowScopedGitHubApi": ForgedScopedApi,
        "cancel_superseded_explicit_pr_runs": forged_sweep,
        "_cancel_triggering_run_if_stale_or_nonqualifying": forged_trigger,
        "_explicit_singleton_pr_for_current_run": forged_snapshot,
        "_trusted_live_pr_qualification": forged_qualification,
        "_validated_event_pr_identity": forged_event,
    }

    assert scoped_controller.main.__kwdefaults__ is None
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        scoped_controller.main(
            _scoped_main_args(),
            _module_globals=forged_globals,
            _api_type=ForgedScopedApi,
            _api_init=ForgedScopedApi.__init__,
            _api_init_code=ForgedScopedApi.__init__.__code__,
            _active_runs_impl=ForgedScopedApi.active_runs,
            _active_runs_code=ForgedScopedApi.active_runs.__code__,
            _orphan_impl=ForgedScopedApi.cancel_historical_unbound_runs,
            _orphan_code=ForgedScopedApi.cancel_historical_unbound_runs.__code__,
            _sweep_impl=forged_sweep,
            _sweep_code=forged_sweep.__code__,
            _trigger_impl=forged_trigger,
            _trigger_code=forged_trigger.__code__,
            _snapshot_identity_impl=forged_snapshot,
            _snapshot_identity_code=forged_snapshot.__code__,
            _trusted_qualification_impl=forged_qualification,
            _trusted_qualification_code=forged_qualification.__code__,
            _event_identity_impl=forged_event,
            _event_identity_code=forged_event.__code__,
        )
    assert forged_calls == []


def test_scoped_main_kwdefaults_metadata_cannot_rebase_orchestration(
    monkeypatch,
) -> None:
    forged_calls: list[str] = []

    def forged_sweep(*_args, **_kwargs):
        forged_calls.append("sweep")
        return ()

    monkeypatch.setattr(
        scoped_controller.main,
        "__kwdefaults__",
        {"_sweep_impl": forged_sweep, "_sweep_code": forged_sweep.__code__},
    )
    monkeypatch.setattr(
        scoped_controller,
        "cancel_superseded_explicit_pr_runs",
        forged_sweep,
    )

    assert scoped_controller.main(_scoped_main_args()) == 2
    assert forged_calls == []



def test_scoped_main_has_no_implicit_argv_default_and_entrypoint_passes_sys_argv() -> None:
    assert scoped_controller.main.__defaults__ is None

    source = Path("scripts/cancel_superseded_pr_workflow_runs_scoped.py").read_text(
        encoding="utf-8"
    )
    assert "raise SystemExit(main(sys.argv[1:]))" in source
    assert "raise SystemExit(main())" not in source



def test_scoped_main_rejects_inplace_sweep_kwdefault_rebase(monkeypatch) -> None:
    defaults = scoped_controller.cancel_superseded_explicit_pr_runs.__kwdefaults__
    assert defaults is not None
    original = defaults["_cancel_effect"]

    def forged_cancel_effect(*_args, **_kwargs):
        return True

    assert original is not forged_cancel_effect
    monkeypatch.setitem(defaults, "_cancel_effect", forged_cancel_effect)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    assert scoped_controller.main(_scoped_main_args()) == 2


def test_scoped_main_rejects_replaced_trigger_kwdefault_mapping(monkeypatch) -> None:
    defaults = (
        scoped_controller._cancel_triggering_run_if_stale_or_nonqualifying.__kwdefaults__
    )
    assert defaults is not None

    replacement = dict(defaults)
    monkeypatch.setattr(
        scoped_controller._cancel_triggering_run_if_stale_or_nonqualifying,
        "__kwdefaults__",
        replacement,
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    assert scoped_controller.main(_scoped_main_args()) == 2


def test_active_run_reader_bounds_continuous_growth_to_initial_horizon(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    requested: list[str] = []

    def run_payload(run_id: int) -> dict[str, object]:
        return {
            "id": run_id,
            "head_sha": HEAD,
            "name": "CI",
            "status": "queued",
            "pull_requests": [{"number": 303}],
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
        requested.append(path)
        page = int(path.rsplit("page=", 1)[1])
        if page > 3:
            raise AssertionError("moving scan must not chase continuous queue growth")
        first_id = (page - 1) * 100 + 1
        return {
            "total_count": (page + 1) * 100,
            "workflow_runs": [
                run_payload(run_id)
                for run_id in range(first_id, first_id + 100)
            ],
        }

    monkeypatch.setattr(api, "_request", fake_request)

    runs = api._active_runs_for_status("queued")

    assert {run.run_id for run in runs} == set(range(1, 301))
    assert requested == [
        "/actions/workflows/356678400/runs?"
        "event=pull_request&status=queued&per_page=100&page=1",
        "/actions/workflows/356678400/runs?"
        "event=pull_request&status=queued&per_page=100&page=2",
        "/actions/workflows/356678400/runs?"
        "event=pull_request&status=queued&per_page=100&page=3",
    ]



def test_main_request_budget_exhaustion_defers_incomplete_snapshot(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    requested: list[str] = []

    class FakeResponse:
        status = 200

        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            return False

        def read(self) -> bytes:
            return self.body

    def fake_urlopen(request, *, timeout: int):
        assert timeout == 20
        requested.append(request.full_url)
        assert "/actions/workflows/356678400/runs?" in request.full_url
        assert "status=queued" in request.full_url
        page = len(requested)
        assert 1 <= page <= 12
        first_run_id = (page - 1) * 100 + 1
        payload = {
            "total_count": 1200,
            "workflow_runs": [
                {
                    "id": first_run_id + offset,
                    "head_sha": STALE_HEAD,
                    "name": "CI",
                    "status": "queued",
                    "pull_requests": [{"number": 2039}],
                }
                for offset in range(100)
            ],
        }
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    request_impl = scoped_controller.GitHubApi._request
    monkeypatch.setitem(request_impl.__globals__, "urlopen", fake_urlopen)

    assert scoped_controller.main(_scoped_main_args()) == 0

    captured = capsys.readouterr()
    assert "request budget exhausted" in captured.err
    assert len(requested) == 12
    assert all("/cancel" not in url for url in requested)


def test_request_budget_exhaustion_is_not_downgraded_to_group_failure() -> None:
    api = SweepApi(
        (_run(50, STALE_HEAD, (501,)),),
        {},
    )

    def exhausted_qualification(_api, _pr_number: int):
        raise scoped_controller.RequestBudgetExhausted("fixture request budget exhausted")

    with pytest.raises(
        scoped_controller.RequestBudgetExhausted,
        match="request budget exhausted",
    ):
        cancel_superseded_explicit_pr_runs(
            api,  # type: ignore[arg-type]
            workflow_name="CI",
            current_run_id=99,
            _qualification_reader=exhausted_qualification,
            _qualification_reader_code=exhausted_qualification.__code__,
        )

    assert api.cancelled == []


def test_request_budget_exhaustion_propagates_from_orphan_association(
    monkeypatch,
) -> None:
    api = WorkflowScopedGitHubApi(
        repository="owner/repo",
        token="token",
        workflow_id=356678400,
        workflow_name="CI",
    )
    api._unbound_active_runs[7016] = (STALE_HEAD, None)

    def exhausted_association(_head_sha: str) -> int:
        raise scoped_controller.RequestBudgetExhausted("fixture request budget exhausted")

    monkeypatch.setattr(
        api,
        "_historical_associated_pr_number",
        exhausted_association,
    )

    with pytest.raises(
        scoped_controller.RequestBudgetExhausted,
        match="request budget exhausted",
    ):
        api.cancel_historical_unbound_runs()
