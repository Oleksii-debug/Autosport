from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

from autosport import windows_entry


def test_workspace_configuration_dialog_does_not_announce_internal_detail() -> None:
    secret = "secret-bearing-workspace-configuration-detail"
    user32 = MagicMock()
    fake_windll = types.SimpleNamespace(user32=user32)

    with patch("ctypes.windll", fake_windll, create=True):
        windows_entry._show_workspace_configuration_error(secret)

    user32.MessageBoxW.assert_called_once()
    _hwnd, message, _title, _flags = user32.MessageBoxW.call_args.args
    assert "AUTOSPORT_WORKSPACE" in message
    assert "LOCALAPPDATA" in message
    assert "Економічний стан і стан спостереження не змінено." in message
    assert secret not in message
    assert "ValueError" not in message


def test_workspace_access_message_does_not_announce_exception_detail(tmp_path: Path) -> None:
    secret = "secret-bearing-filesystem-detail"
    message = windows_entry._workspace_access_error_message(
        tmp_path / "workspace",
        PermissionError(secret),
    )

    assert str(tmp_path / "workspace") not in message
    assert "шлях приховано" in message
    assert "AUTOSPORT_WORKSPACE" in message
    assert "Економічний стан і стан виконання не змінено." in message
    assert secret not in message
    assert "PermissionError" not in message
    assert "OSError" not in message


def test_webview_launch_failure_keeps_native_stop_choice_and_redacts_detail(tmp_path: Path) -> None:
    secret = "secret-bearing-webview-launch-detail"
    runtime_module = types.ModuleType("autosport.webview2_runtime_deployment")
    runtime_module.ensure_webview2_runtime = lambda: types.SimpleNamespace(available=True)

    stop_module = types.ModuleType("autosport.windows_webview_emergency_stop")
    stop_module.EmergencyStopWebController = lambda _workspace: object()
    shell_module = types.ModuleType("autosport.windows_webview_shell")

    class WindowsWebViewUnavailable(RuntimeError):
        pass

    shell_module.WindowsWebViewUnavailable = WindowsWebViewUnavailable
    shell_module.AutosportWebBridge = lambda _controller: object()

    def fail_launch(_bridge: object, *, storage_path: Path) -> int:
        assert storage_path.is_absolute()
        raise WindowsWebViewUnavailable(secret)

    shell_module.launch_windows_shell = fail_launch
    offer_stop = MagicMock()
    workspace = tmp_path / "workspace"

    with (
        patch("autosport.webview2_release_environment.active_webview2_environment_overrides", return_value=()),
        patch.object(windows_entry, "_offer_native_emergency_stop", offer_stop),
        patch.dict(
            sys.modules,
            {
                "autosport.webview2_runtime_deployment": runtime_module,
                "autosport.windows_webview_emergency_stop": stop_module,
                "autosport.windows_webview_shell": shell_module,
            },
        ),
    ):
        assert windows_entry._run_owned_interactive_gui(workspace, tmp_path / "webview-storage") == 3

    offer_stop.assert_called_once_with(workspace, windows_entry._WEBVIEW2_STARTUP_ERROR)
    shown = offer_stop.call_args.args[1]
    assert "Microsoft Edge WebView2 Runtime" in shown
    assert "локального сховища WebView2" in shown
    assert secret not in shown
    assert "WindowsWebViewUnavailable" not in shown
    assert "RuntimeError" not in shown


def test_webview_storage_failure_offers_distinct_native_stop_choice(
    tmp_path: Path,
) -> None:
    secret = "secret-bearing-webview-storage-detail"
    runtime_module = types.ModuleType("autosport.webview2_runtime_deployment")
    runtime_module.ensure_webview2_runtime = lambda: types.SimpleNamespace(available=True)

    stop_module = types.ModuleType("autosport.windows_webview_emergency_stop")
    stop_module.EmergencyStopWebController = lambda _workspace: object()
    shell_module = types.ModuleType("autosport.windows_webview_shell")

    class WindowsWebViewUnavailable(RuntimeError):
        def __init__(self, message: str, *, reason: str = "runtime") -> None:
            super().__init__(message)
            self.reason = reason

    shell_module.WindowsWebViewUnavailable = WindowsWebViewUnavailable
    shell_module.AutosportWebBridge = lambda _controller: object()

    def fail_launch(_bridge: object, *, storage_path: Path) -> int:
        assert storage_path.is_absolute()
        raise WindowsWebViewUnavailable(secret, reason="storage")

    shell_module.launch_windows_shell = fail_launch
    offer_stop = MagicMock()
    workspace = tmp_path / "workspace"

    with (
        patch("autosport.webview2_release_environment.active_webview2_environment_overrides", return_value=()),
        patch.object(windows_entry, "_offer_native_emergency_stop", offer_stop),
        patch.dict(
            sys.modules,
            {
                "autosport.webview2_runtime_deployment": runtime_module,
                "autosport.windows_webview_emergency_stop": stop_module,
                "autosport.windows_webview_shell": shell_module,
            },
        ),
    ):
        assert windows_entry._run_owned_interactive_gui(workspace, tmp_path / "webview-storage") == 2

    offer_stop.assert_called_once_with(workspace, windows_entry._WEBVIEW2_STORAGE_ERROR)
    shown = offer_stop.call_args.args[1]
    assert "LOCALAPPDATA" in shown
    assert "Права адміністратора не потрібні" in shown
    assert "Runtime" not in shown
    assert secret not in shown


def test_workspace_access_message_redacts_secret_in_workspace_path(tmp_path: Path, monkeypatch) -> None:
    canary = "synthetic-access-token-for-test-12345"
    monkeypatch.setenv("AUTOSPORT_API_KEY", canary)
    workspace = tmp_path / f"folder-{canary}" / "workspace"

    message = windows_entry._workspace_access_error_message(
        workspace, PermissionError("synthetic-filesystem-error"),
    )

    assert canary not in message
    assert "шлях приховано" in message
    assert "AUTOSPORT_WORKSPACE" in message
    assert "synthetic-filesystem-error" not in message


def test_native_configuration_error_never_renders_arbitrary_path_or_exception() -> None:
    from autosport.localization import text

    error = "unexpected secret=CANARY\ncaller-provided detail"
    user32 = MagicMock()
    with patch("ctypes.windll", types.SimpleNamespace(user32=user32), create=True):
        windows_entry._show_workspace_configuration_error(error)
    message = user32.MessageBoxW.call_args.args[1]
    assert error not in message
    assert "CANARY" not in message
    assert "caller-provided detail" not in message
    assert message == text("ui.windows.workspace_configuration.message")


def test_native_workspace_error_does_not_echo_arbitrary_unicode_path_and_oserror(tmp_path: Path) -> None:
    workspace = tmp_path / "користувач-CANARY-непоказувати"
    message = windows_entry._workspace_access_error_message(
        workspace, OSError("access-denied-CANARY\nCredentials: secret"),
    )
    assert "CANARY" not in message
    assert "Credentials" not in message
    assert str(workspace) not in message
    assert "AUTOSPORT_WORKSPACE" in message
    assert "Права адміністратора не потрібні" in message
