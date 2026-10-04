from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.windows_webview_shell as windows_webview_shell
from autosport.windows_webview_shell import (
    AutosportWebBridge,
    AutosportWebController,
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
        self.settings = {
            "WEBVIEW2_RUNTIME_PATH": None,
            "REMOTE_DEBUGGING_PORT": None,
        }

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


class _WitnessController(AutosportWebController):
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _WitnessEvent:
    def __init__(self) -> None:
        self.handlers: list[object] = []

    def __iadd__(self, handler: object):
        self.handlers.append(handler)
        return self


class _WitnessWindow:
    def __init__(self, browser_version: object) -> None:
        self.events = SimpleNamespace(
            initialized=_WitnessEvent(),
            before_load=_WitnessEvent(),
            closing=_WitnessEvent(),
        )
        self.native = SimpleNamespace(
            webview=SimpleNamespace(
                CoreWebView2=SimpleNamespace(
                    Environment=SimpleNamespace(
                        BrowserVersionString=browser_version,
                    )
                )
            )
        )
        self.destroyed = False

    def get_current_url(self) -> str:
        return "file:///autosport/windows_web/index.html"

    def destroy(self) -> None:
        self.destroyed = True


class _WitnessWebview:
    def __init__(self, browser_version: object, witness_path: Path) -> None:
        self.window = _WitnessWindow(browser_version)
        self.witness_path = witness_path
        self.witness_seen_before_start_return = False
        self.requested_gui: str | None = None
        self.settings = {
            "WEBVIEW2_RUNTIME_PATH": None,
            "REMOTE_DEBUGGING_PORT": None,
        }

    def create_window(self, *args, **kwargs):
        return self.window

    def start(self, *, gui: str, **kwargs) -> None:
        self.requested_gui = gui
        for handler in tuple(self.window.events.initialized.handlers):
            if handler("edgechromium") is False:
                raise RuntimeError("renderer qualification rejected")
        for handler in tuple(self.window.events.before_load.handlers):
            if handler() is False:
                raise RuntimeError("trusted document qualification rejected")
        self.witness_seen_before_start_return = self.witness_path.is_file()


def _install_witness_webview(
    monkeypatch,
    browser_version: object,
    witness_path: Path,
) -> _WitnessWebview:
    fake = _WitnessWebview(browser_version, witness_path)
    monkeypatch.setitem(sys.modules, "webview", fake)
    return fake


def test_actual_runtime_witness_is_durable_before_webview_start_returns(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    witness_path = workspace / "webview2-runtime-witness.json"
    fake = _install_witness_webview(monkeypatch, "154.0.2847.51", witness_path)
    controller = _WitnessController(workspace)
    bridge = AutosportWebBridge(controller)

    assert launch_windows_shell(
        bridge,
        storage_path=tmp_path / "webview2",
    ) == 0

    assert fake.requested_gui == "edgechromium"
    assert fake.witness_seen_before_start_return is True, (
        "The actual runtime witness must exist while the native WebView is live. "
        "Publishing only after webview.start() returns loses crash/kill and physical "
        "NVDA-session binding evidence."
    )
    assert controller.closed is True
    payload = json.loads(witness_path.read_text(encoding="utf-8"))
    assert payload == {
        "browser_version_string": "154.0.2847.51",
        "human_tested": False,
        "nvda_verified": False,
        "observation_source": "native_core_webview2_environment",
        "real_money_execution": False,
        "renderer": "edgechromium",
        "schema_version": 1,
        "whole_product_complete": False,
    }


def test_actual_runtime_witness_replaces_stale_prior_launch_before_bridge_injection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    witness_path = workspace / "webview2-runtime-witness.json"
    witness_path.write_text('{"browser_version_string":"stale"}\n', encoding="utf-8")
    fake = _install_witness_webview(monkeypatch, "154.0.2847.99", witness_path)
    bridge = AutosportWebBridge(_WitnessController(workspace))

    assert launch_windows_shell(
        bridge,
        storage_path=tmp_path / "webview2",
    ) == 0

    assert fake.witness_seen_before_start_return is True
    payload = json.loads(witness_path.read_text(encoding="utf-8"))
    assert payload["browser_version_string"] == "154.0.2847.99"
    assert payload["observation_source"] == "native_core_webview2_environment"


@pytest.mark.parametrize("browser_version", [None, "", " 154.0.0.0", "154.0\n"])
def test_invalid_actual_runtime_identity_never_publishes_witness(
    monkeypatch,
    tmp_path: Path,
    browser_version: object,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    witness_path = workspace / "webview2-runtime-witness.json"
    _install_witness_webview(monkeypatch, browser_version, witness_path)
    bridge = AutosportWebBridge(_WitnessController(workspace))

    with pytest.raises(WindowsWebViewUnavailable):
        launch_windows_shell(
            bridge,
            storage_path=tmp_path / "webview2",
        )

    assert witness_path.exists() is False


def test_runtime_witness_publication_failure_rejects_privileged_document(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    witness_path = workspace / "webview2-runtime-witness.json"
    _install_witness_webview(monkeypatch, "154.0.2847.51", witness_path)
    bridge = AutosportWebBridge(_WitnessController(workspace))

    def fail_publication(path: Path, browser_version: str) -> None:
        del path, browser_version
        raise OSError("simulated durable publication failure")

    monkeypatch.setattr(
        windows_webview_shell,
        "_write_webview2_runtime_witness",
        fail_publication,
    )

    with pytest.raises(WindowsWebViewUnavailable):
        launch_windows_shell(
            bridge,
            storage_path=tmp_path / "webview2",
        )

    assert witness_path.exists() is False

