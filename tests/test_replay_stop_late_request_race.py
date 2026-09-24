from __future__ import annotations

import threading

from autosport.replay import ReplayStopRequested
from autosport.replay_worker import OneShotReplayWorker


def test_forged_stop_closes_late_request_window_before_classification() -> None:
    worker = OneShotReplayWorker()
    task_ready = threading.Event()
    raise_forged_stop = threading.Event()
    first_disarm = threading.Event()
    release_first_disarm = threading.Event()

    def task():
        task_ready.set()
        if not raise_forged_stop.wait(2.0):
            raise AssertionError("timed out waiting to raise forged STOP")
        raise ReplayStopRequested("task-forged stop")

    assert worker.start(task) is True
    assert task_ready.wait(1.0)

    token = worker._stop_token
    assert token is not None
    original_disarm = token.disarm
    disarm_calls = 0

    def observed_disarm() -> None:
        nonlocal disarm_calls
        original_disarm()
        disarm_calls += 1
        if disarm_calls == 1:
            first_disarm.set()
            if not release_first_disarm.wait(2.0):
                raise AssertionError("timed out holding first disarm")

    token.disarm = observed_disarm
    raise_forged_stop.set()

    assert first_disarm.wait(1.0)
    try:
        assert worker.request_stop() is False
    finally:
        release_first_disarm.set()

    thread = worker._thread
    assert thread is not None
    thread.join(timeout=2.0)
    assert not thread.is_alive()

    message = worker.poll()
    assert message is not None
    assert message.stopped is False
    assert message.result is None
    assert message.error is not None
    assert "ReplayStopRequested" in message.error
