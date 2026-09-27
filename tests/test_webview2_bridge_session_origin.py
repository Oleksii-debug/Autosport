from __future__ import annotations

import sys
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
        )

    def get_current_url(self):
        return self.current_url


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
