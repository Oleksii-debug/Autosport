from __future__ import annotations

import ast
import inspect
import textwrap

from autosport.continuous_session import ContinuousSessionStatus, SessionState
from autosport.localization_product_runtime import product_text
from autosport.product_gui_worker import ProductGuiMessage
from autosport.product_windows_gui import ProductWindowsAutosportApp


class _StateButton:
    def __init__(self, *, disabled: bool) -> None:
        self.disabled = disabled

    def state(self, spec: list[str]) -> None:
        for token in spec:
            if token == "disabled":
                self.disabled = True
            elif token == "!disabled":
                self.disabled = False
            else:
                raise AssertionError(f"unexpected ttk state token: {token}")


class _StringValue:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def set(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


class _Worker:
    def __init__(self) -> None:
        self.busy = True
        self.stop_reasons: list[str] = []

    def request_stop(self, reason: str) -> bool:
        if not self.busy:
            return False
        self.stop_reasons.append(reason)
        return True

    def poll(self) -> None:
        return None


def _status(state: SessionState, *, cycles: int) -> ContinuousSessionStatus:
    return ContinuousSessionStatus(
        session_id="session-1",
        source_id="source-1",
        state=state,
        cycles_completed=cycles,
        last_success_at="2026-09-21T17:00:00+00:00",
        last_error_code=None,
        last_full_refresh_at=None,
        settlement_evidence=(),
    )


def _headless_app() -> tuple[ProductWindowsAutosportApp, _Worker, list[bool], list[bool]]:
    app = object.__new__(ProductWindowsAutosportApp)
    worker = _Worker()
    replay_busy: list[bool] = []
    restored: list[bool] = []

    app.product_worker = worker
    app.product_start_button = _StateButton(disabled=False)
    app.product_stop_button = _StateButton(disabled=True)
    app.product_status = _StringValue(product_text("ui.product_runtime.status.idle"))
    app.status = _StringValue()
    app._product_close_pending = False
    app._product_last_stop = None
    app._set_replay_controls_busy = replay_busy.append
    app._append_log = lambda _message: None
    app._restore_base_session_after_product = lambda: restored.append(True) or True
    return app, worker, replay_busy, restored


def test_runtime_actionable_state_tracks_running_stop_and_terminal_status() -> None:
    app, worker, replay_busy, restored = _headless_app()

    # Canonical runtime ownership has been committed to the worker: START must
    # no longer be actionable while STOP becomes the only runtime action.
    app._set_product_controls_running(True)
    app._apply_product_message(ProductGuiMessage(kind="STARTED", status=_status(SessionState.RUNNING, cycles=0)))

    assert app.product_start_button.disabled is True
    assert app.product_stop_button.disabled is False
    assert app.product_status.get() == product_text(
        "ui.product_runtime.status.running",
        source_id="source-1",
        cycles=0,
        last_success_at="2026-09-21T17:00:00+00:00",
    )
    assert replay_busy == [True]

    # Once cooperative STOP is requested neither START nor a second STOP should
    # appear actionable while durable runtime shutdown is unresolved.
    app.stop_product_runtime()

    assert worker.stop_reasons == ["operator_stop"]
    assert app.product_start_button.disabled is True
    assert app.product_stop_button.disabled is True
    assert app.product_status.get() == product_text("ui.product_runtime.status.stopping")

    # A canonical terminal message updates the readonly value. Only after the
    # worker is no longer busy does the poll boundary re-enable START and keep
    # STOP disabled, matching the now-terminal durable runtime state.
    stopped = ProductGuiMessage(
        kind="STOPPED",
        status=_status(SessionState.STOPPED, cycles=1),
        stop_reason="operator_stop",
    )
    app._apply_product_message(stopped)
    worker.busy = False
    app._poll_product_worker()

    assert app.product_status.get() == product_text(
        "ui.product_runtime.status.stopped",
        reason="operator_stop",
        cycles=1,
    )
    assert app.product_start_button.disabled is False
    assert app.product_stop_button.disabled is True
    assert replay_busy == [True, False]
    assert restored == [True]


def test_runtime_status_control_is_readonly_while_value_remains_programmatic() -> None:
    source = textwrap.dedent(inspect.getsource(ProductWindowsAutosportApp._build))
    tree = ast.parse(source)

    status_entries = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "Entry":
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        textvariable = keywords.get("textvariable")
        state = keywords.get("state")
        if (
            isinstance(textvariable, ast.Attribute)
            and textvariable.attr == "product_status"
            and isinstance(state, ast.Constant)
            and state.value == "readonly"
        ):
            status_entries.append(node)

    assert len(status_entries) == 1

    app, _worker, _replay_busy, _restored = _headless_app()
    app._apply_product_message(
        ProductGuiMessage(kind="STARTED", status=_status(SessionState.RUNNING, cycles=3))
    )
    assert app.product_status.get() == product_text(
        "ui.product_runtime.status.running",
        source_id="source-1",
        cycles=3,
        last_success_at="2026-09-21T17:00:00+00:00",
    )
