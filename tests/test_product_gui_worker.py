from __future__ import annotations

import threading
from pathlib import Path

import pytest

from autosport.continuous_session import (
    ContinuousSessionStatus,
    ContinuousTickResult,
    SessionState,
)
from autosport.product_gui_worker import ProductGuiWorker


def _status(state: SessionState, *, cycles: int) -> ContinuousSessionStatus:
    return ContinuousSessionStatus(
        session_id="session-1",
        source_id="source-1",
        state=state,
        cycles_completed=cycles,
        last_success_at="2026-09-21T07:51:00+00:00" if cycles else None,
        last_error_code=None,
        last_full_refresh_at=None,
        settlement_evidence=(),
    )


def _tick() -> ContinuousTickResult:
    return ContinuousTickResult(
        session_id="session-1",
        cycle_index=1,
        source_id="source-1",
        source_provider_unavailable=False,
        source_gap_states=(),
        source_sync_states=(),
        committed_delta_ids=("delta-1",),
        delivered_delta_ids=("delta-1",),
        affected_input_ids=("market-1",),
        registered_input_ids=(),
        retired_input_ids=(),
        full_refresh_required=False,
        invalidation_backlog=False,
        settled_ticket_ids=(),
        settlement_evidence_ids=(),
        last_success_at="2026-09-21T07:51:00+00:00",
    )


class _FakeRuntime:
    def __init__(self) -> None:
        self.tick_called = threading.Event()
        self.stop_reason: str | None = None
        self.closed = False

    def start(self) -> ContinuousSessionStatus:
        return _status(SessionState.RUNNING, cycles=0)

    def tick(self) -> ContinuousTickResult:
        self.tick_called.set()
        return _tick()

    def stop(self, reason: str = "operator_stop") -> ContinuousSessionStatus:
        self.stop_reason = reason
        return _status(SessionState.STOPPED, cycles=1)

    def close(self) -> None:
        self.closed = True


def _drain(worker: ProductGuiWorker):
    messages = []
    while True:
        message = worker.poll()
        if message is None:
            return messages
        messages.append(message)


def test_worker_drives_existing_runtime_and_cooperatively_stops_without_sleep(tmp_path: Path) -> None:
    runtime = _FakeRuntime()
    captured: list[tuple[Path, str, str]] = []

    def build(workspace: Path, source_factory: str, bankroll: str):
        captured.append((workspace, source_factory, bankroll))
        return runtime

    worker = ProductGuiWorker(runtime_builder=build)
    assert worker.start(
        workspace=tmp_path,
        source_factory="provider.module:factory",
        initial_bankroll="10000",
        poll_seconds=60,
    )
    assert worker._thread is not None
    assert worker._thread.daemon is False
    assert runtime.tick_called.wait(2)
    assert worker.request_stop("operator_stop")
    assert worker.join(2)

    messages = _drain(worker)
    assert [message.kind for message in messages] == ["STARTED", "TICK", "STOPPED"]
    assert messages[-1].stop_reason == "operator_stop"
    assert runtime.stop_reason == "operator_stop"
    assert runtime.closed is True
    assert captured == [(tmp_path, "provider.module:factory", "10000")]
    assert worker.busy is False


def test_worker_publishes_error_type_only_and_never_exception_detail(tmp_path: Path) -> None:
    secret = "provider-token-should-never-reach-ui"

    def fail_build(_workspace: Path, _source_factory: str, _bankroll: str):
        raise RuntimeError(secret)

    worker = ProductGuiWorker(runtime_builder=fail_build)
    assert worker.start(
        workspace=tmp_path,
        source_factory="provider.module:factory",
        poll_seconds=60,
    )
    assert worker.join(2)

    messages = _drain(worker)
    assert len(messages) == 1
    assert messages[0].kind == "ERROR"
    assert messages[0].error_type == "RuntimeError"
    assert secret not in repr(messages[0])


@pytest.mark.parametrize(
    "source_factory,poll_seconds",
    [
        ("", 30),
        (" provider.module:factory", 30),
        ("provider.module:factory ", 30),
        ("provider.module:factory", 0),
        ("provider.module:factory", -1),
        ("provider.module:factory", float("nan")),
    ],
)
def test_worker_rejects_ambiguous_configuration_before_thread_start(
    tmp_path: Path,
    source_factory: str,
    poll_seconds: float,
) -> None:
    worker = ProductGuiWorker(runtime_builder=lambda *_args: _FakeRuntime())
    with pytest.raises(ValueError):
        worker.start(
            workspace=tmp_path,
            source_factory=source_factory,
            poll_seconds=poll_seconds,
        )
    assert worker.busy is False
