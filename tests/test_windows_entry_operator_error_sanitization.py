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
    assert "Economic і live state не змінено" in message
    assert secret not in message
    assert "ValueError" not in message


def test_workspace_access_message_does_not_announce_exception_detail(tmp_path: Path) -> None:
    secret = "secret-bearing-filesystem-detail"
    message = windows_entry._workspace_access_error_message(
        tmp_path / "workspace",
        PermissionError(secret),
    )

    assert str(tmp_path / "workspace") in message
    assert "AUTOSPORT_WORKSPACE" in message
    assert "Economic і live state не змінено" in message
    assert secret not in message
    assert "PermissionError" not in message
    assert "OSError" not in message


def test_webview_launch_failure_does_not_announce_exception_detail(tmp_path: Path) -> None:
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

    def fail_launch(_bridge: object) -> int:
        raise WindowsWebViewUnavailable(secret)

    shell_module.launch_windows_shell = fail_launch
    show_error = MagicMock()

    with (
        patch("autosport.paths.default_workspace", return_value=tmp_path / "workspace"),
        patch.object(windows_entry, "_probe_workspace_writable"),
        patch.object(windows_entry, "_show_startup_error", show_error),
        patch.dict(
            sys.modules,
            {
                "autosport.webview2_runtime_deployment": runtime_module,
                "autosport.windows_webview_emergency_stop": stop_module,
                "autosport.windows_webview_shell": shell_module,
            },
        ),
    ):
        assert windows_entry._run_interactive_gui() == 3

    show_error.assert_called_once_with(windows_entry._WEBVIEW2_STARTUP_ERROR)
    shown = show_error.call_args.args[0]
    assert secret not in shown
    assert "WindowsWebViewUnavailable" not in shown
    assert "RuntimeError" not in shown
