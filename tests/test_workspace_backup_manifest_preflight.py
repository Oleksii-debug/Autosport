"""Plan 6 6В: deny oversized ZIP manifests before decompression or JSON parsing."""

from __future__ import annotations

import zipfile

import pytest

from autosport.workspace_backup import WorkspaceBackupError, restore_workspace


def test_oversized_manifest_is_rejected_without_zip_read(tmp_path, monkeypatch) -> None:
    archive_path = tmp_path / "oversized-manifest.zip"
    manifest_name = "AUTOSPORT_BACKUP_MANIFEST.json"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            manifest_name,
            b"x" * (512 * 1024 + 1),
            compress_type=zipfile.ZIP_DEFLATED,
        )

    original_read = zipfile.ZipFile.read

    def reject_manifest_read(self, member, *args, **kwargs):
        if member == manifest_name:
            raise AssertionError("oversized untrusted manifest was decompressed")
        return original_read(self, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", reject_manifest_read)
    destination = tmp_path / "never-created"
    with pytest.raises(WorkspaceBackupError, match="oversized backup manifest"):
        restore_workspace(archive_path, destination)
    assert not destination.exists()
