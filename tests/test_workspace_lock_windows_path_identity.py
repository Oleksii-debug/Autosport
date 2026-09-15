from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.workspace_lock as workspace_lock
from autosport.workspace_lock import WorkspaceEconomicLock, WorkspaceEconomicLockError


def _stat_without_path_identity(result: os.stat_result) -> SimpleNamespace:
    return SimpleNamespace(
        st_mode=result.st_mode,
        st_nlink=result.st_nlink,
        st_size=result.st_size,
        st_mtime_ns=result.st_mtime_ns,
        st_ctime_ns=result.st_ctime_ns,
        st_ino=0,
        st_dev=0,
    )


def _stat_with_changed_path_metadata(result: os.stat_result) -> SimpleNamespace:
    return SimpleNamespace(
        st_mode=result.st_mode,
        st_nlink=result.st_nlink,
        st_size=result.st_size + 1,
        st_mtime_ns=result.st_mtime_ns,
        st_ctime_ns=result.st_ctime_ns,
        st_ino=getattr(result, "st_ino", 0),
        st_dev=getattr(result, "st_dev", 0),
    )


def test_lock_acquire_and_reacquire_do_not_require_path_stat_identity_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / WorkspaceEconomicLock.FILE_NAME
    real_stat = os.stat

    def stat_with_incomplete_path_identity(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if (
            not isinstance(path, int)
            and Path(path) == lock_path
            and kwargs.get("follow_symlinks", True) is False
        ):
            return _stat_without_path_identity(result)
        return result

    monkeypatch.setattr(workspace_lock.os, "stat", stat_with_incomplete_path_identity)

    with WorkspaceEconomicLock(tmp_path):
        pass
    with WorkspaceEconomicLock(tmp_path):
        pass

    assert lock_path.read_bytes() == b"\0"


def test_lock_rejects_redirected_verification_descriptor_to_different_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / WorkspaceEconomicLock.FILE_NAME
    replacement = tmp_path / "replacement-lock.bin"
    lock_path.write_bytes(b"\0")
    replacement.write_bytes(b"\1")

    real_verification_open = workspace_lock._open_read_only_descriptor

    def redirected_verification_open(path: Path) -> int:
        if Path(path) == lock_path:
            return os.open(path=replacement, flags=os.O_RDONLY | getattr(os, "O_BINARY", 0))
        return real_verification_open(path)

    monkeypatch.setattr(
        workspace_lock,
        "_open_read_only_descriptor",
        redirected_verification_open,
    )

    lock = WorkspaceEconomicLock(tmp_path)
    with pytest.raises(
        WorkspaceEconomicLockError,
        match="changed during acquisition",
    ):
        lock.acquire()

    assert lock._handle is None
    assert lock_path.read_bytes() == b"\0"
    assert replacement.read_bytes() == b"\1"


def test_lock_rechecks_path_after_post_lock_descriptor_identity_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pathname replacement during the final identity proof must fail closed."""

    lock_path = tmp_path / WorkspaceEconomicLock.FILE_NAME
    lock_path.write_bytes(b"\0")
    real_stat = os.stat
    real_sameopenfile = os.path.sameopenfile
    state = {"sameopenfile_calls": 0, "replacement_visible": False}

    def stat_with_post_identity_replacement(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if (
            state["replacement_visible"]
            and not isinstance(path, int)
            and Path(path) == lock_path
            and kwargs.get("follow_symlinks", True) is False
        ):
            return _stat_with_changed_path_metadata(result)
        return result

    def sameopenfile_then_replace_path(first: int, second: int) -> bool:
        result = real_sameopenfile(first, second)
        state["sameopenfile_calls"] += 1
        if state["sameopenfile_calls"] == 2:
            # The second call is the post-OS-lock checkpoint. Emulate a pathname
            # replacement immediately after the two already-open descriptors have
            # been proven identical, without relying on timing or Windows rename rules.
            state["replacement_visible"] = True
        return result

    monkeypatch.setattr(workspace_lock.os, "stat", stat_with_post_identity_replacement)
    monkeypatch.setattr(workspace_lock.os.path, "sameopenfile", sameopenfile_then_replace_path)

    lock = WorkspaceEconomicLock(tmp_path)
    with pytest.raises(
        WorkspaceEconomicLockError,
        match="changed during acquisition",
    ):
        lock.acquire()

    assert state["sameopenfile_calls"] == 2
    assert lock._handle is None
    assert lock_path.read_bytes() == b"\0"
