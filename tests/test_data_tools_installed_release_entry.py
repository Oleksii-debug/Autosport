"""Packaged Autosport-Data must expose the canonical PAPER install lifecycle."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import autosport.data_tools_entry as entry


def test_packaged_data_exposes_both_existing_install_lifecycle_commands() -> None:
    output = StringIO()
    with redirect_stdout(output):
        assert entry.main(["--help"]) == 0
    help_text = output.getvalue()
    assert "verify-installed-release" in help_text
    assert "uninstall-release" in help_text
    assert "install-release" in help_text


def test_packaged_data_forwards_exact_commands_and_anchors() -> None:
    args = [
        "--destination", "C:/OwnerData/Installed Autosport",
        "--source-sha", "a" * 40,
        "--package-sha256", "b" * 64,
    ]
    with patch("autosport.cli.main", return_value=0) as canonical:
        assert entry.main(["verify-installed-release", *args]) == 0
        canonical.assert_called_once_with(["verify-installed-release", *args])
    with patch("autosport.cli.main", return_value=0) as canonical:
        assert entry.main(["uninstall-release", *args]) == 0
        canonical.assert_called_once_with(["uninstall-release", *args])


def test_packaged_data_does_not_turn_lifecycle_errors_into_success() -> None:
    with patch("autosport.cli.main", return_value=3) as canonical:
        assert entry.main(["uninstall-release", "--bad-arguments"]) == 3
        canonical.assert_called_once_with(["uninstall-release", "--bad-arguments"])


def test_windows_builder_probes_lifecycle_commands_before_and_after_unpack() -> None:
    source = (Path(__file__).parents[1] / "scripts" / "build_windows.ps1").read_text(
        encoding="utf-8"
    )
    for command in ("verify-installed-release", "uninstall-release"):
        assert f"& $dataExe {command} --help" in source
        assert f"& $extractedDataExe {command} --help" in source
        assert f"Packaged Autosport-Data.exe {command} --help exited" in source
        assert f"Fresh-extracted Autosport-Data.exe {command} --help exited" in source
