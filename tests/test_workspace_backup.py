from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from autosport.workspace_backup import WorkspaceBackupError, backup_workspace, restore_workspace


def test_complete_fresh_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "data").mkdir(parents=True)
    (root / "data" / "ledger.json").write_text('{"balance":"123"}', encoding="utf-8")
    (root / "state.txt").write_text("paper only", encoding="utf-8")
    archive = tmp_path / "private-backup.zip"
    evidence = backup_workspace(root, archive)
    assert evidence["file_count"] == 2
    assert len(evidence["archive_sha256"]) == 64
    assert "ledger.json" not in str(evidence)
    with zipfile.ZipFile(archive) as z:
        manifest = json.loads(z.read("AUTOSPORT_BACKUP_MANIFEST.json"))
        assert manifest["kind"] == "autosport.workspace.backup"
        assert not any("/tmp/" in x["path"] for x in manifest["files"])
    restored = tmp_path / "restored"
    result = restore_workspace(archive, restored)
    assert result["real_money_execution"] is False
    assert (restored / "data" / "ledger.json").read_text() == '{"balance":"123"}'
    assert (restored / "state.txt").read_text() == "paper only"
    with pytest.raises(WorkspaceBackupError, match="exists"):
        restore_workspace(archive, restored)
    with pytest.raises(WorkspaceBackupError, match="exists"):
        backup_workspace(root, archive)


def test_reject_nested_output_and_alias(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "ok").write_text("ok")
    with pytest.raises(WorkspaceBackupError, match="outside"):
        backup_workspace(root, root / "backup.zip")
    external = tmp_path / "private"
    external.write_text("secret")
    (root / "alias").symlink_to(external)
    with pytest.raises(WorkspaceBackupError, match="unaliased"):
        backup_workspace(root, tmp_path / "archive.zip")


def test_reject_corruption_before_creating_restore_target(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "valid").write_text("original")
    archive = tmp_path / "private.zip"
    backup_workspace(root, archive)
    with zipfile.ZipFile(archive, "a") as z:
        z.writestr("forged", b"evil")
    restored = tmp_path / "target"
    with pytest.raises(WorkspaceBackupError, match="incomplete|inventory"):
        restore_workspace(archive, restored)
    assert not restored.exists()


def test_reject_zip_slip_and_existing_target(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../escape", b"e")
        z.writestr("AUTOSPORT_BACKUP_MANIFEST.json", b"{}")
    with pytest.raises(WorkspaceBackupError, match="noncanonical"):
        restore_workspace(archive, tmp_path / "new")
    target = tmp_path / "existing"
    target.mkdir()
    (target / "do-not-touch").write_text("safe")
    with pytest.raises(WorkspaceBackupError, match="already exists"):
        restore_workspace(archive, target)
    assert (target / "do-not-touch").read_text() == "safe"


def test_busy_economic_lock_is_not_ignored(tmp_path: Path, monkeypatch) -> None:
    import autosport.workspace_backup as module
    root = tmp_path / "source"
    root.mkdir()
    (root / "valid").write_text("paper")

    class BusyLock:
        FILE_NAME = ".economic-run.lock"

        def __init__(self, _: Path) -> None:
            pass

        def __enter__(self):
            raise RuntimeError("economic writer busy")

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(module, "WorkspaceEconomicLock", BusyLock)
    with pytest.raises(RuntimeError, match="economic writer busy"):
        backup_workspace(root, tmp_path / "backup.zip")
    assert not (tmp_path / "backup.zip").exists()


def test_windows_reserved_device_name_never_enters_backup(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "CON.txt").write_text("unsafe portability")
    with pytest.raises(WorkspaceBackupError, match="nonportable"):
        backup_workspace(root, tmp_path / "backup.zip")
    assert not (tmp_path / "backup.zip").exists()
