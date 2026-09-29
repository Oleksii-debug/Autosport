from __future__ import annotations

from scripts.cancel_superseded_pr_workflow_runs import (
    CancellationResult,
    GitHubApi,
    WorkflowRun,
    admit_current_head,
    cancel_superseded,
)


HEAD_A = "a" * 40
HEAD_B = "b" * 40


def _payload(
    head_sha: str,
    *,
    state: str = "open",
    draft: bool = False,
) -> dict[str, object]:
    return {
        "head": {"sha": head_sha},
        "state": state,
        "draft": draft,
    }


class _AdmissionApi(GitHubApi):
    def __init__(self, payload: object) -> None:
        super().__init__(repository="owner/repo", token="token")
        self._payload = payload

    def _request(self, path: str, **kwargs: object) -> object:
        assert path == "/pulls/2016"
        assert not kwargs
        return self._payload


def test_same_head_ready_event_is_rejected_after_live_pr_returns_to_draft() -> None:
    api = _AdmissionApi(_payload(HEAD_B, draft=True))

    assert admit_current_head(
        api=api,
        pr_number=2016,
        event_head_sha=HEAD_B,
    ) == CancellationResult(current_head=False, cancelled_run_ids=())


def test_same_head_ready_event_is_rejected_after_live_pr_closes() -> None:
    api = _AdmissionApi(_payload(HEAD_B, state="closed", draft=False))

    assert admit_current_head(
        api=api,
        pr_number=2016,
        event_head_sha=HEAD_B,
    ) == CancellationResult(current_head=False, cancelled_run_ids=())


class _LifecycleRaceApi(GitHubApi):
    def __init__(
        self,
        payloads: list[object],
        runs: tuple[WorkflowRun, ...],
    ) -> None:
        super().__init__(repository="owner/repo", token="token")
        self._payloads = list(payloads)
        self._runs = runs
        self.cancelled: list[int] = []

    def _request(self, path: str, **kwargs: object) -> object:
        assert path == "/pulls/2016"
        assert not kwargs
        if not self._payloads:
            raise AssertionError("unexpected qualification snapshot read")
        return self._payloads.pop(0)

    def active_runs(self) -> tuple[WorkflowRun, ...]:
        return self._runs

    def cancel(self, run_id: int) -> None:
        self.cancelled.append(run_id)


def _run(run_id: int, head_sha: str) -> WorkflowRun:
    return WorkflowRun(
        run_id=run_id,
        head_sha=head_sha,
        workflow_name="CI",
        pr_numbers=(2016,),
        status="queued",
    )


def test_same_head_draft_transition_after_listing_revokes_cancellation_authority() -> None:
    api = _LifecycleRaceApi(
        [
            _payload(HEAD_B, draft=False),
            _payload(HEAD_B, draft=True),
        ],
        (_run(10, HEAD_A),),
    )

    result = cancel_superseded(
        api=api,
        pr_number=2016,
        event_head_sha=HEAD_B,
        workflow_name="CI",
        current_run_id=11,
    )

    assert result == CancellationResult(current_head=False, cancelled_run_ids=())
    assert api.cancelled == []


def test_same_head_ready_transition_after_listing_revokes_draft_cleanup() -> None:
    api = _LifecycleRaceApi(
        [
            _payload(HEAD_B, draft=True),
            _payload(HEAD_B, draft=False),
        ],
        (_run(10, HEAD_B),),
    )

    result = cancel_superseded(
        api=api,
        pr_number=2016,
        event_head_sha=HEAD_B,
        workflow_name="CI",
        current_run_id=11,
    )

    assert result == CancellationResult(current_head=False, cancelled_run_ids=())
    assert api.cancelled == []
