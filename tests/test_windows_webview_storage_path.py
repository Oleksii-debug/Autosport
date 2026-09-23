from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from autosport.paths import default_webview_storage_path
from autosport.windows_webview_shell import (
    WindowsWebViewUnavailable,
    launch_windows_shell,
)


class _EventHook:
    def __init__(self) -> None:
        self.callback = None

    def __iadd__(self, callback):
        self.callback = callback
        return self


class _Window:
    def __init__(self) -> None:
        self.events = SimpleNamespace(
            initialized=_EventHook(),
            before_load=_EventHook(),
        )


class _Bridge:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _fake_webview(calls: dict[str, object]) -> SimpleNamespace:
    window = _Window()

    def create_window(*args, **kwargs):
        calls["create_window"] = (args, kwargs)
        calls["window"] = window
        return window

    def start(**kwargs):
        calls["start"] = dict(kwargs)
        callback = window.events.initialized.callback
        assert callback is not None
        callback(kwargs["gui"])

    return SimpleNamespace(create_window=create_window, start=start)


def test_webview_storage_path_is_per_user_cwd_independent_and_unicode_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_app_data = tmp_path / "Користувач Тест" / "AppData Local"
    workspace_override = tmp_path / "інший workspace"
    cwd_a = tmp_path / "cwd a"
    cwd_b = tmp_path / "cwd б"
    cwd_a.mkdir()
    cwd_b.mkdir()

    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv("AUTOSPORT_WORKSPACE", str(workspace_override))

    monkeypatch.chdir(cwd_a)
    first = default_webview_storage_path()
    monkeypatch.chdir(cwd_b)
    second = default_webview_storage_path()

    expected = local_app_data / "Autosport" / "webview2"
    assert first == expected
    assert second == expected
    assert first.is_absolute()
    assert workspace_override not in first.parents


def test_webview_storage_rejects_relative_local_app_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", "relative-app-data")

    with pytest.raises(ValueError, match="LOCALAPPDATA must be an absolute path"):
        default_webview_storage_path()


def test_windows_shell_passes_exact_canonical_storage_path_to_pywebview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_app_data = tmp_path / "Profile With Spaces" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("WEBVIEW2_USER_DATA_FOLDER", raising=False)

    calls: dict[str, object] = {}
    monkeypatch.setitem(sys.modules, "webview", _fake_webview(calls))
    bridge = _Bridge()

    assert launch_windows_shell(bridge) == 0

    start = calls["start"]
    assert isinstance(start, dict)
    assert start["gui"] == "edgechromium"
    assert start["storage_path"] == str(
        local_app_data / "Autosport" / "webview2"
    )
    assert Path(start["storage_path"]).is_absolute()
    assert bridge.closed is True


def test_external_webview_user_data_override_fails_before_shell_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv(
        "WEBVIEW2_USER_DATA_FOLDER",
        str(tmp_path / "externally-retargeted-profile"),
    )

    calls: dict[str, object] = {}
    monkeypatch.setitem(sys.modules, "webview", _fake_webview(calls))

    with pytest.raises(
        WindowsWebViewUnavailable,
        match="refused an external WebView2 user-data-folder override",
    ):
        launch_windows_shell(_Bridge())

    assert calls == {}
