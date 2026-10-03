from __future__ import annotations

from pathlib import Path
import inspect
import threading
from types import SimpleNamespace

from autosport.windows_webview_shell import AutosportWebController, web_shell_index_path


class _IdleWorker:
    busy = False

    def poll(self):
        return None


class _QueuedProductWorker:
    def __init__(self, message: object, *, busy: bool = False) -> None:
        self._message = message
        self.busy = busy
        self.stop_reasons: list[str] = []

    def poll(self):
        message = self._message
        self._message = None
        return message

    def request_stop(self, reason: str = "operator_stop") -> bool:
        self.stop_reasons.append(reason)
        return True


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
    controller._product_runtime_identity = None
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
            status=SimpleNamespace(
                session_id="session-1",
                source_id="source-1",
                cycles_completed=0,
            ),
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
                session_id="session-1",
                source_id="source-1",
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
            status=SimpleNamespace(
                session_id="session-1",
                source_id="source-1",
                cycles_completed=3,
            ),
            tick=None,
            stop_reason="operator_stop",
            error_type=None,
        ),
    )

    assert "PAPER" not in status
    assert "оператор" in status.casefold()


def test_error_runtime_status_localizes_copy_without_python_type_leakage(
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
    assert "RuntimeError" not in status
    assert "BaseException" not in status
    assert "віднов" in status.casefold()


def test_runtime_identity_projection_comes_from_canonical_started_status(
    tmp_path: Path,
) -> None:
    controller = _controller(
        tmp_path,
        SimpleNamespace(
            kind="STARTED",
            status=SimpleNamespace(
                session_id="session-1",
                source_id="source-1",
                cycles_completed=0,
            ),
            tick=None,
            stop_reason=None,
            error_type=None,
        ),
    )

    controller._poll_workers()

    assert controller._product_runtime_identity_projection() == {
        "workspace": str(tmp_path),
        "session_id": "session-1",
        "source_id": "source-1",
    }


def test_runtime_identity_drift_quarantines_workspace_and_requests_stop(
    tmp_path: Path,
) -> None:
    controller = _controller(
        tmp_path,
        SimpleNamespace(
            kind="STARTED",
            status=SimpleNamespace(
                session_id="session-1",
                source_id="source-1",
                cycles_completed=0,
            ),
            tick=None,
            stop_reason=None,
            error_type=None,
        ),
    )
    controller._poll_workers()

    drift_worker = _QueuedProductWorker(
        SimpleNamespace(
            kind="TICK",
            status=None,
            tick=SimpleNamespace(
                session_id="session-2",
                source_id="source-1",
                cycle_index=1,
                committed_delta_ids=(),
                settled_ticket_ids=(),
            ),
            stop_reason=None,
            error_type=None,
        ),
        busy=True,
    )
    controller.product_worker = drift_worker

    controller._poll_workers()

    assert tmp_path in controller._recovery_required_workspaces
    assert drift_worker.stop_reasons == ["runtime_error"]
    assert "віднов" in controller.product_runtime_status.casefold()
    assert controller._product_runtime_identity_projection()["session_id"] == "session-1"


def test_runtime_state_contract_projects_canonical_identity_fields() -> None:
    source = inspect.getsource(AutosportWebController.state)

    assert '"workspace": runtime_identity["workspace"]' in source
    assert '"session_id": runtime_identity["session_id"]' in source
    assert '"source_id": runtime_identity["source_id"]' in source


def test_runtime_identity_is_keyboard_readable_in_semantic_shell() -> None:
    index = web_shell_index_path()
    html = index.read_text(encoding="utf-8")
    javascript = index.with_name("app.js").read_text(encoding="utf-8")

    for control_id in (
        "product-runtime-workspace",
        "product-runtime-session-id",
        "product-runtime-source-id",
    ):
        assert f'<label for="{control_id}">' in html
        assert f'id="{control_id}" type="text" readonly' in html
        assert f'byId("{control_id}")' in javascript

    assert 'productRuntime.workspace || "—"' in javascript
    assert 'productRuntime.session_id || "—"' in javascript
    assert 'productRuntime.source_id || "—"' in javascript
