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
    def __init__(self, browser_version: object = "154.0.2847.51") -> None:
        self.events = SimpleNamespace(
            initialized=_InitializedEvent(),
            before_load=_InitializedEvent(),
            closing=_InitializedEvent(),
        )
        self.original_url: str | None = None
        self.real_url = "http://127.0.0.1:41000/index.html"
        self.current_url = self.real_url
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
        return self.current_url

    def destroy(self) -> None:
        self.destroyed = True


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
        self.window.original_url = args[1]
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
        for handler in tuple(self.window.events.before_load.handlers):
            accepted = handler()
            if accepted is False:
                raise RuntimeError(
                    "window creation cancelled by trusted document witness"
                )


def _canonical_bridge(tmp_path: Path) -> tuple[AutosportWebBridge, AutosportWebController]:
    controller = AutosportWebController(tmp_path / "workspace")
    return AutosportWebBridge(controller), controller


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
    tmp_path: Path,
) -> None:
    fake = _install_fake_webview(monkeypatch, "mshtml")
    bridge, controller = _canonical_bridge(tmp_path)

    with pytest.raises(WindowsWebViewUnavailable):
        launch_windows_shell(bridge, storage_path=tmp_path / "webview2")

    assert fake.create_calls == 1
    assert fake.requested_gui == "edgechromium"
    assert fake.window.events.initialized.handlers, (
        "Passing gui='edgechromium' proves only requested renderer intent. "
        "The packaged shell must subscribe to pywebview's initialized event and "
        "observe the renderer that was actually selected."
    )
    assert controller._close_complete is True


def test_launch_accepts_only_after_initialized_renderer_identity_is_observed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = _install_fake_webview(monkeypatch, "edgechromium")
    bridge, controller = _canonical_bridge(tmp_path)

    assert launch_windows_shell(
        bridge,
        storage_path=tmp_path / "webview2",
    ) == 0

    assert fake.requested_gui == "edgechromium"
    assert fake.window.events.initialized.handlers, (
        "A successful machine launch without an initialized-stage renderer witness "
        "cannot qualify the exact packaged runtime as EdgeChromium/WebView2."
    )
    assert controller._close_complete is True


def test_launch_rejects_missing_initialized_renderer_witness(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fake = _install_fake_webview(
        monkeypatch,
        "edgechromium",
        emit_initialized=False,
    )
    bridge, controller = _canonical_bridge(tmp_path)

    with pytest.raises(
        WindowsWebViewUnavailable,
        match="without an observed EdgeChromium/WebView2 renderer witness",
    ):
        launch_windows_shell(bridge, storage_path=tmp_path / "webview2")

    assert fake.requested_gui == "edgechromium"
    assert fake.window.events.initialized.handlers
    assert controller._close_complete is True


@pytest.mark.parametrize("renderer", [None, "", True, "edgeChromium"])
def test_launch_rejects_noncanonical_renderer_witness(
    monkeypatch,
    tmp_path: Path,
    renderer: object,
) -> None:
    fake = _install_fake_webview(monkeypatch, renderer)
    bridge, controller = _canonical_bridge(tmp_path)

    with pytest.raises(WindowsWebViewUnavailable):
        launch_windows_shell(bridge, storage_path=tmp_path / "webview2")

    assert fake.requested_gui == "edgechromium"
    assert controller._close_complete is True


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
        self.original_url: str | None = None
        self.real_url = "http://127.0.0.1:42000/index.html"
        self.current_url = self.real_url
        self.destroyed = False

    def get_current_url(self) -> str:
        return self.current_url

    def destroy(self) -> None:
        self.destroyed = True


class _WitnessWebview:
    def __init__(self, browser_version: object, witness_path: Path) -> None:
        self.window = _WitnessWindow(browser_version)
        self.witness_path = witness_path
        self.witness_seen_before_start_return = False
        self.requested_gui: str | None = None
        self.before_before_load = None
        self.settings = {
            "WEBVIEW2_RUNTIME_PATH": None,
            "REMOTE_DEBUGGING_PORT": None,
        }

    def create_window(self, *args, **kwargs):
        self.window.original_url = args[1]
        return self.window

    def start(self, *, gui: str, **kwargs) -> None:
        self.requested_gui = gui
        for handler in tuple(self.window.events.initialized.handlers):
            if handler("edgechromium") is False:
                raise RuntimeError("renderer qualification rejected")
        if self.before_before_load is not None:
            self.before_before_load()
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
    controller = AutosportWebController(workspace)
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
    assert controller._close_complete is True
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
    controller = AutosportWebController(workspace)
    bridge = AutosportWebBridge(controller)

    assert launch_windows_shell(
        bridge,
        storage_path=tmp_path / "webview2",
    ) == 0

    assert fake.witness_seen_before_start_return is True
    assert controller._close_complete is True
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
    controller = AutosportWebController(workspace)
    bridge = AutosportWebBridge(controller)

    with pytest.raises(WindowsWebViewUnavailable):
        launch_windows_shell(
            bridge,
            storage_path=tmp_path / "webview2",
        )

    assert witness_path.exists() is False
    assert controller._close_complete is True


def test_inflight_runtime_witness_writer_rebind_fails_closed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    witness_path = workspace / "webview2-runtime-witness.json"
    fake = _install_witness_webview(
        monkeypatch,
        "154.0.2847.51",
        witness_path,
    )
    controller = AutosportWebController(workspace)
    bridge = AutosportWebBridge(controller)
    hostile_called = []

    def hostile_writer(path: Path, browser_version: str) -> None:
        hostile_called.append((path, browser_version))

    def rebind_writer() -> None:
        monkeypatch.setattr(
            windows_webview_shell,
            "_write_webview2_runtime_witness",
            hostile_writer,
        )

    fake.before_before_load = rebind_writer

    with pytest.raises(WindowsWebViewUnavailable):
        launch_windows_shell(
            bridge,
            storage_path=tmp_path / "webview2",
        )

    assert hostile_called == []
    assert witness_path.exists() is False
    assert controller._close_complete is True


def test_runtime_witness_publication_failure_rejects_privileged_document(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    witness_path = workspace / "webview2-runtime-witness.json"
    _install_witness_webview(monkeypatch, "154.0.2847.51", witness_path)
    controller = AutosportWebController(workspace)
    bridge = AutosportWebBridge(controller)

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
    assert controller._close_complete is True

