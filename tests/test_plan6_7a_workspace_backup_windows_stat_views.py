"""Plan 6/7A: preserve fail-closed workspace backup across Windows stat API clocks.

An executed Windows candidate had a healthy backup rejected as
"workspace entry changed during backup". Different lstat/fstat ctime clocks
must not masquerade as a mutation, but a change within either view must.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from autosport import workspace_backup


class _StatView:
    def __init__(self, underlying: os.stat_result, **changes: int) -> None:
        self._underlying = underlying
        self._changes = changes

    def __getattr__(self, name: str):
        if name in self._changes:
            return self._changes[name]
        return getattr(self._underlying, name)


def _write_owner_file(tmp_path: Path) -> Path:
    target = tmp_path / "owner-paper.json"
    target.write_bytes(b'{"real_money_execution":false}')
    return target


def test_unchanged_file_accepts_different_path_and_descriptor_ctime_views(
    tmp_path: Path,
) -> None:
    target = _write_owner_file(tmp_path)
    real_fstat = os.fstat

    def windows_like_fstat(fd: int) -> _StatView:
        original = real_fstat(fd)
        # Windows file metadata APIs can represent st_ctime_ns differently.
        # Both observations from the same descriptor remain identical.
        return _StatView(original, st_ctime_ns=original.st_ctime_ns + 10_000_000_000)

    with patch.object(workspace_backup.os, "fstat", side_effect=windows_like_fstat):
        assert workspace_backup._read_regular_stable(target) == target.read_bytes()


def test_descriptor_view_mutation_remains_rejected(tmp_path: Path) -> None:
    target = _write_owner_file(tmp_path)
    real_fstat = os.fstat
    observations = 0

    def mutated_descriptor_fstat(fd: int) -> _StatView:
        nonlocal observations
        observations += 1
        original = real_fstat(fd)
        changed = {"st_ctime_ns": original.st_ctime_ns + 10_000_000_000}
        if observations == 2:
            changed["st_mtime_ns"] = original.st_mtime_ns + 1
        return _StatView(original, **changed)

    with patch.object(workspace_backup.os, "fstat", side_effect=mutated_descriptor_fstat):
        with pytest.raises(workspace_backup.WorkspaceBackupError, match="changed during backup"):
            workspace_backup._read_regular_stable(target)
    assert observations == 2


def test_path_view_mutation_remains_rejected(tmp_path: Path) -> None:
    target = _write_owner_file(tmp_path)
    original_regular = workspace_backup._regular
    observations = 0

    def mutated_path_regular(path: Path, *, label: str):
        nonlocal observations
        observations += 1
        original = original_regular(path, label=label)
        if observations == 2:
            return _StatView(original, st_mtime_ns=original.st_mtime_ns + 1)
        return original

    with patch.object(workspace_backup, "_regular", side_effect=mutated_path_regular):
        with pytest.raises(workspace_backup.WorkspaceBackupError, match="changed during backup"):
            workspace_backup._read_regular_stable(target)
    assert observations == 2


def test_cross_view_inode_mismatch_remains_rejected(tmp_path: Path) -> None:
    target = _write_owner_file(tmp_path)
    real_fstat = os.fstat

    def switched_descriptor(fd: int) -> _StatView:
        original = real_fstat(fd)
        return _StatView(original, st_ino=original.st_ino + 1)

    with patch.object(workspace_backup.os, "fstat", side_effect=switched_descriptor):
        with pytest.raises(workspace_backup.WorkspaceBackupError, match="identity changed"):
            workspace_backup._read_regular_stable(target)
