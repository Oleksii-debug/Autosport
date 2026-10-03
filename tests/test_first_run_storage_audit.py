from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from autosport import windows_entry
from autosport.first_run_storage_audit import run_first_run_storage_audit


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_audit_projects_cwd_independent_unicode_storage_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_app_data = tmp_path / "Користувач Тест" / "AppData Local"
    cwd_a = tmp_path / "launch a"
    cwd_b = tmp_path / "launch б"
    cwd_a.mkdir()
    cwd_b.mkdir()
    output_a = tmp_path / "audit-a.json"
    output_b = tmp_path / "audit-b.json"

    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("AUTOSPORT_WORKSPACE", raising=False)

    monkeypatch.chdir(cwd_a)
    assert run_first_run_storage_audit(output_a) == 0
    monkeypatch.chdir(cwd_b)
    assert run_first_run_storage_audit(output_b) == 0

    first = _read(output_a)
    second = _read(output_b)
    assert first["status"] == "PASS"
    assert second["status"] == "PASS"
    assert first["workspace"] == second["workspace"] == str(
        local_app_data / "Autosport" / "workspace"
    )
    assert first["webview_storage"] == second["webview_storage"] == str(
        local_app_data / "Autosport" / "webview2"
    )
    assert first["launch_cwd"] == str(cwd_a)
    assert second["launch_cwd"] == str(cwd_b)
    assert first["real_money_execution"] is False
    assert first["human_tested"] is False
    assert first["nvda_verified"] is False


def test_audit_uses_windows_known_folder_when_localappdata_is_absent(
    tmp_path: Path,
) -> None:
    known_folder = tmp_path / "Known Local"
    output = tmp_path / "audit.json"

    with (
        patch.dict(os.environ, {}, clear=True),
        patch("autosport.paths.sys.platform", "win32"),
        patch(
            "autosport.paths._windows_known_folder_local_app_data",
            return_value=known_folder,
        ) as resolve_known_folder,
    ):
        assert run_first_run_storage_audit(output) == 0

    evidence = _read(output)
    assert evidence["workspace"] == str(known_folder / "Autosport" / "workspace")
    assert evidence["webview_storage"] == str(
        known_folder / "Autosport" / "webview2"
    )
    assert resolve_known_folder.call_count == 2


def test_audit_rejects_workspace_nested_inside_webview_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_app_data = tmp_path / "Local"
    output = tmp_path / "audit.json"
    webview_storage = local_app_data / "Autosport" / "webview2"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv(
        "AUTOSPORT_WORKSPACE",
        str(webview_storage / "economic-state"),
    )

    assert run_first_run_storage_audit(output) == 1

    evidence = _read(output)
    assert evidence["status"] == "FAIL"
    assert evidence["error_type"] == "ValueError"
    assert evidence["real_money_execution"] is False
    assert evidence["human_tested"] is False
    assert evidence["nvda_verified"] is False


def test_audit_failure_is_bounded_and_does_not_echo_invalid_path(
    tmp_path: Path,
) -> None:
    output = tmp_path / "audit.json"
    secretish_invalid_value = "relative-secretish-storage-root"

    with patch.dict(
        os.environ,
        {"LOCALAPPDATA": secretish_invalid_value},
        clear=True,
    ):
        assert run_first_run_storage_audit(output) == 1

    raw = output.read_text(encoding="utf-8")
    evidence = json.loads(raw)
    assert evidence["status"] == "FAIL"
    assert evidence["error"] == "first-run storage path resolution failed"
    assert evidence["error_type"] == "ValueError"
    assert secretish_invalid_value not in raw
    assert evidence["real_money_execution"] is False
    assert evidence["human_tested"] is False
    assert evidence["nvda_verified"] is False


def test_windows_entry_dispatches_first_run_storage_audit_without_gui() -> None:
    run_audit = MagicMock(return_value=0)
    fake_audit_module = types.ModuleType("autosport.first_run_storage_audit")
    fake_audit_module.run_first_run_storage_audit = run_audit

    with (
        patch.dict(
            sys.modules,
            {"autosport.first_run_storage_audit": fake_audit_module},
        ),
        patch.object(
            windows_entry,
            "_run_interactive_gui",
            side_effect=AssertionError("machine audit must not open interactive GUI"),
        ),
    ):
        exit_code = windows_entry.main(
            ["--first-run-storage-audit-output", "evidence.json"]
        )

    assert exit_code == 0
    run_audit.assert_called_once_with("evidence.json")
