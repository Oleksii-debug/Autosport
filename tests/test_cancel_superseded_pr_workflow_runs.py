from __future__ import annotations

import pytest

from scripts.cancel_superseded_pr_workflow_runs import (
    CancellationError,
    WorkflowRun,
    cancel_superseded,
    select_superseded_runs,
)


HEAD_A = "a" * 40
HEAD_B = "b" * 40
HEAD_C = "c" * 40


def _run(
    run_id: int,
    head_sha: str,
    *,
    workflow_name: str = "CI",
    pr_numbers: tuple[int, ...] = (2008,),
) -> WorkflowRun:
    return WorkflowRun(
        run_id=run_id,
        head_sha=head_sha,
        workflow_name=workflow_name,
        pr_numbers=pr_numbers,
        status="in_progress",
    )


def test_selects_only_superseded_runs_for_same_pr_and_workflow() -> None:
    runs = (
        _run(10, HEAD_A),
        _run(11, HEAD_B),
        _run(12, HEAD_A, workflow_name="Windows candidate"),
        _run(13, HEAD_A, pr_numbers=(999,)),
    )
    assert select_superseded_runs(
        runs,
        pr_number=2008,
        live_head_sha=HEAD_B,
        workflow_name="CI",
        current_run_id=11,
    ) == (10,)


def test_current_run_is_never_selected_even_if_payload_is_inconsistent() -> None:
    assert select_superseded_runs(
        (_run(21, HEAD_A),),
        pr_number=2008,
        live_head_sha=HEAD_B,
        workflow_name="CI",
        current_run_id=21,
    ) == ()


class FakeApi:
    def __init__(self, heads: list[str], runs: tuple[WorkflowRun, ...]) -> None:
        self._heads = list(heads)
        self._runs = runs
        self.active_calls = 0
        self.cancelled: list[int] = []

    def live_pr_head(self, pr_number: int) -> str:
        assert pr_number == 2008
        if not self._heads:
            raise AssertionError("unexpected live-head read")
        return self._heads.pop(0)

    def active_runs(self) -> tuple[WorkflowRun, ...]:
        self.active_calls += 1
        return self._runs

    def cancel(self, run_id: int) -> None:
        self.cancelled.append(run_id)


def test_stale_rerun_has_zero_cancellation_authority() -> None:
    api = FakeApi([HEAD_B], (_run(30, HEAD_B),))
    assert cancel_superseded(
        api=api,
        pr_number=2008,
        event_head_sha=HEAD_A,
        workflow_name="CI",
        current_run_id=29,
    ) == ()
    assert api.active_calls == 0
    assert api.cancelled == []


def test_current_head_cancels_only_older_same_workflow_runs() -> None:
    api = FakeApi(
        [HEAD_B],
        (
            _run(40, HEAD_A),
            _run(41, HEAD_B),
            _run(42, HEAD_C, workflow_name="Windows candidate"),
        ),
    )
    assert cancel_superseded(
        api=api,
        pr_number=2008,
        event_head_sha=HEAD_B,
        workflow_name="CI",
        current_run_id=41,
    ) == (40,)
    assert api.cancelled == [40]


def test_invalid_sha_fails_closed() -> None:
    with pytest.raises(CancellationError):
        select_superseded_runs(
            (),
            pr_number=2008,
            live_head_sha="not-a-sha",
            workflow_name="CI",
            current_run_id=1,
        )
