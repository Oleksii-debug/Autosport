from __future__ import annotations

import sys
from collections import UserDict
from pathlib import Path
from types import SimpleNamespace

import pytest

from autosport.paths import default_webview_storage_path
from autosport.windows_webview_shell import (
    _PYWEBVIEW_RELEASE_SETTINGS,
    _WEBVIEW2_ENVIRONMENT_OVERRIDES,
    WindowsWebViewUnavailable,
    _probe_webview_storage_writable,
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


def _fake_webview(
    calls: dict[str, object],
    *,
    settings: dict[str, object] | None = None,
) -> SimpleNamespace:
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

    release_settings = {name: None for name in _PYWEBVIEW_RELEASE_SETTINGS}
    if settings:
        release_settings.update(settings)
    return SimpleNamespace(
        create_window=create_window,
        start=start,
        settings=release_settings,
    )


def _clear_webview2_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _WEBVIEW2_ENVIRONMENT_OVERRIDES:
        monkeypatch.delenv(name, raising=False)


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
    _clear_webview2_environment_overrides(monkeypatch)

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


def test_prevalidated_webview_storage_path_is_not_reresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prevalidated = tmp_path / "locked Local" / "Autosport" / "webview2"
    _clear_webview2_environment_overrides(monkeypatch)

    def forbidden_reresolve() -> Path:
        raise AssertionError("prevalidated WebView storage must not be re-resolved")

    monkeypatch.setattr(
        "autosport.windows_webview_shell.default_webview_storage_path",
        forbidden_reresolve,
    )
    calls: dict[str, object] = {}
    monkeypatch.setitem(sys.modules, "webview", _fake_webview(calls))

    assert launch_windows_shell(_Bridge(), storage_path=prevalidated) == 0

    start = calls["start"]
    assert isinstance(start, dict)
    assert start["storage_path"] == str(prevalidated)
    assert Path(start["storage_path"]).is_absolute()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", "C:/unqualified-runtime"),
        ("WEBVIEW2_USER_DATA_FOLDER", "C:/externally-retargeted-profile"),
        (
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
            "--remote-debugging-port=9222",
        ),
        ("WEBVIEW2_RELEASE_CHANNEL_PREFERENCE", "1"),
        ("WEBVIEW2_CHANNEL_SEARCH_KIND", "1"),
        ("WEBVIEW2_RELEASE_CHANNELS", "1"),
        ("WEBVIEW2_WAIT_FOR_SCRIPT_DEBUGGER", "1"),
        ("WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER", "autosport-debug-pipe"),
    ],
)
def test_external_webview_environment_override_fails_before_shell_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    _clear_webview2_environment_overrides(monkeypatch)
    monkeypatch.setenv(name, value)

    calls: dict[str, object] = {}
    monkeypatch.setitem(sys.modules, "webview", _fake_webview(calls))

    with pytest.raises(
        WindowsWebViewUnavailable,
        match="заблокував зовнішнє перевизначення WebView2",
    ):
        launch_windows_shell(_Bridge())

    assert calls == {}


@pytest.mark.parametrize("name", _WEBVIEW2_ENVIRONMENT_OVERRIDES)
@pytest.mark.parametrize("value", [" ", "\t", " \t "])
def test_whitespace_webview_environment_override_fails_before_shell_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    _clear_webview2_environment_overrides(monkeypatch)
    monkeypatch.setenv(name, value)

    calls: dict[str, object] = {}
    monkeypatch.setitem(sys.modules, "webview", _fake_webview(calls))

    with pytest.raises(
        WindowsWebViewUnavailable,
        match="заблокував зовнішнє перевизначення WebView2",
    ):
        launch_windows_shell(_Bridge())

    assert calls == {}


def test_empty_webview_environment_overrides_do_not_retarget_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    for name in _WEBVIEW2_ENVIRONMENT_OVERRIDES:
        monkeypatch.setenv(name, "")

    calls: dict[str, object] = {}
    monkeypatch.setitem(sys.modules, "webview", _fake_webview(calls))

    assert launch_windows_shell(_Bridge()) == 0
    assert "create_window" in calls
    assert calls["start"] == {
        "gui": "edgechromium",
        "storage_path": str(tmp_path / "Local" / "Autosport" / "webview2"),
    }


def test_pywebview_userdict_settings_shape_allows_canonical_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    _clear_webview2_environment_overrides(monkeypatch)

    calls: dict[str, object] = {}
    fake = _fake_webview(calls)
    fake.settings = UserDict(
        {
            "WEBVIEW2_RUNTIME_PATH": None,
            "REMOTE_DEBUGGING_PORT": None,
        }
    )
    monkeypatch.setitem(sys.modules, "webview", fake)

    assert launch_windows_shell(_Bridge()) == 0
    assert "create_window" in calls
    assert calls["start"] == {
        "gui": "edgechromium",
        "storage_path": str(tmp_path / "Local" / "Autosport" / "webview2"),
    }


def test_pywebview_settings_missing_controlled_key_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    _clear_webview2_environment_overrides(monkeypatch)

    calls: dict[str, object] = {}
    fake = _fake_webview(calls)
    fake.settings = UserDict({"WEBVIEW2_RUNTIME_PATH": None})
    monkeypatch.setitem(sys.modules, "webview", fake)

    with pytest.raises(
        WindowsWebViewUnavailable,
        match="не може підтвердити параметри pywebview",
    ):
        launch_windows_shell(_Bridge())

    assert calls == {}


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("WEBVIEW2_RUNTIME_PATH", "C:/fixed-version-runtime"),
        ("REMOTE_DEBUGGING_PORT", 9222),
    ],
)
def test_external_pywebview_release_setting_fails_before_shell_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    value: object,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    _clear_webview2_environment_overrides(monkeypatch)

    calls: dict[str, object] = {}
    fake = _fake_webview(calls, settings={setting: value})
    monkeypatch.setitem(sys.modules, "webview", fake)

    with pytest.raises(
        WindowsWebViewUnavailable,
        match="заблокував некваліфікований параметр pywebview",
    ):
        launch_windows_shell(_Bridge())

    assert calls == {}



def test_webview_storage_probe_creates_root_and_leaves_no_probe_artifact(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "Користувач Тест" / "Local" / "Autosport" / "webview2"

    _probe_webview_storage_writable(storage)

    assert storage.is_dir()
    assert list(storage.iterdir()) == []


def test_webview_storage_probe_classifies_write_failure_without_raw_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "secret-bearing-webview-storage-detail"

    def deny_probe(*args, **kwargs):
        del args, kwargs
        raise PermissionError(secret)

    monkeypatch.setattr(
        "autosport.storage_preflight.tempfile.NamedTemporaryFile",
        deny_probe,
    )

    with pytest.raises(WindowsWebViewUnavailable) as captured:
        _probe_webview_storage_writable(tmp_path / "Local" / "Autosport" / "webview2")

    assert captured.value.reason == "storage"
    assert secret not in str(captured.value)
    assert "not writable" in str(captured.value)


def test_unwritable_webview_storage_fails_before_webview_import_or_window_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    _clear_webview2_environment_overrides(monkeypatch)
    calls: dict[str, object] = {}
    monkeypatch.setitem(sys.modules, "webview", _fake_webview(calls))

    def fail_probe(_path: Path) -> None:
        raise WindowsWebViewUnavailable(
            "bounded storage failure",
            reason="storage",
        )

    monkeypatch.setattr(
        "autosport.windows_webview_shell._probe_webview_storage_writable",
        fail_probe,
    )

    with pytest.raises(WindowsWebViewUnavailable) as captured:
        launch_windows_shell(_Bridge())

    assert captured.value.reason == "storage"
    assert calls == {}
