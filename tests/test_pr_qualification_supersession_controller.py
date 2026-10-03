from __future__ import annotations

from pathlib import Path

import scripts.cancel_superseded_pr_workflow_runs_scoped as scoped_controller
from scripts.cancel_superseded_pr_workflow_runs import PullRequestQualification
from scripts.cancel_superseded_pr_workflow_runs_scoped import (
    _cancel_triggering_run_if_stale_or_nonqualifying,
)


_WORKFLOW = Path(".github/workflows/pr-qualification-supersession.yml")


def _text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def test_supersession_controller_is_default_branch_owned_and_write_bounded() -> None:
    workflow = _text()

    assert "workflow_run:" in workflow
    assert "workflows: [CI, Windows candidate, Endurance]" in workflow
    assert "types: [requested]" in workflow
    assert "actions: write" in workflow
    assert "contents: read" in workflow
    assert "pull-requests: read" in workflow
    assert "ref: ${{ github.sha }}" in workflow
    assert "github.event.pull_request.head.sha" not in workflow
    assert "persist-credentials: false" in workflow


def test_current_head_request_cancels_only_obsolete_same_pr_workflow_runs() -> None:
    workflow = _text()

    assert "github.event.workflow_run.event == 'pull_request'" in workflow
    assert "github.event.workflow_run.pull_requests[0].number" in workflow
    assert "github.event.workflow_run.head_sha" in workflow
    assert "github.event.workflow_run.name" in workflow
    assert "github.event.workflow_run.workflow_id" in workflow
    assert "github.event.workflow_run.id" in workflow
    assert "python scripts/cancel_superseded_pr_workflow_runs_scoped.py" in workflow
    assert '--workflow-id "${{ github.event.workflow_run.workflow_id }}"' in workflow
    assert "--admission-only" not in workflow


def test_controller_bounds_pending_work_per_source_workflow() -> None:
    workflow = _text()

    concurrency = workflow.split("concurrency:", 1)[1].split("jobs:", 1)[0]
    assert "group: pr-qualification-supersession-" in concurrency
    assert "github.event.workflow_run.workflow_id" in concurrency
    assert "github.event.workflow_run.pull_requests" not in concurrency
    assert "github.event.workflow_run.head_sha" not in concurrency
    assert "cancel-in-progress: false" in concurrency
    assert "github.event.workflow_run.event == 'pull_request'" in concurrency
    assert "format('non-pr-{0}', github.event.workflow_run.id)" in concurrency
    assert "O(source workflows)" in workflow
    assert "reconciles its source workflow" in workflow


def test_delayed_stale_head_controller_reconciles_live_head_without_killing_running_controller() -> None:
    workflow = _text()

    concurrency = workflow.split("concurrency:", 1)[1].split("jobs:", 1)[0]
    assert "github.event.workflow_run.head_sha" not in concurrency
    assert "delayed stale-head event" in workflow
    assert "reconciles its source workflow" in workflow
    assert "cancel-in-progress: false" in concurrency


def test_explicit_trigger_pr_identity_is_not_scheduler_authority() -> None:
    workflow = _text()

    singleton_predicate = (
        "github.event.workflow_run.pull_requests[0].number && "
        "!github.event.workflow_run.pull_requests[1].number"
    )
    direct_identity = (
        singleton_predicate
        + " && github.event.workflow_run.pull_requests[0].number"
    )
    concurrency = workflow.split("concurrency:", 1)[1].split("jobs:", 1)[0]
    job = workflow.split("jobs:", 1)[1]

    assert singleton_predicate not in concurrency
    assert "github.event.workflow_run.pull_requests" not in concurrency
    assert direct_identity in job
    assert "PR metadata is deliberately not scheduler authority" in workflow
    assert "same-run/same-head singleton snapshot" in workflow
    assert "multi-reference remains permanently" in workflow


def test_empty_or_ambiguous_ref_controller_is_bounded_without_gaining_pr_authority() -> None:
    workflow = _text()

    concurrency = workflow.split("concurrency:", 1)[1].split("jobs:", 1)[0]
    assert "github.event.workflow_run.workflow_id" in concurrency
    assert "github.event.workflow_run.pull_requests" not in concurrency
    assert "github.event.workflow_run.event == 'pull_request'" in concurrency
    assert "format('non-pr-{0}', github.event.workflow_run.id)" in concurrency
    assert "Empty/multi-reference source" in workflow
    assert "cannot grant PR cancellation authority" in workflow
    assert "Historical recovery remains" in workflow
    assert "fail-closed and re-resolves identity" in workflow


def test_controller_does_not_skip_close_merge_run_when_pr_identity_is_unresolved() -> None:
    workflow = _text()
    job = workflow.split("jobs:", 1)[1]

    assert "if: github.event.workflow_run.event == 'pull_request'" in job
    assert "pull_requests[0].number != null" not in job
    assert '--pr-number "${{' in job
    assert "github.event.workflow_run.pull_requests[1].number" in job
    assert "|| 0 }}\"" in job
    assert '--event-pr-reference-mode "${{' in job
    assert "pull_requests[1].number && 'ambiguous'" in job
    assert "pull_requests[0].number && 'singleton' || 'empty'" in job
    assert "resolves identity" in workflow


def test_controller_coalesces_across_prs_but_not_source_workflows() -> None:
    workflow = _text()
    concurrency = workflow.split("concurrency:", 1)[1].split("jobs:", 1)[0]

    assert "github.event.workflow_run.pull_requests" not in concurrency
    assert "github.event.workflow_run.workflow_id" in concurrency
    assert "sweeps every explicit singleton PR group" in workflow
    assert "Different source workflows" in workflow
    assert "remain isolated by workflow_id" in workflow


def test_scoped_controller_reconciles_each_pr_against_fresh_live_qualification() -> None:
    source = Path("scripts/cancel_superseded_pr_workflow_runs_scoped.py").read_text(
        encoding="utf-8"
    )

    assert "cancel_superseded_explicit_pr_runs(" in source
    assert "current_qualification = api.live_pr_qualification(pr_number)" in source
    assert "current_qualification != qualification" in source


class FakeTriggeringRunApi:
    def __init__(self, qualifications: list[PullRequestQualification]) -> None:
        self._qualifications = list(qualifications)
        self.cancelled: list[int] = []

    def live_pr_qualification(self, pr_number: int) -> PullRequestQualification:
        assert pr_number == 2022
        if not self._qualifications:
            raise AssertionError("unexpected qualification reread")
        return self._qualifications.pop(0)

    def cancel(self, run_id: int) -> None:
        self.cancelled.append(run_id)


def test_trusted_controller_cancels_stable_same_head_nonqualifying_source_run() -> None:
    qualification = PullRequestQualification(
        head_sha="a" * 40,
        integration_capable=False,
    )
    api = FakeTriggeringRunApi([qualification])

    assert _cancel_triggering_run_if_stale_or_nonqualifying(
        api,
        pr_number=2022,
        event_head_sha="a" * 40,
        current_run_id=7001,
        qualification=qualification,
    )
    assert api.cancelled == [7001]


def test_trusted_controller_does_not_cancel_after_live_qualification_becomes_capable() -> None:
    initial = PullRequestQualification(
        head_sha="a" * 40,
        integration_capable=False,
    )
    api = FakeTriggeringRunApi(
        [
            PullRequestQualification(
                head_sha="a" * 40,
                integration_capable=True,
            )
        ]
    )

    assert not _cancel_triggering_run_if_stale_or_nonqualifying(
        api,
        pr_number=2022,
        event_head_sha="a" * 40,
        current_run_id=7002,
        qualification=initial,
    )
    assert api.cancelled == []


def test_trusted_controller_cancels_stale_trigger_after_live_qualification_reread() -> None:
    qualification = PullRequestQualification(
        head_sha="b" * 40,
        integration_capable=True,
    )
    api = FakeTriggeringRunApi([qualification])

    assert _cancel_triggering_run_if_stale_or_nonqualifying(
        api,
        pr_number=2022,
        event_head_sha="a" * 40,
        current_run_id=7003,
        qualification=qualification,
    )
    assert api.cancelled == [7003]


def test_stale_trigger_cancellation_is_revoked_if_live_qualification_moves_again() -> None:
    qualification = PullRequestQualification(
        head_sha="b" * 40,
        integration_capable=True,
    )
    api = FakeTriggeringRunApi(
        [
            PullRequestQualification(
                head_sha="c" * 40,
                integration_capable=True,
            )
        ]
    )

    assert not _cancel_triggering_run_if_stale_or_nonqualifying(
        api,
        pr_number=2022,
        event_head_sha="a" * 40,
        current_run_id=7004,
        qualification=qualification,
    )
    assert api.cancelled == []


def test_zero_trigger_identity_leaves_current_run_eligible_for_orphan_cleanup(
    monkeypatch,
) -> None:
    events: list[str] = []

    class FakeScopedApi:
        def __init__(self, **kwargs) -> None:
            events.append("api")

        def active_runs(self) -> tuple[WorkflowRun, ...]:
            events.append("snapshot")
            return ()

        def cancel_historical_unbound_runs(self, **kwargs) -> tuple[int, ...]:
            assert kwargs["exclude_run_ids"] == ()
            events.append("orphan")
            return ()

    monkeypatch.setattr(scoped_controller, "WorkflowScopedGitHubApi", FakeScopedApi)

    def sweep(*args, **kwargs) -> tuple[int, ...]:
        events.append("sweep")
        return ()

    monkeypatch.setattr(
        scoped_controller,
        "cancel_superseded_explicit_pr_runs",
        sweep,
    )
    monkeypatch.setattr(
        scoped_controller,
        "_cancel_triggering_run_if_stale_or_nonqualifying",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ambiguous trigger must not gain PR cancellation authority")
        ),
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "Oleksii-debug/Autosport")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    assert scoped_controller.main(
        [
            "--pr-number",
            "0",
            "--event-pr-reference-mode",
            "empty",
            "--event-head-sha",
            "a" * 40,
            "--workflow-name",
            "CI",
            "--workflow-id",
            "356678400",
            "--current-run-id",
            "7000",
        ]
    ) == 0
    assert events == ["api", "snapshot", "sweep", "orphan"]


def test_zero_trigger_orphan_cleanup_excludes_only_already_swept_runs(
    monkeypatch,
) -> None:
    events: list[str] = []

    class FakeScopedApi:
        def __init__(self, **kwargs) -> None:
            events.append("api")

        def active_runs(self) -> tuple[WorkflowRun, ...]:
            events.append("snapshot")
            return ()

        def cancel_historical_unbound_runs(self, **kwargs) -> tuple[int, ...]:
            assert kwargs["exclude_run_ids"] == (6001, 6002)
            events.append("orphan")
            return ()

    monkeypatch.setattr(scoped_controller, "WorkflowScopedGitHubApi", FakeScopedApi)

    def sweep(*args, **kwargs) -> tuple[int, ...]:
        events.append("sweep")
        return (6001, 6002)

    monkeypatch.setattr(
        scoped_controller,
        "cancel_superseded_explicit_pr_runs",
        sweep,
    )
    monkeypatch.setattr(
        scoped_controller,
        "_cancel_triggering_run_if_stale_or_nonqualifying",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ambiguous trigger must not gain PR cancellation authority")
        ),
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "Oleksii-debug/Autosport")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    assert scoped_controller.main(
        [
            "--pr-number",
            "0",
            "--event-pr-reference-mode",
            "empty",
            "--event-head-sha",
            "a" * 40,
            "--workflow-name",
            "CI",
            "--workflow-id",
            "356678400",
            "--current-run-id",
            "7000",
        ]
    ) == 0
    assert events == ["api", "snapshot", "sweep", "orphan"]


def test_zero_trigger_recovers_consistent_snapshot_identity_for_boundary_cancel(
    monkeypatch,
) -> None:
    events: list[str] = []
    stale_qualification = PullRequestQualification(
        head_sha="b" * 40,
        integration_capable=True,
    )

    class FakeScopedApi:
        def __init__(self, **kwargs) -> None:
            self._workflow_name = "CI"
            events.append("api")

        def active_runs(self) -> tuple[WorkflowRun, ...]:
            events.append("snapshot")
            return (
                WorkflowRun(
                    run_id=7000,
                    head_sha="a" * 40,
                    workflow_name="CI",
                    pr_numbers=(2022,),
                    status="queued",
                ),
            )

        def live_pr_qualification(self, pr_number: int) -> PullRequestQualification:
            assert pr_number == 2022
            events.append("qualification")
            return stale_qualification

        def cancel_historical_unbound_runs(self, **kwargs) -> tuple[int, ...]:
            assert kwargs["exclude_run_ids"] == (7000,)
            events.append("orphan")
            return ()

        def cancel(self, run_id: int) -> None:
            assert run_id == 7000
            events.append("cancel")

    monkeypatch.setattr(scoped_controller, "WorkflowScopedGitHubApi", FakeScopedApi)
    monkeypatch.setenv("GITHUB_REPOSITORY", "Oleksii-debug/Autosport")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    assert scoped_controller.main(
        [
            "--pr-number",
            "0",
            "--event-pr-reference-mode",
            "empty",
            "--event-head-sha",
            "a" * 40,
            "--workflow-name",
            "CI",
            "--workflow-id",
            "356678400",
            "--current-run-id",
            "7000",
        ]
    ) == 0
    assert events == [
        "api",
        "snapshot",
        "orphan",
        "qualification",
        "qualification",
        "cancel",
    ]


def test_explicit_stale_trigger_is_cancelled_once_after_orphan_exclusion(
    monkeypatch,
) -> None:
    qualification = PullRequestQualification(
        head_sha="b" * 40,
        integration_capable=True,
    )
    instances = []

    class FakeScopedApi:
        def __init__(self, **kwargs) -> None:
            self.cancelled: list[int] = []
            instances.append(self)

        def live_pr_qualification(self, pr_number: int) -> PullRequestQualification:
            assert pr_number == 2022
            return qualification

        def cancel_historical_unbound_runs(self, **kwargs) -> tuple[int, ...]:
            excluded = set(kwargs["exclude_run_ids"])
            assert excluded == {7005}
            # Reproduce the metadata race: the current source run appears orphaned
            # in the Actions snapshot. It must not be consumed by this phase.
            if 7005 not in excluded:
                self.cancel(7005)
                return (7005,)
            return ()

        def cancel(self, run_id: int) -> None:
            self.cancelled.append(run_id)

    monkeypatch.setattr(scoped_controller, "WorkflowScopedGitHubApi", FakeScopedApi)
    monkeypatch.setattr(
        scoped_controller,
        "cancel_superseded_explicit_pr_runs",
        lambda *args, **kwargs: (),
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "Oleksii-debug/Autosport")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    assert scoped_controller.main(
        [
            "--pr-number",
            "2022",
            "--event-pr-reference-mode",
            "singleton",
            "--event-head-sha",
            "a" * 40,
            "--workflow-name",
            "CI",
            "--workflow-id",
            "356678400",
            "--current-run-id",
            "7005",
        ]
    ) == 0
    assert len(instances) == 1
    assert instances[0].cancelled == [7005]


def test_trigger_initial_qualification_failure_preserves_completed_cleanup(
    monkeypatch,
) -> None:
    events: list[str] = []

    class FakeScopedApi:
        def __init__(self, **kwargs) -> None:
            events.append("api")

        def live_pr_qualification(self, pr_number: int) -> PullRequestQualification:
            assert pr_number == 2022
            events.append("trigger-qualification-failed")
            raise CancellationError("fixture trigger qualification unavailable")

        def cancel_historical_unbound_runs(self, **kwargs) -> tuple[int, ...]:
            assert kwargs["exclude_run_ids"] == (7005,)
            events.append("orphan")
            return ()

    monkeypatch.setattr(scoped_controller, "WorkflowScopedGitHubApi", FakeScopedApi)

    def sweep(*args, **kwargs) -> tuple[int, ...]:
        events.append("sweep")
        return ()

    monkeypatch.setattr(
        scoped_controller,
        "cancel_superseded_explicit_pr_runs",
        sweep,
    )
    monkeypatch.setattr(
        scoped_controller,
        "_cancel_triggering_run_if_stale_or_nonqualifying",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unqualified trigger must receive no cancellation authority")
        ),
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "Oleksii-debug/Autosport")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    assert scoped_controller.main(
        [
            "--pr-number",
            "2022",
            "--event-pr-reference-mode",
            "singleton",
            "--event-head-sha",
            "a" * 40,
            "--workflow-name",
            "CI",
            "--workflow-id",
            "356678400",
            "--current-run-id",
            "7005",
        ]
    ) == 0
    assert events == [
        "api",
        "sweep",
        "orphan",
        "trigger-qualification-failed",
    ]


def test_main_reaches_triggering_run_check_after_orphan_authority_race_skip(
    monkeypatch,
) -> None:
    events: list[str] = []
    qualification = PullRequestQualification(
        head_sha="b" * 40,
        integration_capable=True,
    )

    class FakeScopedApi:
        def __init__(self, **kwargs) -> None:
            events.append("api")

        def live_pr_qualification(self, pr_number: int) -> PullRequestQualification:
            assert pr_number == 2022
            return qualification

        def configure_historical_candidate_recovery(self, **kwargs) -> None:
            events.append("historical-recovery")

        def configure_same_head_candidate_recovery(self, **kwargs) -> None:
            raise AssertionError("stale trigger must not configure same-head recovery")

        def cancel_historical_unbound_runs(self, **kwargs) -> tuple[int, ...]:
            events.append("orphan-authority-race-skipped")
            return ()

    class Result:
        cancelled_run_ids: tuple[int, ...] = ()

    monkeypatch.setattr(scoped_controller, "WorkflowScopedGitHubApi", FakeScopedApi)
    monkeypatch.setattr(
        scoped_controller,
        "cancel_superseded_explicit_pr_runs",
        lambda *args, **kwargs: (),
    )

    def trigger_check(*args, **kwargs) -> bool:
        events.append("triggering-run-check")
        return False

    monkeypatch.setattr(
        scoped_controller,
        "_cancel_triggering_run_if_stale_or_nonqualifying",
        trigger_check,
    )
    monkeypatch.setenv("GITHUB_REPOSITORY", "Oleksii-debug/Autosport")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    assert scoped_controller.main(
        [
            "--pr-number",
            "2022",
            "--event-pr-reference-mode",
            "singleton",
            "--event-head-sha",
            "a" * 40,
            "--workflow-name",
            "CI",
            "--workflow-id",
            "356678400",
            "--current-run-id",
            "7005",
        ]
    ) == 0
    assert events[-2:] == [
        "orphan-authority-race-skipped",
        "triggering-run-check",
    ]



def test_non_pr_source_events_cannot_evict_pending_pr_cleanup_controller() -> None:
    workflow = _text()
    concurrency = workflow.split("concurrency:", 1)[1].split("jobs:", 1)[0]

    assert "github.event.workflow_run.workflow_id" in concurrency
    assert "github.event.workflow_run.event == 'pull_request'" in concurrency
    assert "&& 'pr'" in concurrency
    assert "format('non-pr-{0}', github.event.workflow_run.id)" in concurrency
    assert "concurrency is evaluated before the job-level pull_request guard" in workflow
    assert "source run id is scheduler isolation only" in workflow
