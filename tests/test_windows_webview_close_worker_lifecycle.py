from __future__ import annotations

import threading
from pathlib import Path

from autosport.replay_worker import OneShotReplayWorker
from autosport.windows_webview_shell import AutosportWebController


class _IdleWorker:
    busy = False

    def poll(self):
        return None


class _IdleProductWorker(_IdleWorker):
    def __init__(self) -> None:
        self.stop_reasons: list[str] = []

    def request_stop(self, reason: str = "operator_stop") -> bool:
        self.stop_reasons.append(reason)
        return False


def _bare_controller(tmp_path: Path) -> AutosportWebController:
    controller = AutosportWebController.__new__(AutosportWebController)
    controller._lock = threading.RLock()
    controller._closing = False
    controller.status = ""
    controller.last_error = ""
    controller.log = []
    controller.workspace = tmp_path
    controller._active_workspace = tmp_path
    controller.dataset_worker = _IdleWorker()
    controller.replay_worker = _IdleWorker()
    controller.live_worker = _IdleWorker()
    controller.recovery_worker = _IdleWorker()
    controller.evidence_export_worker = _IdleWorker()
    controller.product_worker = _IdleProductWorker()
    return controller


def test_webview_close_does_not_return_while_committed_replay_is_still_active(
    tmp_path: Path,
) -> None:
    """Closing the only operator surface must not orphan economic work.

    OneShotReplayWorker is deliberately non-daemon because replay can cross
    durable/economic boundaries.  Once a replay has committed, controller.close()
    must not return while that worker can still mutate hidden from the operator.
    """

    controller = _bare_controller(tmp_path)
    replay = OneShotReplayWorker()
    controller.replay_worker = replay

    entered = threading.Event()
    release = threading.Event()

    def task():
        entered.set()
        if not release.wait(2.0):
            raise AssertionError("test replay was not released")
        return object()

    assert replay.start(task)
    assert entered.wait(2.0)

    close_returned = threading.Event()

    def close_controller() -> None:
        controller.close()
        close_returned.set()

    close_thread = threading.Thread(target=close_controller)
    close_thread.start()
    try:
        assert not close_returned.wait(0.1), (
            "controller.close() returned while the committed non-daemon replay "
            "was still active; the WebView can disappear while economic work "
            "continues without an operator surface"
        )
        assert "Автоспорт завершує роботу" in controller.status
        assert controller.last_error == ""
    finally:
        release.set()
        close_thread.join(2.0)
        # Consume the terminal message so the worker's single-flight state is
        # not left busy after this expected-RED falsifier finishes.
        replay.poll()

    assert close_returned.is_set()

class _BlockingTerminalWorker:
    def __init__(self) -> None:
        self.busy = True
        self.release = threading.Event()

    def poll(self):
        if not self.release.is_set():
            return None
        self.busy = False
        return object()


class _JoinAwareProductWorker(_IdleProductWorker):
    def __init__(self) -> None:
        super().__init__()
        self.join_calls = 0

    def join(self, timeout=None) -> bool:
        del timeout
        self.join_calls += 1
        return True


def test_webview_close_waits_for_every_non_daemon_one_shot_slot(
    tmp_path: Path,
) -> None:
    for attribute in (
        "replay_worker",
        "live_worker",
        "recovery_worker",
        "evidence_export_worker",
    ):
        controller = _bare_controller(tmp_path)
        worker = _BlockingTerminalWorker()
        setattr(controller, attribute, worker)
        close_returned = threading.Event()

        def close_controller() -> None:
            controller.close()
            close_returned.set()

        close_thread = threading.Thread(target=close_controller)
        close_thread.start()
        try:
            assert not close_returned.wait(0.1), (
                f"controller.close() returned while {attribute} was still active"
            )
        finally:
            worker.release.set()
            close_thread.join(2.0)

        assert close_returned.is_set()


def test_webview_close_requests_product_stop_then_joins_product_worker(
    tmp_path: Path,
) -> None:
    controller = _bare_controller(tmp_path)
    product = _JoinAwareProductWorker()
    controller.product_worker = product

    controller.close()

    assert product.stop_reasons == ["app_close"]
    assert product.join_calls == 1



class _FailOnceProductWorker(_IdleProductWorker):
    def __init__(self) -> None:
        super().__init__()
        self.fail = True
        self.join_calls = 0

    def request_stop(self, reason: str = "operator_stop") -> bool:
        self.stop_reasons.append(reason)
        if self.fail:
            raise RuntimeError("SECRET_SYNTHETIC_CLOSE_FAILURE")
        return False

    def join(self, timeout=None) -> bool:
        del timeout
        self.join_calls += 1
        return True


def test_webview_close_failure_keeps_controller_retryable_and_error_secret_safe(
    tmp_path: Path,
) -> None:
    controller = _bare_controller(tmp_path)
    controller.status = ""
    controller.last_error = ""
    controller.log = []
    product = _FailOnceProductWorker()
    controller.product_worker = product

    try:
        controller.close()
    except RuntimeError as exc:
        assert "SECRET_SYNTHETIC_CLOSE_FAILURE" in str(exc)
    else:
        raise AssertionError("synthetic close failure was not propagated")

    assert controller._closing is False
    assert getattr(controller, "_close_complete", False) is False
    assert "Вікно залишено відкритим" in controller.last_error
    assert "SECRET_SYNTHETIC_CLOSE_FAILURE" not in controller.last_error
    assert "SECRET_SYNTHETIC_CLOSE_FAILURE" not in "\n".join(controller.log)

    product.fail = False
    controller.close()

    assert controller._closing is True
    assert controller._close_complete is True
    assert product.stop_reasons == ["app_close", "app_close"]
    assert product.join_calls == 1
