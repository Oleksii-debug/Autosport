from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

import autosport.evidence_export as evidence_export


def _create_directory_alias(alias: Path, target: Path) -> None:
    try:
        os.symlink(target, alias, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable in this environment")


def _retarget_directory_alias(alias: Path, target: Path) -> None:
    alias.unlink()
    os.symlink(target, alias, target_is_directory=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows V1 workspace alias snapshot boundary")
def test_windows_export_keeps_resolved_workspace_after_alias_retarget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_a = tmp_path / "workspace-a"
    target_b = tmp_path / "workspace-b"
    target_a.mkdir()
    target_b.mkdir()
    bytes_a = b"authoritative-paper-a"
    bytes_b = b"replacement-paper-b"
    (target_a / "paper_book.json").write_bytes(bytes_a)
    (target_b / "paper_book.json").write_bytes(bytes_b)

    alias = tmp_path / "workspace-alias"
    _create_directory_alias(alias, target_a)
    output = tmp_path / "manifest.json"

    real_names = evidence_export._canonical_source_names
    name_checks = 0
    retargeted = False

    def retarget_after_watcher_arm(workspace: Path) -> tuple[str, ...]:
        nonlocal name_checks, retargeted
        name_checks += 1
        # Export performs one pre-lock eligibility check. The second discovery is
        # inside WorkspaceEconomicLock after the Windows watcher is armed.
        if name_checks == 2:
            _retarget_directory_alias(alias, target_b)
            retargeted = True
        return real_names(workspace)

    monkeypatch.setattr(
        evidence_export,
        "_canonical_source_names",
        retarget_after_watcher_arm,
    )

    report = evidence_export.export_evidence_manifest(alias, output)

    assert retargeted is True
    assert alias.resolve(strict=True) == target_b.resolve(strict=True)
    assert report["files"] == [
        {
            "path": "paper_book.json",
            "size_bytes": len(bytes_a),
            "sha256": hashlib.sha256(bytes_a).hexdigest(),
        }
    ]
    assert output.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows V1 workspace alias snapshot boundary")
def test_windows_verify_does_not_follow_alias_retarget_to_matching_decoy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_a = tmp_path / "workspace-a"
    target_b = tmp_path / "workspace-b"
    target_a.mkdir()
    target_b.mkdir()
    (target_a / "paper_book.json").write_bytes(b"authoritative-paper-a")
    (target_b / "paper_book.json").write_bytes(b"decoy-paper-b")

    manifest = tmp_path / "manifest.json"
    evidence_export.export_evidence_manifest(target_b, manifest)

    alias = tmp_path / "workspace-alias"
    _create_directory_alias(alias, target_a)

    real_names = evidence_export._canonical_source_names
    retargeted = False

    def retarget_after_watcher_arm(workspace: Path) -> tuple[str, ...]:
        nonlocal retargeted
        if not retargeted:
            _retarget_directory_alias(alias, target_b)
            retargeted = True
        return real_names(workspace)

    monkeypatch.setattr(
        evidence_export,
        "_canonical_source_names",
        retarget_after_watcher_arm,
    )

    with pytest.raises(
        ValueError,
        match="workspace evidence does not match manifest: paper_book.json",
    ):
        evidence_export.verify_evidence_manifest(manifest, alias)

    assert retargeted is True
    assert alias.resolve(strict=True) == target_b.resolve(strict=True)
