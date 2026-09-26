from __future__ import annotations

from scripts.cancel_superseded_pr_workflow_runs import (
    CancellationResult,
    WorkflowRun,
    admit_current_head,
    has_older_current_head_attempt,
)


HEAD = "a" * 40


def _run(run_id: int, *, head_sha: str = HEAD, workflow: str = "CI") -> WorkflowRun:
    return WorkflowRun(
        run_id=run_id,
        head_sha=head_sha,
        workflow_name=workflow,
        pr_numbers=(2008,),
        status="in_progress",
    )


class FakeApi:
    def __init__(self, heads: list[str], runs: tuple[WorkflowRun, ...]) -> None:
        self._heads = list(heads)
        self._runs = runs
        self.active_calls = 0

    def live_pr_head(self, pr_number: int) -> str:
        assert pr_number == 2008
        return self._heads.pop(0)

    def active_runs(self) -> tuple[WorkflowRun, ...]:
        self.active_calls += 1
        return self._runs


def test_older_active_exact_head_attempt_suppresses_later_duplicate() -> None:
    runs = (_run(100), _run(101))
    assert has_older_current_head_attempt(
        runs,
        pr_number=2008,
        live_head_sha=HEAD,
        workflow_name="CI",
        current_run_id=101,
    ) is True

    api = FakeApi([HEAD, HEAD], runs)
    assert admit_current_head(
        api=api,
        pr_number=2008,
        event_head_sha=HEAD,
        workflow_name="CI",
        current_run_id=101,
        dedupe_same_head=True,
    ) == CancellationResult(current_head=False, cancelled_run_ids=())
    assert api.active_calls == 1


def test_oldest_active_exact_head_attempt_keeps_gate_authority() -> None:
    runs = (_run(100), _run(101))
    api = FakeApi([HEAD, HEAD], runs)
    assert admit_current_head(
        api=api,
        pr_number=2008,
        event_head_sha=HEAD,
        workflow_name="CI",
        current_run_id=100,
        dedupe_same_head=True,
    ) == CancellationResult(current_head=True, cancelled_run_ids=())


def test_head_only_admission_does_not_require_actions_read() -> None:
    runs = (_run(100), _run(101))
    api = FakeApi([HEAD], runs)
    assert admit_current_head(
        api=api,
        pr_number=2008,
        event_head_sha=HEAD,
        workflow_name="Windows candidate",
        current_run_id=101,
        dedupe_same_head=False,
    ) == CancellationResult(current_head=True, cancelled_run_ids=())
    assert api.active_calls == 0


def test_other_workflow_or_other_head_does_not_suppress_current_attempt() -> None:
    other_head = "b" * 40
    runs = (
        _run(90, head_sha=other_head),
        _run(91, workflow="Windows candidate"),
    )
    assert has_older_current_head_attempt(
        runs,
        pr_number=2008,
        live_head_sha=HEAD,
        workflow_name="CI",
        current_run_id=100,
    ) is False
