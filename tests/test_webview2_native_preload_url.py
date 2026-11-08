"""Fail-closed native document URL proof at the blocking pywebview before_load seam."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from autosport.windows_webview_shell import (
    AutosportWebBridge,
    AutosportWebController,
    WindowsWebBridgeTrustError,
)


TRUSTED_URL = "http://127.0.0.1:42000/index.html"


class _NativeWindow:
    def __init__(self, source: object = TRUSTED_URL) -> None:
        self.native = SimpleNamespace(
            webview=SimpleNamespace(CoreWebView2=SimpleNamespace(Source=source))
        )
        self.getter_calls = 0

    def get_current_url(self) -> str:
        self.getter_calls += 1
        raise AssertionError("loaded-only pywebview getter must not be called")


def test_preload_uses_native_source_without_waiting_for_loaded(tmp_path) -> None:
    window = _NativeWindow()
    controller = AutosportWebController(tmp_path / "workspace")
    bridge = AutosportWebBridge(controller)
    try:
        bridge._bind_trusted_window(window, expected_url=TRUSTED_URL)
        assert window.getter_calls == 0
        assert bridge.get_state()["ok"] is True
        assert window.getter_calls == 0

        # Even if a loaded-only getter would still report the original URL,
        # live native navigation revokes all privileged bridge operations.
        window.native.webview.CoreWebView2.Source = "https://example.invalid/other"
        with pytest.raises(WindowsWebBridgeTrustError, match="URL drift"):
            bridge.get_state()
        assert window.getter_calls == 0
        assert bridge._trust_revoked is True
    finally:
        bridge._close_from_host()


@pytest.mark.parametrize("source", [None, "", " ", TRUSTED_URL + "\n"])
def test_missing_or_malformed_native_url_is_rejected_without_loaded_wait(
    source: object,
) -> None:
    window = _NativeWindow(source)
    with pytest.raises(WindowsWebBridgeTrustError):
        AutosportWebBridge._current_window_url(window)
    assert window.getter_calls == 0


def test_real_pywebview_window_without_native_source_fails_closed() -> None:
    # A synthetic type with the exact real pywebview.Window identity markers:
    # unlike a tiny test double, it must never reach get_current_url().
    RealWindowShape = type(
        "Window",
        (),
        {
            "__module__": "webview.window",
            "native": SimpleNamespace(
                webview=SimpleNamespace(CoreWebView2=SimpleNamespace())
            ),
            "get_current_url": lambda self: (_ for _ in ()).throw(
                AssertionError("deadlocking loaded-only API called")
            ),
        },
    )
    with pytest.raises(
        WindowsWebBridgeTrustError,
        match="no native document URL authority",
    ):
        AutosportWebBridge._current_window_url(RealWindowShape())


class _UiThreadCore:
    def __init__(self, native):
        self.native = native
        self.source = TRUSTED_URL

    @property
    def CoreWebView2(self):
        assert self.native.on_ui, "CoreWebView2 was read off its owning UI thread"
        return self

    @property
    def Source(self):
        assert self.native.on_ui, "Source was read off its owning UI thread"
        return self.source


class _UiThreadNative:
    InvokeRequired = True

    def __init__(self):
        self.on_ui = False
        self.invocations = 0
        self.webview = _UiThreadCore(self)

    def Invoke(self, callback):
        self.invocations += 1
        self.on_ui = True
        try:
            callback()
        finally:
            self.on_ui = False


@pytest.fixture
def native_delegate(monkeypatch):
    import sys

    class Func:
        @classmethod
        def __class_getitem__(cls, item):
            return lambda callback: callback

    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Func=Func, Type=type))


def test_worker_url_inspection_uses_ui_thread_and_observes_live_drift(tmp_path, native_delegate):
    window = _NativeWindow()
    native = _UiThreadNative()
    window.native = native
    bridge = AutosportWebBridge(AutosportWebController(tmp_path / "ui-thread-workspace"))
    try:
        bridge._bind_trusted_window(window, expected_url=TRUSTED_URL)
        assert bridge.get_state()["ok"] is True
        assert native.invocations >= 2 and window.getter_calls == 0
        native.webview.source = "https://example.invalid/navigation"
        with pytest.raises(WindowsWebBridgeTrustError, match="URL drift"):
            bridge.get_state()
        assert bridge._trust_revoked is True and window.getter_calls == 0
    finally:
        bridge._close_from_host()


@pytest.mark.parametrize("mode", ("raises", "does-not-run"))
def test_failed_ui_dispatch_never_reuses_cached_or_loaded_url(mode, native_delegate):
    window = _NativeWindow()
    native = _UiThreadNative()
    window.native = native

    def failed_dispatch(callback):
        if mode == "raises":
            raise RuntimeError("untrusted dispatcher detail")
        return None

    native.Invoke = failed_dispatch
    with pytest.raises(WindowsWebBridgeTrustError):
        AutosportWebBridge._current_window_url(window)
    assert window.getter_calls == 0


def test_ui_thread_url_inspection_does_not_invoke_itself(native_delegate):
    window = _NativeWindow()
    native = _UiThreadNative()
    native.InvokeRequired = False
    native.on_ui = True
    window.native = native
    assert AutosportWebBridge._current_window_url(window) == TRUSTED_URL
    assert native.invocations == 0 and window.getter_calls == 0
