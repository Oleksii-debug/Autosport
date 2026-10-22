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


def test_windows_reserved_device_name_never_enters_backup() -> None:
    from autosport.workspace_backup import _safe_relative

    with pytest.raises(WorkspaceBackupError, match="nonportable"):
        _safe_relative("CON.txt")
    with pytest.raises(WorkspaceBackupError, match="nonportable"):
        _safe_relative("session/LPT1.txt")


@pytest.mark.parametrize(
    "name",
    [
        "COM¹.txt",
        "nested/LPT².txt",
        "nested/COM³",
        "bad?.json",
        "bad|name.txt",
        "bad<name.txt",
        'bad"name.txt',
        "bad\x01name.txt",
        "autosport_backup_manifest.JSON",
        "nested/.ECONOMIC-RUN.LOCK",
        "nested/\ud800",
    ],
)
def test_reject_windows_nonportable_or_reserved_member_names(name: str) -> None:
    from autosport.workspace_backup import _safe_relative

    with pytest.raises(WorkspaceBackupError, match="nonportable|reserved"):
        _safe_relative(name)


def test_reject_windows_case_colliding_archive_entries(tmp_path: Path) -> None:
    archive = tmp_path / "case-colliding.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("Report.txt", b"A")
        z.writestr("report.TXT", b"B")
        z.writestr("AUTOSPORT_BACKUP_MANIFEST.json", b"{}")
    target = tmp_path / "restored"
    with pytest.raises(WorkspaceBackupError, match="case-colliding"):
        restore_workspace(archive, target)
    assert not target.exists()


def test_backup_rejects_windows_case_colliding_files(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "Report.txt").write_text("A", encoding="utf-8")
    second = root / "report.TXT"
    if second.exists():
        pytest.skip("filesystem cannot materialize case-colliding source files")
    second.write_text("B", encoding="utf-8")
    with pytest.raises(WorkspaceBackupError, match="case-colliding"):
        backup_workspace(root, tmp_path / "private.zip")
    assert not (tmp_path / "private.zip").exists()


def test_restore_publication_never_replaces_racing_empty_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An attacker/concurrent process may create the destination after preflight.

    POSIX os.rename(stage, existing_empty_dir) would silently replace it.
    The publication primitive must reject even an empty existing directory.
    """
    from autosport import workspace_backup as backup_module

    source = tmp_path / "source"
    source.mkdir()
    (source / "ledger.json").write_text('{"paper":"untouched"}', encoding="utf-8")
    backup = tmp_path / "private.zip"
    backup_workspace(source, backup)

    target = tmp_path / "destination"
    publish = backup_module._publish_new_workspace

    def concurrent_creation(stage: Path, destination: Path) -> None:
        destination.mkdir()
        publish(stage, destination)

    monkeypatch.setattr(backup_module, "_publish_new_workspace", concurrent_creation)
    with pytest.raises(WorkspaceBackupError, match="already exists"):
        restore_workspace(backup, target)
    assert target.is_dir()
    assert list(target.iterdir()) == []
    assert not list(tmp_path.glob(".autosport-restoring-*"))

def test_regular_backup_tolerates_stable_windows_path_handle_timestamp_precision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Path and handle timestamp precision may differ without any file change."""
    from types import SimpleNamespace
    from autosport import workspace_backup as implementation

    source = tmp_path / "stable-input"
    source.write_bytes(b"paper state")
    original_regular = implementation._regular

    def distinct_path_view(path: Path, *, label: str):
        info = original_regular(path, label=label)
        return SimpleNamespace(
            st_dev=info.st_dev, st_ino=info.st_ino,
            st_mode=info.st_mode, st_nlink=info.st_nlink,
            st_size=info.st_size, st_mtime_ns=info.st_mtime_ns + 1000,
            st_ctime_ns=info.st_ctime_ns + 1000,
        )

    monkeypatch.setattr(implementation, "_regular", distinct_path_view)
    assert implementation._read_regular_stable(source) == b"paper state"


def test_regular_backup_rejects_changed_path_witness_with_split_timestamp_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tolerance for timestamp precision must not permit source mutation."""
    from types import SimpleNamespace
    from autosport import workspace_backup as implementation

    source = tmp_path / "stable-input"
    source.write_bytes(b"paper state")
    original_regular = implementation._regular
    calls = 0

    def altered_path_view(path: Path, *, label: str):
        nonlocal calls
        calls += 1
        info = original_regular(path, label=label)
        return SimpleNamespace(
            st_dev=info.st_dev, st_ino=info.st_ino,
            st_mode=info.st_mode, st_nlink=info.st_nlink,
            st_size=info.st_size,
            st_mtime_ns=info.st_mtime_ns + 1000 + (1 if calls == 2 else 0),
            st_ctime_ns=info.st_ctime_ns + 1000,
        )

    monkeypatch.setattr(implementation, "_regular", altered_path_view)
    with pytest.raises(WorkspaceBackupError, match="changed during backup"):
        implementation._read_regular_stable(source)
