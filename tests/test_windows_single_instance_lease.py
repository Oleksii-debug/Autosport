from __future__ import annotations

import os
from pathlib import Path
import threading

import pytest

import autosport.windows_single_instance_lease as lease_module
from autosport.windows_single_instance_lease import (
    WindowsLaunchLeaseError,
    WindowsLaunchLeaseHeldError,
    acquire_windows_launch_lease,
)


class _FakeExclusiveKernel:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held_paths: set[Path] = set()
        self._path_by_handle: dict[int, Path] = {}
        self._next_handle = 100
        self.close_calls: list[int] = []

    def create(self, path: Path) -> int:
        with self._lock:
            if path in self._held_paths:
                raise WindowsLaunchLeaseHeldError(
                    "Autosport is already running for this user scope"
                )
            handle = self._next_handle
            self._next_handle += 1
            self._held_paths.add(path)
            self._path_by_handle[handle] = path
            return handle

    def close(self, handle: int) -> None:
        with self._lock:
            self.close_calls.append(handle)
            path = self._path_by_handle.pop(handle)
            self._held_paths.remove(path)


def _install_fake_kernel(monkeypatch: pytest.MonkeyPatch) -> _FakeExclusiveKernel:
    backend = _FakeExclusiveKernel()
    monkeypatch.setattr(
        lease_module,
        "_create_exclusive_windows_handle",
        backend.create,
    )
    monkeypatch.setattr(
        lease_module,
        "_close_windows_handle",
        backend.close,
    )
    return backend


def test_two_concurrent_contenders_get_exactly_one_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_kernel(monkeypatch)
    start = threading.Barrier(3)
    outcomes_ready = threading.Barrier(3)
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    def contender() -> None:
        lease = None
        start.wait()
        try:
            lease = acquire_windows_launch_lease(
                lease_root=tmp_path,
                user_scope="user-A",
            )
            outcome = "ACQUIRED"
        except WindowsLaunchLeaseHeldError:
            outcome = "HELD"
        with outcomes_lock:
            outcomes.append(outcome)

        # The winner still owns the lease while the loser reports HELD.
        outcomes_ready.wait()
        if lease is not None:
            lease.release()

    threads = [threading.Thread(target=contender) for _ in range(2)]
    for thread in threads:
        thread.start()

    start.wait()
    outcomes_ready.wait()

    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["ACQUIRED", "HELD"]


def test_release_makes_same_user_scope_reacquirable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_kernel(monkeypatch)

    first = acquire_windows_launch_lease(
        lease_root=tmp_path,
        user_scope="user-A",
    )
    with pytest.raises(WindowsLaunchLeaseHeldError):
        acquire_windows_launch_lease(
            lease_root=tmp_path,
            user_scope="user-A",
        )

    first.release()
    second = acquire_windows_launch_lease(
        lease_root=tmp_path,
        user_scope="user-A",
    )
    second.release()


def test_distinct_user_scopes_use_distinct_opaque_lease_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_kernel(monkeypatch)

    first = acquire_windows_launch_lease(
        lease_root=tmp_path,
        user_scope="user-A",
    )
    second = acquire_windows_launch_lease(
        lease_root=tmp_path,
        user_scope="user-B",
    )
    try:
        assert first.path != second.path
        assert first.lease_key_sha256 != second.lease_key_sha256
        assert "user-A" not in first.path.name
        assert "user-B" not in second.path.name
        assert len(first.lease_key_sha256) == 64
    finally:
        first.release()
        second.release()


def test_release_closes_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _install_fake_kernel(monkeypatch)
    lease = acquire_windows_launch_lease(
        lease_root=tmp_path,
        user_scope="user-A",
    )

    lease.release()
    lease.release()

    assert lease.released is True
    assert len(backend.close_calls) == 1


def test_close_failure_is_fail_closed_and_never_double_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lease_module,
        "_create_exclusive_windows_handle",
        lambda path: 123,
    )
    attempts: list[int] = []

    def fail_close(handle: int) -> None:
        attempts.append(handle)
        raise WindowsLaunchLeaseError("synthetic CloseHandle failure")

    monkeypatch.setattr(
        lease_module,
        "_close_windows_handle",
        fail_close,
    )

    lease = acquire_windows_launch_lease(
        lease_root=tmp_path,
        user_scope="user-A",
    )
    with pytest.raises(WindowsLaunchLeaseError, match="CloseHandle"):
        lease.release()

    assert lease.released is True
    lease.release()
    assert attempts == [123]


def test_context_manager_holds_until_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_kernel(monkeypatch)

    with acquire_windows_launch_lease(
        lease_root=tmp_path,
        user_scope="user-A",
    ) as lease:
        assert lease.released is False
        with pytest.raises(WindowsLaunchLeaseHeldError):
            acquire_windows_launch_lease(
                lease_root=tmp_path,
                user_scope="user-A",
            )

    assert lease.released is True
    replacement = acquire_windows_launch_lease(
        lease_root=tmp_path,
        user_scope="user-A",
    )
    replacement.release()


@pytest.mark.parametrize(
    "user_scope",
    (
        "",
        " user-A",
        "user-A ",
        "user\\nA",
        "x" * 513,
    ),
)
def test_noncanonical_user_scope_fails_before_kernel_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    user_scope: str,
) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(
        lease_module,
        "_create_exclusive_windows_handle",
        lambda path: opened.append(path) or 123,
    )

    with pytest.raises(WindowsLaunchLeaseError, match="user_scope"):
        acquire_windows_launch_lease(
            lease_root=tmp_path,
            user_scope=user_scope,
        )

    assert opened == []


def test_lease_root_must_be_existing_absolute_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(
        lease_module,
        "_create_exclusive_windows_handle",
        lambda path: opened.append(path) or 123,
    )

    with pytest.raises(WindowsLaunchLeaseError, match="absolute"):
        acquire_windows_launch_lease(
            lease_root=Path("relative-launch-state"),
            user_scope="user-A",
        )

    with pytest.raises(WindowsLaunchLeaseError, match="existing directory"):
        acquire_windows_launch_lease(
            lease_root=tmp_path / "missing",
            user_scope="user-A",
        )

    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("x", encoding="utf-8")
    with pytest.raises(WindowsLaunchLeaseError, match="existing directory"):
        acquire_windows_launch_lease(
            lease_root=regular_file,
            user_scope="user-A",
        )

    assert opened == []


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows CreateFileW semantics")
def test_windows_kernel_no_share_handle_excludes_second_acquire(
    tmp_path: Path,
) -> None:
    first = acquire_windows_launch_lease(
        lease_root=tmp_path,
        user_scope="windows-candidate-user",
    )
    try:
        with pytest.raises(WindowsLaunchLeaseHeldError):
            acquire_windows_launch_lease(
                lease_root=tmp_path,
                user_scope="windows-candidate-user",
            )
    finally:
        first.release()

    replacement = acquire_windows_launch_lease(
        lease_root=tmp_path,
        user_scope="windows-candidate-user",
    )
    replacement.release()
