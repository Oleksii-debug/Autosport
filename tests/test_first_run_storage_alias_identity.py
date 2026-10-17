from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from autosport import windows_entry
from autosport.first_run_storage_audit import run_first_run_storage_audit
from autosport.paths import validate_product_storage_roots


def _directory_symlink_or_skip(link: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlink unavailable in this test environment: {exc}")
    if not link.is_symlink():
        pytest.skip("directory symlink was not created")


def _assert_audit_does_not_silently_authorize_workspace_alias(
    *,
    output: Path,
    physical_workspace: Path,
) -> None:
    result = run_first_run_storage_audit(output)
    payload = json.loads(output.read_text(encoding="utf-8"))

    if result == 0:
        assert payload["status"] == "PASS"
        assert Path(payload["workspace"]) == physical_workspace.resolve(strict=False)
    else:
        assert payload["status"] == "FAIL"


def test_first_run_audit_cannot_pass_with_final_workspace_symlink_as_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    physical_workspace = tmp_path / "physical" / "workspace"
    workspace_alias = tmp_path / "workspace-alias"
    _directory_symlink_or_skip(workspace_alias, physical_workspace)

    local_app_data = tmp_path / "profile" / "Local App Data"
    monkeypatch.setenv("AUTOSPORT_WORKSPACE", str(workspace_alias))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    _assert_audit_does_not_silently_authorize_workspace_alias(
        output=tmp_path / "final-component-audit.json",
        physical_workspace=physical_workspace,
    )


def test_first_run_audit_cannot_pass_with_symlinked_workspace_parent_as_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    physical_parent = tmp_path / "physical-parent"
    alias_parent = tmp_path / "alias-parent"
    _directory_symlink_or_skip(alias_parent, physical_parent)

    configured_workspace = alias_parent / "workspace"
    physical_workspace = physical_parent / "workspace"
    local_app_data = tmp_path / "profile" / "Local App Data"
    monkeypatch.setenv("AUTOSPORT_WORKSPACE", str(configured_workspace))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    _assert_audit_does_not_silently_authorize_workspace_alias(
        output=tmp_path / "parent-alias-audit.json",
        physical_workspace=physical_workspace,
    )


def test_first_run_audit_cannot_pass_with_aliased_local_app_data_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    physical_local_app_data = tmp_path / "physical-profile" / "Local App Data"
    alias_local_app_data = tmp_path / "local-app-data-alias"
    _directory_symlink_or_skip(alias_local_app_data, physical_local_app_data)

    monkeypatch.delenv("AUTOSPORT_WORKSPACE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(alias_local_app_data))

    output = tmp_path / "local-app-data-alias-audit.json"
    result = run_first_run_storage_audit(output)
    payload = json.loads(output.read_text(encoding="utf-8"))

    if result == 0:
        assert payload["status"] == "PASS"
        assert Path(payload["workspace"]) == (
            physical_local_app_data / "Autosport" / "workspace"
        ).resolve(strict=False)
        assert Path(payload["webview_storage"]) == (
            physical_local_app_data / "Autosport" / "webview2"
        ).resolve(strict=False)
    else:
        assert payload["status"] == "FAIL"


def test_interactive_startup_cannot_use_workspace_alias_as_durable_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    physical_workspace = tmp_path / "physical" / "workspace"
    workspace_alias = tmp_path / "workspace-alias"
    _directory_symlink_or_skip(workspace_alias, physical_workspace)

    local_app_data = tmp_path / "profile" / "Local App Data"
    monkeypatch.setenv("AUTOSPORT_WORKSPACE", str(workspace_alias))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))

    captured: dict[str, Path] = {}

    def probe(workspace: Path) -> None:
        captured["probe"] = workspace

    release_environment = types.ModuleType("autosport.webview2_release_environment")
    release_environment.active_webview2_environment_overrides = lambda: ()

    runtime_deployment = types.ModuleType("autosport.webview2_runtime_deployment")
    runtime_deployment.ensure_webview2_runtime = lambda: types.SimpleNamespace(
        available=True
    )

    emergency_stop = types.ModuleType("autosport.windows_webview_emergency_stop")

    class FakeController:
        def __init__(self, workspace: Path) -> None:
            captured["controller"] = workspace

    emergency_stop.EmergencyStopWebController = FakeController

    webview_shell = types.ModuleType("autosport.windows_webview_shell")

    class FakeWindowsWebViewUnavailable(RuntimeError):
        pass

    webview_shell.AutosportWebBridge = lambda controller: controller
    webview_shell.WindowsWebViewUnavailable = FakeWindowsWebViewUnavailable
    webview_shell.launch_windows_shell = (
        lambda _bridge, *, storage_path: 0
    )

    monkeypatch.setattr(windows_entry, "_probe_workspace_writable", probe)
    monkeypatch.setitem(
        sys.modules,
        "autosport.webview2_release_environment",
        release_environment,
    )
    monkeypatch.setitem(
        sys.modules,
        "autosport.webview2_runtime_deployment",
        runtime_deployment,
    )
    monkeypatch.setitem(
        sys.modules,
        "autosport.windows_webview_emergency_stop",
        emergency_stop,
    )
    monkeypatch.setitem(
        sys.modules,
        "autosport.windows_webview_shell",
        webview_shell,
    )

    result = windows_entry._run_interactive_gui()

    if result == 0:
        expected = physical_workspace.resolve(strict=False)
        assert captured["probe"] == expected
        assert captured["controller"] == expected
    else:
        assert result == 2


def test_storage_root_isolation_uses_physical_identity_for_alias_collisions(
    tmp_path: Path,
) -> None:
    physical_root = tmp_path / "physical-root"
    alias_root = tmp_path / "alias-root"
    _directory_symlink_or_skip(alias_root, physical_root)

    workspace = alias_root / "workspace"
    webview_storage = physical_root / "workspace" / "webview2"

    with pytest.raises(ValueError, match="must be disjoint trees"):
        validate_product_storage_roots(workspace, webview_storage)
