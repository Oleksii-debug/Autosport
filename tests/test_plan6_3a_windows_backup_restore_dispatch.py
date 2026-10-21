"""Plan 6 §3А: the packaged Windows data entry exposes canonical private backup/restore.

The delegated commands are already implemented and independently tested by the
canonical CLI. These tests prove the Windows entrypoint reaches them without
introducing a parallel writer, fake success, or distinct workspace authority.
Native Windows EXE/NVDA release acceptance is not claimed.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from autosport import data_tools_entry


@pytest.mark.parametrize(
    "command,args",
    [
        ("backup-workspace", ["--workspace", "C:/paper workspace", "--output", "C:/backup.zip"]),
        ("restore-workspace", ["--archive", "C:/backup.zip", "--destination", "C:/restored"]),
    ],
)
def test_windows_data_entry_delegates_to_canonical_cli(command, args):
    with patch("autosport.cli.main", return_value=0) as cli:
        assert data_tools_entry._dispatch(command, args) == 0
    cli.assert_called_once_with([command, *args])


@pytest.mark.parametrize("command", ["backup-workspace", "restore-workspace"])
def test_windows_data_entry_preserves_canonical_failure_status(command):
    with patch("autosport.cli.main", return_value=3) as cli:
        assert data_tools_entry._dispatch(command, ["--invalid"]) == 3
    cli.assert_called_once_with([command, "--invalid"])


def test_windows_data_entry_discloses_real_backup_restore_commands():
    usage = data_tools_entry._USAGE
    assert "Autosport-Data.exe backup-workspace --workspace" in usage
    assert "Autosport-Data.exe restore-workspace --archive" in usage
    assert "Plan 5 economic lock" in usage
    assert "NEW empty workspace" in usage


def test_windows_data_entry_unknown_command_does_not_become_backup(capsys):
    with patch("autosport.cli.main") as cli:
        assert data_tools_entry._dispatch("restore-other-workspace", []) == 2
    cli.assert_not_called()
