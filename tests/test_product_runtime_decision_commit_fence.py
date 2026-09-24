from __future__ import annotations

from threading import Event, Thread
from types import SimpleNamespace

import pytest

from autosport.continuous_session import SessionState
from autosport.product_runtime import (
    AutonomousProductRuntime,
    ProductCompositionError,
    ProductCompositionManifest,
)


class _Lease:
    def __init__(self) -> None:
        self.authority_active = True

    def release(self) -> None:
        self.authority_active = False


class _Collector:
    def __init__(self, stop_called: Event) -> None:
        self._stop_called = stop_called
        self._stopped = False

    def status(self):
        return {
            "stopped_at": "2026-09-22T14:00:00+00:00" if self._stopped else None,
            "stop_reason": "operator_stop" if self._stopped else None,
        }

    def resume(self) -> None:
        self._stopped = False

    def stop(self, reason: str) -> None:
        assert reason
        self._stop_called.set()
        self._stopped = True


class _Coordinator:
    def __init__(self) -> None:
        self.state = SessionState.RUNNING

    def status(self):
        return SimpleNamespace(state=self.state)

    def resume(self) -> None:
        self.state = SessionState.RUNNING

    def pause(self) -> None:
        self.state = SessionState.PAUSED

    def stop(self, reason: str) -> None:
        assert reason
        self.state = SessionState.STOPPED


class _MarketStore:
    def close(self) -> None:
        return None


class _StartTransitionStore:
    def pending(self):
        return None


def _runtime(tmp_path, *, stop_called: Event) -> AutonomousProductRuntime:
    return AutonomousProductRuntime(
        workspace=tmp_path,
        manifest=ProductCompositionManifest(
            source_id="provider-a",
            initial_bankroll="100",
        ),
        coordinator=_Coordinator(),
        collector=_Collector(stop_called),
        market_store=_MarketStore(),
        lifecycle=SimpleNamespace(),
        mirror=SimpleNamespace(),
        invalidations=SimpleNamespace(),
        dependencies=SimpleNamespace(),
        _runtime_lease=_Lease(),
        _start_transition_store=_StartTransitionStore(),
    )


def test_stop_waits_until_already_admitted_decision_commit_fence_releases(
    tmp_path,
) -> None:
    stop_called = Event()
    stop_thread_started = Event()
    runtime = _runtime(tmp_path, stop_called=stop_called)
    errors: list[BaseException] = []

    def _stop() -> None:
        stop_thread_started.set()
        try:
            runtime.stop("operator_stop")
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    with runtime.decision_commit_fence():
        thread = Thread(target=_stop)
        thread.start()
        assert stop_thread_started.wait(1)
        assert not stop_called.wait(0.05)
        assert runtime.status().state is SessionState.RUNNING

    assert stop_called.wait(1)
    thread.join(1)
    assert not thread.is_alive()
    assert errors == []
    assert runtime.status().state is SessionState.STOPPED


def test_stop_winning_first_blocks_later_decision_commit_fence(tmp_path) -> None:
    stop_called = Event()
    runtime = _runtime(tmp_path, stop_called=stop_called)

    runtime.stop("operator_stop")
    assert stop_called.is_set()

    with pytest.raises(
        ProductCompositionError,
        match="PAPER decision commit requires the canonical product runtime to remain running",
    ):
        with runtime.decision_commit_fence():
            raise AssertionError("STOPPED runtime must never enter decision commit body")
