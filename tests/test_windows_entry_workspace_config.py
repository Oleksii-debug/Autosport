from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

from autosport import windows_entry


def test_invalid_workspace_configuration_is_reported_before_gui_import(tmp_path: Path) -> None:
    detail = "AUTOSPORT_WORKSPACE must be an absolute path"

    with (
        patch("autosport.paths.default_workspace", side_effect=ValueError(detail)),
        patch.object(windows_entry, "_show_workspace_configuration_error") as show_error,
        patch.dict(sys.modules, {"autosport.windows_gui": None}),
    ):
        exit_code = windows_entry._run_interactive_gui()

    assert exit_code == 2
    show_error.assert_called_once_with(detail)


def test_real_relative_workspace_override_is_rejected_before_gui_import() -> None:
    with (
        patch.dict(os.environ, {"AUTOSPORT_WORKSPACE": "relative-workspace"}, clear=False),
        patch.object(windows_entry, "_show_workspace_configuration_error") as show_error,
        patch.dict(sys.modules, {"autosport.windows_gui": None}),
    ):
        exit_code = windows_entry._run_interactive_gui()

    assert exit_code == 2
    show_error.assert_called_once()
    assert "absolute path" in show_error.call_args.args[0]


def test_valid_workspace_configuration_delegates_to_webview_shell(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runtime_preflight = MagicMock(
        return_value=types.SimpleNamespace(available=True)
    )
    controller = object()
    bridge = object()
    build_controller = MagicMock(return_value=controller)
    build_bridge = MagicMock(return_value=bridge)
    launch_shell = MagicMock(return_value=7)

    runtime_module = types.ModuleType("autosport.webview2_runtime_deployment")
    runtime_module.ensure_webview2_runtime = runtime_preflight
    stop_module = types.ModuleType("autosport.windows_webview_emergency_stop")
    stop_module.EmergencyStopWebController = build_controller
    shell_module = types.ModuleType("autosport.windows_webview_shell")
    shell_module.AutosportWebBridge = build_bridge
    shell_module.WindowsWebViewUnavailable = RuntimeError
    shell_module.launch_windows_shell = launch_shell

    webview_storage = tmp_path / "webview2"
    with (
        patch("autosport.paths.default_workspace", return_value=workspace) as validate_workspace,
        patch(
            "autosport.paths.default_webview_storage_path",
            return_value=webview_storage,
        ) as validate_webview_storage,
        patch.object(windows_entry, "_probe_workspace_writable") as probe_workspace,
        patch.object(windows_entry, "_show_workspace_configuration_error") as show_error,
        patch.dict(
            sys.modules,
            {
                "autosport.webview2_runtime_deployment": runtime_module,
                "autosport.windows_webview_emergency_stop": stop_module,
                "autosport.windows_webview_shell": shell_module,
            },
        ),
    ):
        exit_code = windows_entry._run_interactive_gui()

    assert exit_code == 7
    validate_workspace.assert_called_once_with()
    validate_webview_storage.assert_called_once_with()
    show_error.assert_not_called()
    runtime_preflight.assert_called_once_with()
    probe_workspace.assert_called_once_with(workspace)
    build_controller.assert_called_once_with(workspace)
    build_bridge.assert_called_once_with(controller)
    launch_shell.assert_called_once_with(bridge, storage_path=webview_storage)
    assert (workspace / ".interactive-run.lock").is_file()
    assert (webview_storage.parent / ".interactive-run.lock").is_file()

    from autosport.workspace_lock import WorkspaceInteractiveLock

    # Shell exit must release both OS ownership records while leaving the hardened
    # persistent sidecars intact for the next launch.
    with WorkspaceInteractiveLock(workspace):
        pass
    with WorkspaceInteractiveLock(webview_storage.parent):
        pass


def test_overlapping_workspace_and_webview_roots_fail_before_writability_or_runtime(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "shared"
    webview_storage = workspace / "webview2"

    with (
        patch("autosport.paths.default_workspace", return_value=workspace),
        patch(
            "autosport.paths.default_webview_storage_path",
            return_value=webview_storage,
        ),
        patch.object(
            windows_entry,
            "_probe_workspace_writable",
            side_effect=AssertionError("overlap must fail before workspace mutation"),
        ),
        patch.object(windows_entry, "_show_workspace_configuration_error") as show_error,
        patch.dict(
            sys.modules,
            {
                "autosport.webview2_runtime_deployment": types.SimpleNamespace(
                    ensure_webview2_runtime=lambda: (_ for _ in ()).throw(
                        AssertionError("overlap must fail before runtime deployment")
                    )
                )
            },
        ),
    ):
        exit_code = windows_entry._run_interactive_gui()

    assert exit_code == 2
    show_error.assert_called_once()
    assert "disjoint trees" in show_error.call_args.args[0]


def test_machine_mode_does_not_validate_interactive_workspace() -> None:
    run_diagnostic = MagicMock(return_value=0)
    fake_diagnostic_module = types.ModuleType("autosport.diagnostic")
    fake_diagnostic_module.run_machine_diagnostic = run_diagnostic

    with (
        patch.dict(
            sys.modules,
            {"autosport.diagnostic": fake_diagnostic_module},
        ),
        patch(
            "autosport.paths.default_workspace",
            side_effect=ValueError("AUTOSPORT_WORKSPACE must be an absolute path"),
        ) as validate_workspace,
        patch.object(windows_entry, "_show_workspace_configuration_error") as show_error,
    ):
        exit_code = windows_entry.main(["--diagnostic-output", "diagnostic.json"])

    assert exit_code == 0
    run_diagnostic.assert_called_once_with("diagnostic.json")
    validate_workspace.assert_not_called()
    show_error.assert_not_called()


def test_native_workspace_error_dialog_is_actionable_and_accessible_boundary() -> None:
    user32 = MagicMock()
    fake_windll = types.SimpleNamespace(user32=user32)
    detail = "AUTOSPORT_WORKSPACE must be an absolute path"

    with patch("ctypes.windll", fake_windll, create=True):
        windows_entry._show_workspace_configuration_error(detail)

    user32.MessageBoxW.assert_called_once()
    hwnd, message, title, flags = user32.MessageBoxW.call_args.args
    assert hwnd is None
    assert detail not in message
    assert "AUTOSPORT_WORKSPACE" in message
    assert "LOCALAPPDATA" in message
    assert "потім перезапустіть Автоспорт" in message
    assert "Economic і live state не змінено" in message
    assert "помилка конфігурації workspace" in title
    assert flags & 0x00000010
