from __future__ import annotations

import threading
from pathlib import Path

from autosport.continuous_session import SessionStoppedError
from autosport.operator_source_store import OperatorSourceConfigStore
from autosport.product_gui_worker import ProductGuiWorker
from autosport.windows_webview_shell import AutosportWebController, _safe_exception_text


_ROOT = Path(__file__).resolve().parents[1]


class _IdleWorker:
    busy = False

    def poll(self):
        return None


class _QueuedProductWorker:
    busy = False

    def __init__(self, messages: list[object]) -> None:
        self.messages = list(messages)

    def poll(self):
        return self.messages.pop(0) if self.messages else None

    def request_stop(self, reason: str = "operator_stop") -> bool:
        return False


class _FakeProductWorker:
    def __init__(self) -> None:
        self.busy = False
        self.start_calls: list[dict[str, object]] = []
        self.stop_reasons: list[str] = []

    def start(self, **kwargs: object) -> bool:
        self.start_calls.append(dict(kwargs))
        self.busy = True
        return True

    def request_stop(self, reason: str = "operator_stop") -> bool:
        if not self.busy:
            return False
        self.stop_reasons.append(reason)
        return True


class _InterruptibleStopRuntime:
    def __init__(self) -> None:
        self.tick_entered = threading.Event()
        self.stop_signal = threading.Event()
        self.request_reasons: list[str] = []
        self.stop_reasons: list[str] = []
        self.closed = False

    def start(self):
        return object()

    def request_stop(self, reason: str = "operator_stop") -> None:
        self.request_reasons.append(reason)
        self.stop_signal.set()

    def tick(self):
        self.tick_entered.set()
        if not self.stop_signal.wait(2.0):
            raise AssertionError("runtime STOP was not signalled while tick was blocked")
        raise SessionStoppedError("cooperative runtime stop")

    def stop(self, reason: str = "operator_stop"):
        self.stop_reasons.append(reason)
        self.stop_signal.set()
        return object()

    def close(self) -> None:
        self.closed = True


class _PartialStartRuntime:
    def __init__(self) -> None:
        self.collector_resumed = False
        self.stop_reason: str | None = None
        self.closed = False

    def start(self):
        self.collector_resumed = True
        raise RuntimeError("provider-secret-must-not-reach-ui")

    def stop(self, reason: str = "operator_stop"):
        self.stop_reason = reason
        self.collector_resumed = False
        return None

    def close(self) -> None:
        self.closed = True


def _bare_controller(tmp_path: Path) -> AutosportWebController:
    controller = AutosportWebController.__new__(AutosportWebController)
    controller._lock = threading.RLock()
    controller._request_results = {}
    controller._closing = False
    controller.workspace = tmp_path
    controller._active_workspace = tmp_path
    controller._recovery_required_workspaces = set()
    controller.strategy_id = "baseline-v1"
    controller.research_plan = None
    controller.dataset_worker = _IdleWorker()
    controller.replay_worker = _IdleWorker()
    controller.live_worker = _IdleWorker()
    controller.recovery_worker = _IdleWorker()
    controller.evidence_export_worker = _IdleWorker()
    controller.product_worker = _FakeProductWorker()
    controller.product_runtime_status = "Тривала PAPER-робота не запущена."
    controller.status = ""
    controller.last_error = ""
    controller.log = []
    controller.manual_result = "prior"
    controller.manual_status = ""
    return controller


def test_webview_runtime_start_stop_delegates_to_canonical_worker(
    tmp_path: Path,
) -> None:
    controller = _bare_controller(tmp_path)
    OperatorSourceConfigStore(tmp_path / "operator-source.json").write_source_id(
        "parlayapi-table-tennis"
    )

    started = controller._action_product_runtime_start({})
    assert started["status"] == "completed"
    worker = controller.product_worker
    assert isinstance(worker, _FakeProductWorker)
    assert worker.start_calls == [
        {
            "workspace": tmp_path,
            "source_factory": "autosport.product_source:create_parlay_product_source",
            "expected_source_id": "parlayapi:table_tennis",
            "initial_bankroll": "10000",
            "poll_seconds": 30.0,
        }
    ]

    stopped = controller._action_product_runtime_stop({})
    assert stopped["status"] == "completed"
    assert worker.stop_reasons == ["operator_stop"]


def test_bridge_duplicate_request_id_replays_result_without_second_mutation(
    tmp_path: Path,
) -> None:
    controller = _bare_controller(tmp_path)
    command = {
        "request_id": "same-request",
        "action_id": "manual.clear",
        "payload": {},
    }

    first = controller.dispatch(command)
    log_after_first = list(controller.log)
    second = controller.dispatch(command)

    assert first == second
    assert controller.log == log_after_first

    collision = controller.dispatch(
        {
            "request_id": "same-request",
            "action_id": "manual.clear",
            "payload": {"changed": True},
        }
    )
    assert collision["status"] == "rejected"
    assert controller.log == log_after_first


def test_worker_stop_interrupts_active_runtime_tick_and_emits_stopped(
    tmp_path: Path,
) -> None:
    runtime = _InterruptibleStopRuntime()
    worker = ProductGuiWorker(runtime_builder=lambda *_args: runtime)

    assert worker.start(
        workspace=tmp_path,
        source_factory="provider.module:factory",
        poll_seconds=60,
    )
    assert runtime.tick_entered.wait(2.0)
    assert worker.request_stop("operator_stop")
    assert worker.join(2.0)

    messages = []
    while True:
        message = worker.poll()
        if message is None:
            break
        messages.append(message)

    assert runtime.request_reasons == ["operator_stop"]
    assert runtime.stop_reasons == ["operator_stop"]
    assert runtime.closed is True
    assert [message.kind for message in messages] == ["STARTED", "STOPPED"]
    assert messages[-1].stop_reason == "operator_stop"


def test_partial_runtime_start_is_compensated_and_error_detail_is_not_projected(
    tmp_path: Path,
) -> None:
    runtime = _PartialStartRuntime()
    worker = ProductGuiWorker(runtime_builder=lambda *_args: runtime)

    assert worker.start(
        workspace=tmp_path,
        source_factory="provider.module:factory",
        poll_seconds=60,
    )
    assert worker.join(2)

    messages = []
    while True:
        message = worker.poll()
        if message is None:
            break
        messages.append(message)

    assert runtime.stop_reason == "runtime_error"
    assert runtime.collector_resumed is False
    assert runtime.closed is True
    assert len(messages) == 1
    assert messages[0].kind == "ERROR"
    assert messages[0].error_type == "RuntimeError"
    assert "provider-secret" not in repr(messages[0])


def test_terminal_product_message_is_drained_before_start_can_reenable(
    tmp_path: Path,
) -> None:
    controller = _bare_controller(tmp_path)
    started = type(
        "Started",
        (),
        {
            "kind": "STARTED",
            "status": type("Status", (), {"source_id": "source-1", "cycles_completed": 0})(),
            "tick": None,
            "stop_reason": None,
            "error_type": None,
        },
    )()
    stopped = type(
        "Stopped",
        (),
        {
            "kind": "STOPPED",
            "status": type("Status", (), {"source_id": "source-1", "cycles_completed": 1})(),
            "tick": None,
            "stop_reason": "operator_stop",
            "error_type": None,
        },
    )()
    controller.product_worker = _QueuedProductWorker([started, stopped])
    controller._refresh_owner_projection = lambda: None
    refreshed: list[bool] = []
    controller._refresh_economic_projection = lambda: refreshed.append(True)

    controller._poll_workers()

    assert controller.product_worker.poll() is None
    assert "операторська зупинка" in controller.product_runtime_status
    assert refreshed == [True]


def test_visible_exception_projection_never_contains_raw_detail() -> None:
    projected = _safe_exception_text(RuntimeError("token=/secret/path"))
    assert "RuntimeError" in projected
    assert "token" not in projected
    assert "/secret/path" not in projected

    hostile_type = type("СекретнийТип", (Exception,), {})
    hostile = _safe_exception_text(hostile_type("credential=hidden"))
    assert "СекретнийТип" not in hostile
    assert "credential" not in hostile


def test_semantic_shell_contains_runtime_controls_and_real_tickets_table() -> None:
    html = (_ROOT / "src/autosport/windows_web/index.html").read_text(encoding="utf-8")
    js = (_ROOT / "src/autosport/windows_web/app.js").read_text(encoding="utf-8")
    shell = (_ROOT / "src/autosport/windows_webview_shell.py").read_text(
        encoding="utf-8"
    )

    assert 'id="product-runtime-start"' in html
    assert 'id="product-runtime-stop"' in html
    assert 'id="product-runtime-status"' in html
    assert '<table id="201"' in html
    assert "<caption>Паперові квитки і результати</caption>" in html
    assert '<th scope="col">' in html
    assert 'id="tickets-table-body"' in html

    assert 'dispatch("product_runtime.start")' in js
    assert 'dispatch("product_runtime.stop")' in js
    assert 'renderSingleColumnTable(byId("tickets-table-body"), state.tickets)' in js
    assert 'String(error)' not in js

    assert '"product_runtime.start": self._action_product_runtime_start' in shell
    assert '"product_runtime.stop": self._action_product_runtime_stop' in shell
    assert "text_select=True" in shell
    assert "zoomable=True" in shell
    assert "detail=message.error" not in shell
    assert "detail=replay_message.error" not in shell
    assert "detail=live_message.error" not in shell
    assert "detail=recovery_message.error" not in shell
    assert "error=export_message.error" not in shell
