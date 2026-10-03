from __future__ import annotations

import time
from unittest import mock

from autosport.live_observation import OneShotObservationWorker
from autosport.localization import text


class _ExplodingTextError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("exception stringification failed")


class _HostileTypeNameMeta(type):
    def __getattribute__(cls, name: str):
        if name == "__name__":
            raise RuntimeError("exception type-name lookup failed")
        return super().__getattribute__(name)


class _HostileMetadataAndTextError(RuntimeError, metaclass=_HostileTypeNameMeta):
    def __str__(self) -> str:
        raise RuntimeError("exception stringification failed")


def _wait_for_terminal(
    worker: OneShotObservationWorker,
    *,
    timeout: float = 2.0,
):
    thread = worker._thread
    if thread is not None:
        thread.join(timeout=timeout)
        assert not thread.is_alive()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = worker.poll()
        if message is not None:
            return message
        time.sleep(0.01)
    raise AssertionError("live observation worker did not publish a terminal message")


def _assert_safe_terminal_error(worker: OneShotObservationWorker, message) -> None:
    assert message.result is None
    assert message.error is not None
    assert "exception details unavailable" in message.error
    assert "exception stringification failed" not in message.error
    assert worker.busy is False


def test_task_exception_with_exploding_str_terminalizes_and_releases_busy() -> None:
    worker = OneShotObservationWorker()

    def task():
        raise _ExplodingTextError()

    assert worker.start(task) is True
    message = _wait_for_terminal(worker)

    _assert_safe_terminal_error(worker, message)

    assert worker.start(lambda: object()) is True
    retry = _wait_for_terminal(worker)
    assert retry.error is None
    assert retry.result is not None
    assert worker.busy is False


def test_task_exception_with_hostile_type_metadata_and_str_terminalizes() -> None:
    worker = OneShotObservationWorker()

    def task():
        raise _HostileMetadataAndTextError()

    assert worker.start(task) is True
    message = _wait_for_terminal(worker)

    _assert_safe_terminal_error(worker, message)


def test_thread_constructor_failure_with_exploding_str_publishes_terminal() -> None:
    worker = OneShotObservationWorker()

    with mock.patch(
        "autosport.live_observation.threading.Thread",
        side_effect=_ExplodingTextError(),
    ):
        assert worker.start(lambda: object()) is True

    assert worker._thread is None
    message = worker.poll()
    assert message is not None

    _assert_safe_terminal_error(worker, message)


def test_thread_start_failure_with_exploding_str_publishes_terminal_and_releases_busy() -> None:
    worker = OneShotObservationWorker()

    class _FailingThread:
        ident = None

        def start(self) -> None:
            raise _ExplodingTextError()

    with mock.patch(
        "autosport.live_observation.threading.Thread",
        return_value=_FailingThread(),
    ):
        assert worker.start(lambda: object()) is True

    assert worker._thread is None
    message = worker.poll()
    assert message is not None

    _assert_safe_terminal_error(worker, message)


def test_task_error_is_redacted_before_localized_live_presentation() -> None:
    worker = OneShotObservationWorker()
    secret = "live-worker-secret-2056"

    def task():
        raise RuntimeError(
            f"Authorization: Bearer {secret}; ordinary=provider-timeout"
        )

    assert worker.start(task) is True
    message = _wait_for_terminal(worker)

    assert message.result is None
    assert message.error is not None
    assert "[REDACTED]" in message.error
    assert "ordinary=provider-timeout" in message.error
    assert secret not in message.error
    assert worker.busy is False

    rendered = text("ui.error.live.snapshot", detail=message.error)
    assert rendered.startswith("Помилка поточного знімка:")
    assert message.error in rendered
    assert secret not in rendered
