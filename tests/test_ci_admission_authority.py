from __future__ import annotations

import pytest

import scripts.cancel_superseded_pr_workflow_runs as controller_module
from scripts.cancel_superseded_pr_workflow_runs import CancellationError, CancellationResult


HEAD_A = "a" * 40
HEAD_B = "b" * 40


def test_admission_rejects_inflight_qualification_state_reader_rebind(
    monkeypatch,
) -> None:
    forged_calls: list[object] = []

    def forged_reader(qualification: object) -> tuple[str, bool]:
        forged_calls.append(qualification)
        return HEAD_A, True

    class FakeApi:
        def live_pr_qualification(self, pr_number: int) -> tuple[str, bool]:
            assert pr_number == 2008
            monkeypatch.setattr(
                controller_module,
                "_pull_request_qualification_state",
                forged_reader,
            )
            return HEAD_B, True

    with pytest.raises(
        CancellationError,
        match="pull request qualification snapshot authority changed",
    ):
        controller_module.admit_current_head(
            api=FakeApi(),
            pr_number=2008,
            event_head_sha=HEAD_A,
        )

    assert forged_calls == []


def test_admission_rejects_inflight_result_constructor_rebind(monkeypatch) -> None:
    forged_calls: list[tuple[bool, tuple[int, ...]]] = []

    class ForgedResult:
        def __init__(
            self,
            *,
            current_head: bool,
            cancelled_run_ids: tuple[int, ...],
        ) -> None:
            forged_calls.append((current_head, cancelled_run_ids))
            self.current_head = True
            self.cancelled_run_ids = ()

    class FakeApi:
        def live_pr_qualification(self, pr_number: int) -> tuple[str, bool]:
            assert pr_number == 2008
            monkeypatch.setattr(
                controller_module,
                "CancellationResult",
                ForgedResult,
            )
            return HEAD_B, True

    with pytest.raises(
        CancellationError,
        match="pull request admission authority changed",
    ):
        controller_module.admit_current_head(
            api=FakeApi(),
            pr_number=2008,
            event_head_sha=HEAD_B,
        )

    assert forged_calls == []


def test_admission_ignores_rebound_event_sha_helper(monkeypatch) -> None:
    monkeypatch.setattr(
        controller_module,
        "_require_sha",
        lambda _value, *, field: HEAD_B,
    )

    class FakeApi:
        def live_pr_qualification(self, pr_number: int) -> tuple[str, bool]:
            assert pr_number == 2008
            return HEAD_B, True

    assert controller_module.admit_current_head(
        api=FakeApi(),
        pr_number=2008,
        event_head_sha=HEAD_A,
    ) == CancellationResult(
        current_head=False,
        cancelled_run_ids=(),
    )


def test_admission_rejects_preentry_snapshot_helper_rebind(monkeypatch) -> None:
    forged_calls: list[int] = []

    def forged_snapshot(api, pr_number: int, **_kwargs) -> tuple[str, bool]:
        del api
        forged_calls.append(pr_number)
        return HEAD_A, True

    monkeypatch.setattr(
        controller_module,
        "_qualification_snapshot",
        forged_snapshot,
    )

    class FakeApi:
        def live_pr_qualification(self, pr_number: int) -> tuple[str, bool]:
            raise AssertionError(f"forged pre-entry path must fail first: {pr_number}")

    with pytest.raises(
        CancellationError,
        match="pull request admission authority changed",
    ):
        controller_module.admit_current_head(
            api=FakeApi(),
            pr_number=2008,
            event_head_sha=HEAD_A,
        )

    assert forged_calls == []
