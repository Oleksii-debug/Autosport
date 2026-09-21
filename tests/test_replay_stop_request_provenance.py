from __future__ import annotations

from autosport.replay import ReplayStopRequested
from autosport.replay_worker import OneShotReplayWorker


def test_task_cannot_self_mint_operator_stop_without_accepted_request() -> None:
    worker = OneShotReplayWorker()

    def task():
        raise ReplayStopRequested("task-forged stop")

    assert worker.start(task) is True
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
