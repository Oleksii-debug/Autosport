from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def _assert_audit_does_not_silently_authorize_alias(
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

    _assert_audit_does_not_silently_authorize_alias(
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

    _assert_audit_does_not_silently_authorize_alias(
        output=tmp_path / "parent-alias-audit.json",
        physical_workspace=physical_workspace,
    )


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
