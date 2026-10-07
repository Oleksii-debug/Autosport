from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

import autosport.workspace_lock as workspace_lock
from autosport.workspace_lock import WorkspaceEconomicLock, WorkspaceEconomicLockError


def test_verification_descriptor_never_resolves_final_symlink(tmp_path: Path) -> None:
    """No-follow verification may reject an alias or open the alias object, never its target."""

    target = tmp_path / "target-lock.bin"
    alias = tmp_path / "alias-lock.bin"
    target.write_bytes(b"\0")
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlink creation unavailable: {exc}")

    target_descriptor = os.open(
        target,
        os.O_RDONLY | getattr(os, "O_BINARY", 0),
    )
    verification_descriptor: int | None = None
    try:
        try:
            verification_descriptor = workspace_lock._open_read_only_descriptor(alias)
        except OSError:
            # POSIX O_NOFOLLOW normally rejects the final symlink outright. Windows
            # may likewise reject conversion of an OPEN_REPARSE_POINT handle.
            return
        try:
            resolves_target = os.path.sameopenfile(
                target_descriptor,
                verification_descriptor,
            )
        except OSError:
            # A platform that cannot produce identity for the reparse-object handle
            # is also fail-closed at the production sameopenfile boundary.
            return
        assert resolves_target is False
    finally:
        if verification_descriptor is not None:
            os.close(verification_descriptor)
        os.close(target_descriptor)


def test_final_no_follow_rejection_fails_lock_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An alias appearing at the final current-path open cannot be accepted as the primary."""

    lock_path = tmp_path / WorkspaceEconomicLock.FILE_NAME
    lock_path.write_bytes(b"\0")
    real_verification_open = workspace_lock._open_read_only_descriptor
    state = {"verification_open_calls": 0}

    def reject_alias_at_final_post_lock_open(path: Path) -> int:
        state["verification_open_calls"] += 1
        if state["verification_open_calls"] == 4:
            raise OSError(errno.ELOOP, "simulated final-component alias")
        return real_verification_open(path)

    monkeypatch.setattr(
        workspace_lock,
        "_open_read_only_descriptor",
        reject_alias_at_final_post_lock_open,
    )

    lock = WorkspaceEconomicLock(tmp_path)
    with pytest.raises(
        WorkspaceEconomicLockError,
        match="changed during acquisition",
    ):
        lock.acquire()

    assert state["verification_open_calls"] == 4
    assert lock._handle is None
    assert lock_path.read_bytes() == b"\0"


def test_same_lock_file_metadata_change_is_not_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent sentinel write to the same inode must not look like pathname replacement."""

    lock = WorkspaceEconomicLock(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    handle = lock._open_lock_handle()
    real_verification_open = workspace_lock._open_read_only_descriptor
    verification_calls = 0

    def open_then_mutate_same_file(path: Path) -> int:
        nonlocal verification_calls
        descriptor = real_verification_open(path)
        verification_calls += 1
        if verification_calls == 1:
            with path.open("ab") as writer:
                writer.write(b"\0")
                writer.flush()
                os.fsync(writer.fileno())
        return descriptor

    monkeypatch.setattr(
        workspace_lock,
        "_open_read_only_descriptor",
        open_then_mutate_same_file,
    )
    try:
        lock._validate_open_handle_identity(handle)
    finally:
        handle.close()

    assert verification_calls == 2
    assert lock.path.read_bytes() == b"\0"
