from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from autosport.windows_webview_shell import (
    WindowsWebViewUnavailable,
    launch_windows_shell,
)


class _InitializedEvent:
    def __init__(self) -> None:
        self.handlers: list[object] = []

    def __iadd__(self, handler: object):
        self.handlers.append(handler)
        return self


class _FakeWindow:
    def __init__(self) -> None:
        self.events = SimpleNamespace(initialized=_InitializedEvent())


class _FakeWebview:
    def __init__(
        self,
        selected_renderer: object,
        *,
        emit_initialized: bool = True,
    ) -> None:
        self.selected_renderer = selected_renderer
        self.emit_initialized = emit_initialized
        self.window = _FakeWindow()
        self.requested_gui: str | None = None
        self.create_calls = 0

    def create_window(self, *args, **kwargs):
        self.create_calls += 1
        return self.window

    def start(self, *, gui: str, **kwargs) -> None:
        self.requested_gui = gui
        if not self.emit_initialized:
            return
        for handler in tuple(self.window.events.initialized.handlers):
            accepted = handler(self.selected_renderer)
            if accepted is False:
                raise RuntimeError(
                    "window creation cancelled by initialized renderer witness"
                )


class _Bridge:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _install_fake_webview(
    monkeypatch,
    selected_renderer: object,
    *,
    emit_initialized: bool = True,
) -> _FakeWebview:
    fake = _FakeWebview(
        selected_renderer,
        emit_initialized=emit_initialized,
    )
    monkeypatch.setitem(sys.modules, "webview", fake)
    return fake


def test_launch_rejects_initialized_renderer_mismatch_even_when_edgechromium_was_requested(
    monkeypatch,
) -> None:
    fake = _install_fake_webview(monkeypatch, "mshtml")
    bridge = _Bridge()

    with pytest.raises(WindowsWebViewUnavailable):
        launch_windows_shell(bridge)

    assert fake.create_calls == 1
    assert fake.requested_gui == "edgechromium"
    assert fake.window.events.initialized.handlers, (
        "Passing gui='edgechromium' proves only requested renderer intent. "
        "The packaged shell must subscribe to pywebview's initialized event and "
        "observe the renderer that was actually selected."
    )
    assert bridge.closed is True


def test_launch_accepts_only_after_initialized_renderer_identity_is_observed(
    monkeypatch,
) -> None:
    fake = _install_fake_webview(monkeypatch, "edgechromium")
    bridge = _Bridge()

    assert launch_windows_shell(bridge) == 0

    assert fake.requested_gui == "edgechromium"
    assert fake.window.events.initialized.handlers, (
        "A successful machine launch without an initialized-stage renderer witness "
        "cannot qualify the exact packaged runtime as EdgeChromium/WebView2."
    )
    assert bridge.closed is True


def test_launch_rejects_missing_initialized_renderer_witness(
    monkeypatch,
) -> None:
    fake = _install_fake_webview(
        monkeypatch,
        "edgechromium",
        emit_initialized=False,
    )
    bridge = _Bridge()

    with pytest.raises(
        WindowsWebViewUnavailable,
        match="without an observed EdgeChromium/WebView2 renderer witness",
    ):
        launch_windows_shell(bridge)

    assert fake.requested_gui == "edgechromium"
    assert fake.window.events.initialized.handlers
    assert bridge.closed is True


@pytest.mark.parametrize("renderer", [None, "", True, "edgeChromium"])
def test_launch_rejects_noncanonical_renderer_witness(
    monkeypatch,
    renderer: object,
) -> None:
    fake = _install_fake_webview(monkeypatch, renderer)
    bridge = _Bridge()

    with pytest.raises(WindowsWebViewUnavailable):
        launch_windows_shell(bridge)

    assert fake.requested_gui == "edgechromium"
    assert bridge.closed is True
