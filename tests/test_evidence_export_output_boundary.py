from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import autosport.evidence_export as evidence_export
from autosport.evidence_export import export_evidence_manifest


def _workspace_with_evidence(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "paper_book.json").write_bytes(b'{"balance":"100"}\n')
    return workspace


@pytest.mark.parametrize(
    ("relative_path", "original"),
    [
        (Path("market.db"), b"sqlite-product-state"),
        (Path(".economic-run.lock"), b"existing-lock-metadata"),
        (Path("source_health.json.lock"), b"source-health-lock-metadata"),
        (Path("token.json"), b'{"token":"must-survive"}\n'),
        (Path("arbitrary-product-state.bin"), b"arbitrary-product-state"),
        (Path(".run-transactions") / "run-1" / "manifest.json", b"transaction-evidence"),
    ],
)
def test_export_rejects_existing_workspace_destination_without_modifying_bytes(
    tmp_path: Path,
    relative_path: Path,
    original: bytes,
) -> None:
    workspace = _workspace_with_evidence(tmp_path)
    destination = workspace / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(original)

    with pytest.raises(ValueError, match="outside the Autosport workspace"):
        export_evidence_manifest(workspace, destination)

    assert destination.read_bytes() == original
    assert (workspace / "paper_book.json").read_bytes() == b'{"balance":"100"}\n'


def test_export_rejects_new_destination_anywhere_inside_workspace(tmp_path: Path) -> None:
    workspace = _workspace_with_evidence(tmp_path)
    destination = workspace / "exports" / "manifest.json"

    with pytest.raises(ValueError, match="outside the Autosport workspace"):
        export_evidence_manifest(workspace, destination)

    assert not destination.exists()
    assert not destination.parent.exists()


def test_export_rejects_external_path_that_resolves_back_into_workspace(tmp_path: Path) -> None:
    workspace = _workspace_with_evidence(tmp_path)
    alias = tmp_path / "workspace-alias"
    try:
        os.symlink(workspace, alias, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable in this environment")

    destination = alias / "market.db"
    with pytest.raises(ValueError, match="outside the Autosport workspace"):
        export_evidence_manifest(workspace, destination)

    assert not (workspace / "market.db").exists()


def test_export_uses_resolved_external_target_without_replacing_workspace_symlink(
    tmp_path: Path,
) -> None:
    workspace = _workspace_with_evidence(tmp_path)
    external_target = tmp_path / "external-manifest.json"
    external_target.write_text("old-external-bytes\n", encoding="utf-8")
    link = workspace / "export-link.json"
    try:
        os.symlink(external_target, link)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks unavailable in this environment")

    report = export_evidence_manifest(workspace, link)

    assert link.is_symlink()
    assert link.resolve() == external_target.resolve()
    assert json.loads(external_target.read_text(encoding="utf-8")) == report
    assert not (workspace / "export-link.json").is_file() or link.is_symlink()


def test_export_rechecks_destination_after_snapshot_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace_with_evidence(tmp_path)
    external_target = tmp_path / "external-manifest.json"
    external_target.write_text("old-external-bytes\n", encoding="utf-8")
    internal_target = workspace / "market.db"
    internal_target.write_bytes(b"sqlite-product-state")
    link = workspace / "destination-link.json"
    try:
        os.symlink(external_target, link)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks unavailable in this environment")

    real_hash = evidence_export._open_and_hash_regular_file
    redirected = False

    def hash_then_redirect(path: Path) -> tuple[int, str]:
        nonlocal redirected
        result = real_hash(path)
        if not redirected:
            link.unlink()
            os.symlink(internal_target, link)
            redirected = True
        return result

    monkeypatch.setattr(evidence_export, "_open_and_hash_regular_file", hash_then_redirect)

    with pytest.raises(ValueError, match="outside the Autosport workspace"):
        export_evidence_manifest(workspace, link)

    assert redirected is True
    assert external_target.read_text(encoding="utf-8") == "old-external-bytes\n"
    assert internal_target.read_bytes() == b"sqlite-product-state"


def test_export_rejects_reparentable_sibling_output_parent(tmp_path: Path) -> None:
    workspace = _workspace_with_evidence(tmp_path)
    destination = tmp_path / "exports" / "manifest.json"

    with pytest.raises(ValueError, match="ancestor of the Autosport workspace"):
        export_evidence_manifest(workspace, destination)

    assert not destination.exists()
    assert not destination.parent.exists()


def test_export_accepts_output_in_workspace_ancestor(tmp_path: Path) -> None:
    safe_parent = tmp_path / "safe-parent"
    safe_parent.mkdir()
    workspace = _workspace_with_evidence(safe_parent)
    destination = safe_parent / "manifest.json"

    report = export_evidence_manifest(workspace, destination)

    assert json.loads(destination.read_text(encoding="utf-8")) == report
    assert not (workspace / "manifest.json").exists()


def test_bound_parent_cannot_cross_into_workspace_after_ancestry_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attack the exact review seam: check returns, then rename before mutation."""

    safe_parent = tmp_path / "safe-parent"
    safe_parent.mkdir()
    workspace = _workspace_with_evidence(safe_parent)
    destination = safe_parent / "manifest.json"
    captured_parent = workspace / "captured-parent"
    attack_attempted = False
    rename_blocked = False

    if os.name == "nt":
        seam_name = "_require_windows_output_parent_outside_workspace"
    else:
        seam_name = "_require_posix_output_parent_outside_workspace"
    real_require = getattr(evidence_export, seam_name)

    def require_then_attack(parent_handle: int, workspace_handle: int) -> None:
        nonlocal attack_attempted, rename_blocked
        real_require(parent_handle, workspace_handle)
        if attack_attempted:
            return
        attack_attempted = True
        try:
            safe_parent.rename(captured_parent)
        except OSError:
            rename_blocked = True
        else:
            pytest.fail("safe publication parent was reparented into its workspace descendant")

    monkeypatch.setattr(evidence_export, seam_name, require_then_attack)

    report = export_evidence_manifest(workspace, destination)

    assert attack_attempted is True
    assert rename_blocked is True
    assert json.loads(destination.read_text(encoding="utf-8")) == report
    assert not captured_parent.exists()
    assert not (workspace / "manifest.json").exists()


def test_export_fails_closed_if_safe_parent_moves_and_old_path_is_recreated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-publication PASS still binds the caller-visible parent/file identity."""

    safe_parent = tmp_path / "safe-parent"
    safe_parent.mkdir()
    workspace = _workspace_with_evidence(safe_parent)
    destination = safe_parent / "manifest.json"
    moved_parent = tmp_path / "safe-parent-moved"
    real_writer = evidence_export.atomic_write_json
    attack_attempted = False
    expected_payload: dict[str, object] | None = None

    def move_recreate_and_publish(path: Path, payload: dict[str, object]) -> None:
        nonlocal attack_attempted, expected_payload
        attack_attempted = True
        expected_payload = payload
        try:
            safe_parent.rename(moved_parent)
        except OSError:
            pytest.skip("platform prevents renaming the bound safe parent")

        safe_parent.mkdir()
        decoy_workspace = safe_parent / "workspace"
        decoy_workspace.mkdir()
        (decoy_workspace / "paper_book.json").write_bytes(b'{"balance":"100"}\n')
        destination.write_bytes(evidence_export._manifest_file_bytes(payload))
        real_writer(path, payload)

    monkeypatch.setattr(evidence_export, "atomic_write_json", move_recreate_and_publish)

    with pytest.raises(ValueError, match="caller-visible evidence output parent changed before PASS"):
        export_evidence_manifest(workspace, destination)

    assert attack_attempted is True
    assert expected_payload is not None
    assert json.loads(destination.read_text(encoding="utf-8")) == expected_payload
    assert json.loads((moved_parent / "manifest.json").read_text(encoding="utf-8")) == expected_payload
