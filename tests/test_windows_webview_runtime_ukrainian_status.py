from __future__ import annotations

from pathlib import Path
import threading
from types import SimpleNamespace

from autosport.windows_webview_shell import AutosportWebController


class _IdleWorker:
    busy = False

    def poll(self):
        return None


class _QueuedProductWorker:
    busy = False

    def __init__(self, message: object) -> None:
        self._message = message

    def poll(self):
        message = self._message
        self._message = None
        return message

    def request_stop(self, reason: str = "operator_stop") -> bool:
        del reason
        return False


def _controller(tmp_path: Path, message: object) -> AutosportWebController:
    controller = AutosportWebController.__new__(AutosportWebController)
    controller._lock = threading.RLock()
    controller.workspace = tmp_path
    controller._active_workspace = tmp_path
    controller._recovery_required_workspaces = set()
    controller.dataset_worker = _IdleWorker()
    controller.replay_worker = _IdleWorker()
    controller.live_worker = _IdleWorker()
    controller.recovery_worker = _IdleWorker()
    controller.evidence_export_worker = _IdleWorker()
    controller.product_worker = _QueuedProductWorker(message)
    controller.product_runtime_status = ""
    controller.status = ""
    controller.last_error = ""
    controller.log = []
    controller._refresh_owner_projection = lambda: None
    controller._refresh_economic_projection = lambda: None
    return controller


def _project(tmp_path: Path, message: object) -> str:
    controller = _controller(tmp_path, message)
    controller._poll_workers()
    return controller.product_runtime_status


def test_started_runtime_status_uses_ukrainian_operator_copy(tmp_path: Path) -> None:
    status = _project(
        tmp_path,
        SimpleNamespace(
            kind="STARTED",
            status=SimpleNamespace(source_id="source-1", cycles_completed=0),
            tick=None,
            stop_reason=None,
            error_type=None,
        ),
    )

    assert "PAPER" not in status
    assert "source-1" in status


def test_tick_runtime_status_does_not_expose_internal_english_terms(tmp_path: Path) -> None:
    status = _project(
        tmp_path,
        SimpleNamespace(
            kind="TICK",
            status=None,
            tick=SimpleNamespace(
                cycle_index=7,
                committed_delta_ids=("delta-1", "delta-2"),
                settled_ticket_ids=("ticket-1",),
            ),
            stop_reason=None,
            error_type=None,
        ),
    )

    assert "PAPER" not in status
    assert "runtime:" not in status
    assert "delta " not in status
    assert "settlement " not in status
    assert "7" in status


def test_stopped_runtime_status_uses_ukrainian_operator_copy(tmp_path: Path) -> None:
    status = _project(
        tmp_path,
        SimpleNamespace(
            kind="STOPPED",
            status=SimpleNamespace(source_id="source-1", cycles_completed=3),
            tick=None,
            stop_reason="operator_stop",
            error_type=None,
        ),
    )

    assert "PAPER" not in status
    assert "оператор" in status.casefold()


def test_error_runtime_status_localizes_copy_but_preserves_bounded_type(
    tmp_path: Path,
) -> None:
    status = _project(
        tmp_path,
        SimpleNamespace(
            kind="ERROR",
            status=None,
            tick=None,
            stop_reason="runtime_error",
            error_type="RuntimeError",
        ),
    )

    assert "PAPER" not in status
    assert "RuntimeError" in status
    assert "віднов" in status.casefold()
