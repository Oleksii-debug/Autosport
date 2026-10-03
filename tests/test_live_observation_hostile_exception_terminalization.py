from __future__ import annotations

import time
from unittest import mock

import autosport.live_observation as live_observation
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


class _HostileRenderedText(str):
    def __len__(self) -> int:
        raise RuntimeError("rendered text truthiness failed")


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




def test_renderer_failure_uses_secret_free_terminal_fallback_and_releases_busy() -> None:
    worker = OneShotObservationWorker()

    def task():
        raise RuntimeError("provider-timeout")

    with mock.patch(
        "autosport.live_observation.safe_exception_text",
        side_effect=RuntimeError("renderer failed"),
    ):
        assert worker.start(task) is True
        message = _wait_for_terminal(worker)

    assert message.result is None
    assert message.error == "BaseException: exception details unavailable"
    assert worker.busy is False


def test_renderer_rebinding_cannot_publish_plausible_unredacted_text() -> None:
    worker = OneShotObservationWorker()
    secret = "renderer-rebind-secret-2056"

    def task():
        raise RuntimeError(f"Authorization: Bearer {secret}")

    with mock.patch(
        "autosport.live_observation.safe_exception_text",
        return_value=f"RuntimeError: Authorization: Bearer {secret}",
    ) as forged_renderer:
        assert worker.start(task) is True
        message = _wait_for_terminal(worker)

    assert message.result is None
    assert message.error == "BaseException: exception details unavailable"
    assert secret not in message.error
    forged_renderer.assert_not_called()
    assert worker.busy is False


def test_renderer_in_place_code_mutation_fails_closed_before_publication() -> None:
    worker = OneShotObservationWorker()
    original_code = live_observation.safe_exception_text.__code__

    def forged_renderer(
        exc,
        *,
        unavailable_detail="exception details unavailable",
        extra_secret_values=(),
    ):
        return "forged terminal diagnostic"

    try:
        live_observation.safe_exception_text.__code__ = forged_renderer.__code__

        def task():
            raise RuntimeError("provider-timeout")

        assert worker.start(task) is True
        message = _wait_for_terminal(worker)
    finally:
        live_observation.safe_exception_text.__code__ = original_code

    assert message.result is None
    assert message.error == "BaseException: exception details unavailable"
    assert worker.busy is False


def test_terminal_bound_globals_cannot_widen_published_diagnostic() -> None:
    worker = OneShotObservationWorker()
    oversized = "bounded-" + ("z" * 10_000)

    with (
        mock.patch("autosport.live_observation._TERMINAL_ERROR_MAX_CHARS", 100_000),
        mock.patch(
            "autosport.live_observation._TERMINAL_ERROR_TRUNCATION",
            "attacker-controlled-marker",
        ),
    ):

        def task():
            raise RuntimeError(oversized)

        assert worker.start(task) is True
        message = _wait_for_terminal(worker)

    assert message.result is None
    assert message.error is not None
    assert len(message.error) == 2048
    assert message.error.endswith("... [truncated]")
    assert "attacker-controlled-marker" not in message.error
    assert worker.busy is False


def test_renderer_invalid_return_uses_secret_free_terminal_fallback() -> None:
    worker = OneShotObservationWorker()

    def task():
        raise RuntimeError("provider-timeout")

    with mock.patch(
        "autosport.live_observation.safe_exception_text",
        return_value=None,
    ):
        assert worker.start(task) is True
        message = _wait_for_terminal(worker)

    assert message.result is None
    assert message.error == "BaseException: exception details unavailable"
    assert worker.busy is False

def test_renderer_hostile_str_subclass_uses_secret_free_terminal_fallback() -> None:
    worker = OneShotObservationWorker()

    def task():
        raise RuntimeError("provider-timeout")

    with mock.patch(
        "autosport.live_observation.safe_exception_text",
        return_value=_HostileRenderedText("provider-timeout"),
    ):
        assert worker.start(task) is True
        message = _wait_for_terminal(worker)

    assert message.result is None
    assert message.error == "BaseException: exception details unavailable"
    assert worker.busy is False


def test_oversized_task_error_is_redacted_then_bounded_and_releases_busy() -> None:
    worker = OneShotObservationWorker()
    secret = "live-worker-secret-bounded-2056"
    oversized_tail = "x" * 10_000

    def task():
        raise RuntimeError(
            f"Authorization: Bearer {secret}; ordinary=provider-timeout;{oversized_tail}"
        )

    assert worker.start(task) is True
    message = _wait_for_terminal(worker)

    assert message.result is None
    assert message.error is not None
    assert len(message.error) == 2048
    assert message.error.endswith("... [truncated]")
    assert "[REDACTED]" in message.error
    assert "ordinary=provider-timeout" in message.error
    assert secret not in message.error
    assert worker.busy is False

    rendered = text("ui.error.live.snapshot", detail=message.error)
    assert message.error in rendered
    assert secret not in rendered


def test_oversized_thread_constructor_error_is_bounded_and_pollable() -> None:
    worker = OneShotObservationWorker()
    oversized = "setup-" + ("y" * 10_000)

    with mock.patch(
        "autosport.live_observation.threading.Thread",
        side_effect=RuntimeError(oversized),
    ):
        assert worker.start(lambda: object()) is True

    assert worker._thread is None
    assert worker.busy is True
    message = worker.poll()
    assert message is not None
    assert message.result is None
    assert message.error is not None
    assert len(message.error) == 2048
    assert message.error.startswith("RuntimeError: setup-")
    assert message.error.endswith("... [truncated]")
    assert worker.busy is False


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



def test_poll_join_interruption_preserves_terminal_message_and_busy_ownership() -> None:
    worker = OneShotObservationWorker()
    sentinel = object()

    assert worker.start(lambda: sentinel) is True
    helper = worker._thread
    assert helper is not None
    helper.join(timeout=2.0)
    assert not helper.is_alive()
    assert worker.busy is True

    with mock.patch.object(
        helper,
        "join",
        side_effect=KeyboardInterrupt("poll join interrupted"),
    ):
        try:
            worker.poll()
        except KeyboardInterrupt as exc:
            assert str(exc) == "poll join interrupted"
        else:
            raise AssertionError("poll join interruption must propagate")

    # The failed reap cannot consume the one terminal disposition or release the
    # single-flight slot. A subsequent ordinary poll must recover deterministically.
    assert worker.busy is True
    assert worker._thread is helper
    assert worker.start(lambda: object()) is False

    message = worker.poll()
    assert message is not None
    assert message.result is sentinel
    assert message.error is None
    assert worker.busy is False
    assert worker._thread is None
