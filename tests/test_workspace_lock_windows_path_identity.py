from __future__ import annotations

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.workspace_lock as workspace_lock
from autosport.workspace_lock import (
    WorkspaceEconomicLock,
    WorkspaceEconomicLockBusyError,
    WorkspaceEconomicLockError,
)


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


def _patch_lock_backend_error(
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
) -> None:
    if os.name == "nt":
        import msvcrt

        def fail_locking(_descriptor: int, _mode: int, _count: int) -> None:
            raise error

        monkeypatch.setattr(msvcrt, "locking", fail_locking)
        return

    import fcntl

    def fail_flock(_descriptor: int, _operation: int) -> None:
        raise error

    monkeypatch.setattr(fcntl, "flock", fail_flock)


def test_lock_backend_contention_has_distinct_busy_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / WorkspaceEconomicLock.FILE_NAME
    lock_path.write_bytes(b"\0")
    _patch_lock_backend_error(
        monkeypatch,
        BlockingIOError(errno.EAGAIN, "simulated active writer"),
    )

    with lock_path.open("r+b") as handle:
        with pytest.raises(
            WorkspaceEconomicLockBusyError,
            match="another Autosport process owns",
        ) as caught:
            WorkspaceEconomicLock._lock_handle(handle)

    assert isinstance(caught.value, WorkspaceEconomicLockError)


def test_lock_backend_integrity_failure_is_not_busy_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / WorkspaceEconomicLock.FILE_NAME
    lock_path.write_bytes(b"\0")
    _patch_lock_backend_error(
        monkeypatch,
        OSError(errno.EBADF, "simulated invalid lock handle"),
    )

    with lock_path.open("r+b") as handle:
        with pytest.raises(
            WorkspaceEconomicLockError,
            match="cannot acquire workspace economic-writer lock",
        ) as caught:
            WorkspaceEconomicLock._lock_handle(handle)

    assert not isinstance(caught.value, WorkspaceEconomicLockBusyError)


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
        if state["sameopenfile_calls"] == 3:
            # Two identity proofs complete the pre-lock checkpoint. The third call is
            # the first proof in the post-OS-lock checkpoint; expose replacement after
            # that proof so the following pathname observation must detect the change.
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

    assert state["sameopenfile_calls"] == 4
    assert lock._handle is None
    assert lock_path.read_bytes() == b"\0"


def test_lock_rejects_same_metadata_replacement_at_final_handle_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metadata equality cannot substitute for a final current-path handle proof."""

    lock_path = tmp_path / WorkspaceEconomicLock.FILE_NAME
    replacement = tmp_path / "same-metadata-replacement-lock.bin"
    lock_path.write_bytes(b"\0")
    replacement.write_bytes(b"\0")

    real_stat = os.stat
    real_sameopenfile = os.path.sameopenfile
    real_verification_open = workspace_lock._open_read_only_descriptor
    original_stat = real_stat(lock_path, follow_symlinks=False)
    state = {
        "sameopenfile_calls": 0,
        "verification_open_calls": 0,
        "replacement_visible": False,
    }

    def stat_with_same_metadata_replacement(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if (
            state["replacement_visible"]
            and not isinstance(path, int)
            and Path(path) == lock_path
            and kwargs.get("follow_symlinks", True) is False
        ):
            # Emulate replacement file B presenting exactly the metadata fields used
            # by _stable_stat_metadata(), so a metadata-only boundary would accept it.
            return SimpleNamespace(
                st_mode=original_stat.st_mode,
                st_nlink=1,
                st_size=original_stat.st_size,
                st_mtime_ns=original_stat.st_mtime_ns,
                st_ctime_ns=original_stat.st_ctime_ns,
                st_ino=0,
                st_dev=0,
            )
        return result

    def sameopenfile_then_make_replacement_visible(first: int, second: int) -> bool:
        result = real_sameopenfile(first, second)
        state["sameopenfile_calls"] += 1
        if state["sameopenfile_calls"] == 3:
            # The third identity proof is the first descriptor proof in the post-lock
            # checkpoint after the pre-lock checkpoint has completed both proofs.
            state["replacement_visible"] = True
        return result

    def redirect_only_final_current_path_open(path: Path) -> int:
        if Path(path) == lock_path:
            state["verification_open_calls"] += 1
            if state["verification_open_calls"] == 4:
                # The fourth read-only open is the final fresh pathname descriptor in
                # the post-lock checkpoint. Bind it to distinct file B deterministically.
                return os.open(
                    path=replacement,
                    flags=os.O_RDONLY | getattr(os, "O_BINARY", 0),
                )
        return real_verification_open(path)

    monkeypatch.setattr(workspace_lock.os, "stat", stat_with_same_metadata_replacement)
    monkeypatch.setattr(workspace_lock.os.path, "sameopenfile", sameopenfile_then_make_replacement_visible)
    monkeypatch.setattr(
        workspace_lock,
        "_open_read_only_descriptor",
        redirect_only_final_current_path_open,
    )

    lock = WorkspaceEconomicLock(tmp_path)
    with pytest.raises(
        WorkspaceEconomicLockError,
        match="changed during acquisition",
    ):
        lock.acquire()

    assert state["verification_open_calls"] == 4
    assert state["sameopenfile_calls"] == 4
    assert lock._handle is None
    assert lock_path.read_bytes() == b"\0"
    assert replacement.read_bytes() == b"\0"
