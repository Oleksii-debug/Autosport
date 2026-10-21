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
