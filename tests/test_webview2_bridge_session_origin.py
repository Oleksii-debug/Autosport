from __future__ import annotations

import sys
import threading
from types import SimpleNamespace

import pytest

from autosport.windows_webview_shell import (
    AutosportWebBridge,
    WindowsWebBridgeTrustError,
    WindowsWebViewUnavailable,
    launch_windows_shell,
)


class _Controller:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def dispatch(self, raw):
        self.events.append(("dispatch", raw))
        return {"request_id": raw.get("request_id", "test"), "status": "completed"}

    def state(self):
        self.events.append(("state", None))
        return {"status": "ok"}

    def close(self) -> None:
        self.events.append(("close", None))


class _Window:
    def __init__(self, url: str = "http://127.0.0.1:41000/index.html") -> None:
        self.current_url = url
        self.events = SimpleNamespace(
            initialized=_Event(),
            before_load=_Event(),
            closing=_Event(),
        )
        self.destroy_calls = 0
        self.destroy_callback = None

    def get_current_url(self):
        return self.current_url

    def destroy(self) -> None:
        self.destroy_calls += 1
        if self.destroy_callback is not None:
            self.destroy_callback()


class _Event:
    def __init__(self) -> None:
        self.handlers: list[object] = []

    def __iadd__(self, handler: object):
        self.handlers.append(handler)
        return self

    def fire(self, *args):
        return [handler(*args) for handler in tuple(self.handlers)]


class _FakeWebview:
    def __init__(self, *, navigate_to: str | None = None) -> None:
        self.window = _Window()
        self.navigate_to = navigate_to
        self.api = None
        self.requested_gui: str | None = None
        self.bridge_rejected_after_navigation = False
        self.settings = {
            "WEBVIEW2_RUNTIME_PATH": None,
            "REMOTE_DEBUGGING_PORT": None,
        }

    def create_window(self, *args, **kwargs):
        self.api = kwargs["js_api"]
        return self.window

    def start(self, *, gui: str, **kwargs) -> None:
        self.requested_gui = gui
        assert self.window.events.initialized.fire("edgechromium") == [True]
        self.window.events.before_load.fire()

        if self.navigate_to is None:
            assert self.api.get_state()["ok"] is True
            return

        self.window.current_url = self.navigate_to
        self.window.events.before_load.fire()
        with pytest.raises(WindowsWebBridgeTrustError):
            self.api.get_state()
        self.bridge_rejected_after_navigation = True


def test_bridge_rejects_public_calls_before_trusted_document_binding() -> None:
    controller = _Controller()
    bridge = AutosportWebBridge(controller)

    with pytest.raises(WindowsWebBridgeTrustError):
        bridge.dispatch({"request_id": "r1", "action_id": "noop", "payload": {}})
    with pytest.raises(WindowsWebBridgeTrustError):
        bridge.get_state()
    with pytest.raises(WindowsWebBridgeTrustError):
        bridge.close()

    assert controller.events == []
    bridge._close_from_host()
    assert controller.events == [("close", None)]


def test_bridge_accepts_only_the_exact_bound_window_and_url() -> None:
    controller = _Controller()
    bridge = AutosportWebBridge(controller)
    window = _Window()

    bridge._bind_trusted_window(window)

    result = bridge.dispatch(
        {"request_id": "r1", "action_id": "noop", "payload": {}}
    )
    assert result["status"] == "completed"
    assert bridge.get_state() == {"ok": True, "state": {"status": "ok"}}

    bridge.close()
    assert controller.events == [
        (
            "dispatch",
            {"request_id": "r1", "action_id": "noop", "payload": {}},
        ),
        ("state", None),
        ("close", None),
    ]
    with pytest.raises(WindowsWebBridgeTrustError):
        bridge.get_state()


def test_url_drift_revokes_launch_trust_permanently() -> None:
    controller = _Controller()
    bridge = AutosportWebBridge(controller)
    window = _Window()
    trusted_url = window.current_url

    bridge._bind_trusted_window(window)
    window.current_url = "https://foreign.example/"

    with pytest.raises(WindowsWebBridgeTrustError, match="URL drift"):
        bridge.get_state()

    window.current_url = trusted_url
    with pytest.raises(WindowsWebBridgeTrustError):
        bridge._bind_trusted_window(window)
    with pytest.raises(WindowsWebBridgeTrustError):
        bridge.dispatch({"request_id": "r2", "action_id": "noop", "payload": {}})

    assert controller.events == []
    bridge._close_from_host()
    assert controller.events == [("close", None)]


def test_second_window_cannot_reuse_same_url_or_bridge_generation() -> None:
    controller = _Controller()
    bridge = AutosportWebBridge(controller)
    first = _Window()
    second = _Window(first.current_url)

    bridge._bind_trusted_window(first)
    with pytest.raises(WindowsWebBridgeTrustError, match="different window or document"):
        bridge._bind_trusted_window(second)

    with pytest.raises(WindowsWebBridgeTrustError):
        bridge.get_state()
    bridge._close_from_host()
    assert controller.events == [("close", None)]


def test_same_window_same_url_before_load_recheck_is_idempotent() -> None:
    controller = _Controller()
    bridge = AutosportWebBridge(controller)
    window = _Window()

    bridge._bind_trusted_window(window)
    bridge._bind_trusted_window(window)

    assert bridge.get_state()["state"]["status"] == "ok"
    bridge._close_from_host()


def test_launch_binds_bridge_before_pywebview_api_use(monkeypatch) -> None:
    fake = _FakeWebview()
    controller = _Controller()
    bridge = AutosportWebBridge(controller)
    monkeypatch.setitem(sys.modules, "webview", fake)

    assert launch_windows_shell(bridge) == 0

    assert fake.requested_gui == "edgechromium"
    assert fake.window.events.before_load.handlers
    assert controller.events == [("state", None), ("close", None)]


def test_navigation_before_load_revokes_bridge_and_fails_launch(monkeypatch) -> None:
    fake = _FakeWebview(navigate_to="https://foreign.example/")
    controller = _Controller()
    bridge = AutosportWebBridge(controller)
    monkeypatch.setitem(sys.modules, "webview", fake)

    with pytest.raises(
        WindowsWebViewUnavailable,
        match="lost its trusted WebView document binding",
    ):
        launch_windows_shell(bridge)

    assert fake.bridge_rejected_after_navigation is True
    assert controller.events == [("close", None)]


class _FailingCloseController(_Controller):
    def __init__(self) -> None:
        super().__init__()
        self.fail_close = True
        self.failed_close_returned = threading.Event()

    def close(self) -> None:
        self.events.append(("close", None))
        if self.fail_close:
            self.failed_close_returned.set()
            raise RuntimeError("synthetic teardown failure")


class _NativeClosingWebview(_FakeWebview):
    def __init__(self, controller: _FailingCloseController | _Controller) -> None:
        super().__init__()
        self.controller = controller
        self.first_close_result: list[object] | None = None
        self.destroy_close_result: list[object] | None = None
        self.retry_close_results: list[list[object]] = []
        self.state_after_failed_close: dict[str, object] | None = None
        self.destroyed = threading.Event()
        self.window.destroy_callback = self._native_destroy

    def _native_destroy(self) -> None:
        self.destroy_close_result = self.window.events.closing.fire()
        self.destroyed.set()

    def start(self, *, gui: str, **kwargs) -> None:
        self.requested_gui = gui
        assert self.window.events.initialized.fire("edgechromium") == [True]
        self.window.events.before_load.fire()
        self.first_close_result = self.window.events.closing.fire()
        assert self.first_close_result == [False]

        if isinstance(self.controller, _FailingCloseController):
            assert self.controller.failed_close_returned.wait(1.0)
            self.state_after_failed_close = self.api.get_state()
            self.controller.fail_close = False

            # A close retry that races the tail of the failed teardown may still
            # be vetoed. Repeating is deterministic: at most one teardown worker
            # exists, and the first post-failure retry starts the successful one.
            for _ in range(100):
                self.retry_close_results.append(self.window.events.closing.fire())
                if len([event for event in self.controller.events if event[0] == "close"]) >= 2:
                    break
                threading.Event().wait(0.01)

        assert self.destroyed.wait(1.0)


def test_native_window_close_vetoes_first_close_until_canonical_teardown_finishes(
    monkeypatch,
    tmp_path,
) -> None:
    controller = _Controller()
    fake = _NativeClosingWebview(controller)
    bridge = AutosportWebBridge(controller)
    monkeypatch.setitem(sys.modules, "webview", fake)

    assert launch_windows_shell(bridge, storage_path=tmp_path / "webview") == 0

    assert fake.window.events.closing.handlers
    assert fake.first_close_result == [False]
    assert fake.destroy_close_result == [True]
    assert fake.window.destroy_calls == 1
    assert controller.events == [("close", None)]


def test_native_window_close_failure_stays_open_then_retries_teardown(
    monkeypatch,
    tmp_path,
) -> None:
    controller = _FailingCloseController()
    fake = _NativeClosingWebview(controller)
    bridge = AutosportWebBridge(controller)
    monkeypatch.setitem(sys.modules, "webview", fake)

    assert launch_windows_shell(bridge, storage_path=tmp_path / "webview") == 0

    assert fake.first_close_result == [False]
    assert fake.state_after_failed_close == {"ok": True, "state": {"status": "ok"}}
    assert fake.retry_close_results
    assert all(result == [False] for result in fake.retry_close_results)
    assert fake.destroy_close_result == [True]
    assert fake.window.destroy_calls == 1
    assert controller.events == [
        ("close", None),
        ("state", None),
        ("close", None),
    ]


class _BlockingCloseController(_Controller):
    def __init__(self) -> None:
        super().__init__()
        self.close_entered = threading.Event()
        self.release_close = threading.Event()

    def close(self) -> None:
        self.events.append(("close", None))
        self.close_entered.set()
        if not self.release_close.wait(2.0):
            raise RuntimeError("test close was not released")


def test_bridge_does_not_hold_document_trust_lock_across_long_close() -> None:
    controller = _BlockingCloseController()
    bridge = AutosportWebBridge(controller)
    window = _Window()
    bridge._bind_trusted_window(window)

    errors: list[BaseException] = []

    def run_close() -> None:
        try:
            bridge.close()
        except BaseException as exc:
            errors.append(exc)

    close_thread = threading.Thread(target=run_close)
    close_thread.start()
    try:
        assert controller.close_entered.wait(1.0)
        # A long canonical teardown must not monopolize the trust lock. State
        # remains readable from the same trusted document while close is pending;
        # ordinary controller admission is separately fenced by _closing in the
        # real controller and the emergency safety lane remains independent.
        assert bridge.get_state() == {"ok": True, "state": {"status": "ok"}}
        assert close_thread.is_alive()
    finally:
        controller.release_close.set()
        close_thread.join(2.0)

    assert errors == []
    with pytest.raises(WindowsWebBridgeTrustError):
        bridge.get_state()


class _ThreadStartFailureWebview(_FakeWebview):
    def __init__(self) -> None:
        super().__init__()
        self.close_result: list[object] | None = None

    def start(self, *, gui: str, **kwargs) -> None:
        self.requested_gui = gui
        assert self.window.events.initialized.fire("edgechromium") == [True]
        self.window.events.before_load.fire()
        self.close_result = self.window.events.closing.fire()


def test_native_close_falls_back_synchronously_if_teardown_thread_cannot_start(
    monkeypatch,
    tmp_path,
) -> None:
    fake = _ThreadStartFailureWebview()
    controller = _Controller()
    bridge = AutosportWebBridge(controller)
    monkeypatch.setitem(sys.modules, "webview", fake)

    def fail_thread_creation(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("synthetic thread creation failure")

    monkeypatch.setattr(
        "autosport.windows_webview_shell.threading.Thread",
        fail_thread_creation,
    )

    assert launch_windows_shell(bridge, storage_path=tmp_path / "webview") == 0

    # The callback must explicitly allow close only because canonical teardown
    # completed synchronously. A thread-start exception must never be swallowed
    # by pywebview into an implicit, unsafe native close.
    assert fake.close_result == [True]
    assert controller.events == [("close", None)]
    assert fake.window.destroy_calls == 0
