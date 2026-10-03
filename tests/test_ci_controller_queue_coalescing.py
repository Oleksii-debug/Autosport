from __future__ import annotations

from pathlib import Path

from scripts.cancel_superseded_pr_workflow_runs import (
    PullRequestQualification,
    WorkflowRun,
)
from scripts.cancel_superseded_pr_workflow_runs_scoped import (
    _cancel_triggering_run_if_stale_or_nonqualifying,
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


def test_controller_scheduler_coalesces_all_prs_per_source_workflow() -> None:
    text = Path(".github/workflows/pr-qualification-supersession.yml").read_text(
        encoding="utf-8"
    )
    concurrency = text.split("concurrency:", 1)[1].split("jobs:", 1)[0]

    assert "github.event.workflow_run.workflow_id" in concurrency
    assert "github.event.workflow_run.pull_requests" not in concurrency
    assert "github.event.workflow_run.head_sha" not in concurrency
    assert "cancel-in-progress: false" in concurrency


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

    def active_runs(self) -> tuple[WorkflowRun, ...]:
        return self._runs

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


def test_controller_main_uses_workflow_wide_sweep_and_trigger_boundary() -> None:
    text = Path("scripts/cancel_superseded_pr_workflow_runs_scoped.py").read_text(
        encoding="utf-8"
    )
    assert "cancel_superseded_explicit_pr_runs(" in text
    assert "runs = api.active_runs()" in text
    assert "_cancel_triggering_run_if_stale_or_nonqualifying(" in text
