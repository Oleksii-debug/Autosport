"""Plan 6 §6В: an unreadable subtree must never become a successful partial backup."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from autosport.workspace_backup import WorkspaceBackupError, backup_workspace


def test_nested_scandir_failure_rejects_partial_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.walk without onerror silently skips a denied subtree."""
    root = tmp_path / "workspace"
    denied = root / "restricted"
    denied.mkdir(parents=True)
    (denied / "private-ledger.json").write_text('{"paper":"ledger"}', encoding="utf-8")
    (root / "public.txt").write_text("visible", encoding="utf-8")
    target = tmp_path / "backup.zip"
    real_scandir = os.scandir

    def refuse_nested(path):
        if Path(path) == denied:
            raise PermissionError("private path and account data must not enter reports")
        return real_scandir(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "scandir", refuse_nested)
        with pytest.raises(WorkspaceBackupError, match="workspace directory enumeration failed"):
            backup_workspace(root, target)

    assert not target.exists()
    assert not tuple(tmp_path.glob(".autosport-backup-*.tmp"))
