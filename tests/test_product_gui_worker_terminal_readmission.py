from __future__ import annotations

import threading
from pathlib import Path

from autosport.product_gui_worker import ProductGuiWorker


class _FailingRuntime:
    def start(self) -> object:
        raise RuntimeError("synthetic runtime failure")

    def stop(self, reason: str) -> object:
        return object()

    def close(self) -> None:
        return None


class _BlockingRuntime:
    def __init__(self) -> None:
        self.tick_entered = threading.Event()
        self.release_tick = threading.Event()
        self.stop_reason: str | None = None
        self._status = object()

    def start(self) -> object:
        return self._status

    def tick(self) -> object:
        self.tick_entered.set()
        assert self.release_tick.wait(2.0)
        return object()

    def stop(self, reason: str) -> object:
        self.stop_reason = reason
        return self._status

    def close(self) -> None:
        return None


def test_error_terminal_truth_blocks_successor_start_until_polled(tmp_path: Path) -> None:
    builds = 0

    def build(_workspace: Path, _source_factory: str, _bankroll: str) -> _FailingRuntime:
        nonlocal builds
        builds += 1
        return _FailingRuntime()

    worker = ProductGuiWorker(runtime_builder=build)
    assert worker.start(
        workspace=tmp_path,
        source_factory="tests.fake:source",
        poll_seconds=1.0,
    )
    assert worker.join(2.0)

    # ERROR has already been published by the background thread, but until the
    # controller consumes it a successor run must not be admitted. Otherwise
    # start() replaces _messages and can erase the ERROR before recovery quarantine.
    assert worker.busy is True
    assert not worker.start(
        workspace=tmp_path,
        source_factory="tests.fake:source",
        poll_seconds=1.0,
    )
    assert builds == 1

    terminal = worker.poll()
    assert terminal is not None
    assert terminal.kind == "ERROR"
    assert worker.busy is False

    # Once terminal truth has crossed the presentation boundary, normal admission
    # resumes. The second synthetic run is drained so no worker is left behind.
    assert worker.start(
        workspace=tmp_path,
        source_factory="tests.fake:source",
        poll_seconds=1.0,
    )
    assert worker.join(2.0)
    assert builds == 2
    terminal = worker.poll()
    assert terminal is not None
    assert terminal.kind == "ERROR"
    assert worker.busy is False


def test_stopped_terminal_truth_blocks_successor_start_until_polled(tmp_path: Path) -> None:
    runtime = _BlockingRuntime()
    builds = 0

    def build(_workspace: Path, _source_factory: str, _bankroll: str) -> _BlockingRuntime:
        nonlocal builds
        builds += 1
        return runtime

    worker = ProductGuiWorker(runtime_builder=build)
    assert worker.start(
        workspace=tmp_path,
        source_factory="tests.fake:source",
        poll_seconds=60.0,
    )
    assert runtime.tick_entered.wait(2.0)

    started = worker.poll()
    assert started is not None
    assert started.kind == "STARTED"

    assert worker.request_stop("operator_stop")
    runtime.release_tick.set()
    assert worker.join(2.0)
    assert runtime.stop_reason == "operator_stop"

    assert worker.busy is True
    assert not worker.start(
        workspace=tmp_path,
        source_factory="tests.fake:source",
        poll_seconds=60.0,
    )
    assert builds == 1

    terminal = worker.poll()
    assert terminal is not None
    assert terminal.kind == "STOPPED"
    assert terminal.stop_reason == "operator_stop"
    assert worker.busy is False
