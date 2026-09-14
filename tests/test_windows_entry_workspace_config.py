from __future__ import annotations

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


def test_valid_workspace_configuration_delegates_to_gui(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    gui_main = MagicMock(return_value=7)
    fake_gui_module = types.ModuleType("autosport.windows_gui")
    fake_gui_module.main = gui_main

    with (
        patch("autosport.paths.default_workspace", return_value=workspace) as validate_workspace,
        patch.object(windows_entry, "_show_workspace_configuration_error") as show_error,
        patch.dict(sys.modules, {"autosport.windows_gui": fake_gui_module}),
    ):
        exit_code = windows_entry._run_interactive_gui()

    assert exit_code == 7
    validate_workspace.assert_called_once_with()
    show_error.assert_not_called()
    gui_main.assert_called_once_with()


def test_machine_mode_does_not_validate_interactive_workspace() -> None:
    install_layout = MagicMock()
    run_diagnostic = MagicMock(return_value=0)
    fake_layout_module = types.ModuleType("autosport.windows_layout")
    fake_layout_module.install_compact_windows_layout = install_layout
    fake_diagnostic_module = types.ModuleType("autosport.diagnostic")
    fake_diagnostic_module.run_machine_diagnostic = run_diagnostic

    with (
        patch.dict(
            sys.modules,
            {
                "autosport.windows_layout": fake_layout_module,
                "autosport.diagnostic": fake_diagnostic_module,
            },
        ),
        patch(
            "autosport.paths.default_workspace",
            side_effect=ValueError("AUTOSPORT_WORKSPACE must be an absolute path"),
        ) as validate_workspace,
        patch.object(windows_entry, "_show_workspace_configuration_error") as show_error,
    ):
        exit_code = windows_entry.main(["--diagnostic-output", "diagnostic.json"])

    assert exit_code == 0
    install_layout.assert_called_once_with()
    run_diagnostic.assert_called_once_with("diagnostic.json")
    validate_workspace.assert_not_called()
    show_error.assert_not_called()


def test_native_workspace_error_dialog_is_actionable_and_accessible_boundary() -> None:
    user32 = MagicMock()
    fake_windll = types.SimpleNamespace(user32=user32)

    with patch("ctypes.windll", fake_windll, create=True):
        windows_entry._show_workspace_configuration_error(
            "AUTOSPORT_WORKSPACE must be an absolute path"
        )

    user32.MessageBoxW.assert_called_once()
    hwnd, message, title, flags = user32.MessageBoxW.call_args.args
    assert hwnd is None
    assert "AUTOSPORT_WORKSPACE must be an absolute path" in message
    assert "потім перезапустіть Автоспорт" in message
    assert "Economic і live state не змінено" in message
    assert "помилка конфігурації workspace" in title
    assert flags & 0x00000010