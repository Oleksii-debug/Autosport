from __future__ import annotations

from pathlib import Path

from scripts.cancel_superseded_pr_workflow_runs import PullRequestQualification
from scripts.cancel_superseded_pr_workflow_runs_scoped import (
    _cancel_triggering_run_if_nonqualifying,
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


def test_controller_coalesces_only_same_pr_same_head_same_workflow_decision() -> None:
    workflow = _text()

    concurrency = workflow.split("concurrency:", 1)[1].split("jobs:", 1)[0]
    assert "group: pr-qualification-supersession-" in concurrency
    assert "github.event.workflow_run.pull_requests[0].number" in concurrency
    assert "github.event.workflow_run.head_sha" in concurrency
    assert "github.event.workflow_run.workflow_id" in concurrency
    assert "cancel-in-progress: true" in concurrency
    assert "fresh" in workflow
    assert "head/state/draft" in workflow


def test_delayed_stale_head_controller_cannot_preempt_current_head_controller() -> None:
    workflow = _text()

    concurrency = workflow.split("concurrency:", 1)[1].split("jobs:", 1)[0]
    assert "github.event.workflow_run.head_sha" in concurrency
    assert "delayed stale-head workflow_run event must never evict" in workflow
    assert "latest-wins" in workflow


def test_explicit_pr_identity_is_used_only_for_a_singleton_event_reference() -> None:
    workflow = _text()

    singleton_guard = (
        "github.event.workflow_run.pull_requests[0].number && "
        "!github.event.workflow_run.pull_requests[1].number && "
        "github.event.workflow_run.pull_requests[0].number"
    )
    concurrency = workflow.split("concurrency:", 1)[1].split("jobs:", 1)[0]
    job = workflow.split("jobs:", 1)[1]

    assert singleton_guard in concurrency
    assert singleton_guard in job
    assert "array" in workflow
    assert "arbitrary first array member" in workflow


def test_empty_or_ambiguous_ref_controller_is_run_unique_until_identity_is_resolved() -> None:
    workflow = _text()

    concurrency = workflow.split("concurrency:", 1)[1].split("jobs:", 1)[0]
    assert "format('unresolved-run-{0}', github.event.workflow_run.id)" in concurrency
    assert "github.event.workflow_run.pull_requests[1].number" in concurrency
    assert "github.event.workflow_run.head_sha" in concurrency
    assert "two distinct PRs may point" in workflow
    assert "empty or multi-reference payload is unresolved" in workflow
    assert "run-unique" in workflow


def test_controller_does_not_skip_close_merge_run_when_pr_identity_is_unresolved() -> None:
    workflow = _text()
    job = workflow.split("jobs:", 1)[1]

    assert "if: github.event.workflow_run.event == 'pull_request'" in job
    assert "pull_requests[0].number != null" not in job
    assert '--pr-number "${{' in job
    assert "github.event.workflow_run.pull_requests[1].number" in job
    assert "|| 0 }}\"" in job
    assert "unique" in workflow


def test_controller_does_not_cross_cancel_other_source_workflow_controllers() -> None:
    workflow = _text()

    assert "Controllers for CI, Windows candidate, and Endurance must not" in workflow
    assert "preempt one another" in workflow.replace("\n# ", " ")
    assert "each invocation cancels only obsolete runs of its own" in workflow
    assert "source workflow" in workflow
    assert "exact workflow_id carried by workflow_run" in workflow


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

    assert _cancel_triggering_run_if_nonqualifying(
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

    assert not _cancel_triggering_run_if_nonqualifying(
        api,
        pr_number=2022,
        event_head_sha="a" * 40,
        current_run_id=7002,
        qualification=initial,
    )
    assert api.cancelled == []


def test_trusted_controller_does_not_cancel_stale_event_head() -> None:
    qualification = PullRequestQualification(
        head_sha="b" * 40,
        integration_capable=False,
    )
    api = FakeTriggeringRunApi([])

    assert not _cancel_triggering_run_if_nonqualifying(
        api,
        pr_number=2022,
        event_head_sha="a" * 40,
        current_run_id=7003,
        qualification=qualification,
    )
    assert api.cancelled == []
