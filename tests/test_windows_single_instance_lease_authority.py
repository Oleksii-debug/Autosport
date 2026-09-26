from __future__ import annotations

from pathlib import Path
import threading

import pytest

import autosport.windows_single_instance_lease as lease_module
from autosport.windows_single_instance_lease import (
    WindowsLaunchLease,
    WindowsLaunchLeaseError,
    acquire_windows_launch_lease,
)


def test_public_lease_type_cannot_mint_kernel_ownership(tmp_path: Path) -> None:
    with pytest.raises(
        WindowsLaunchLeaseError,
        match="must be issued by acquire_windows_launch_lease",
    ):
        WindowsLaunchLease(
            path=tmp_path / "forged.lease",
            lease_key_sha256="0" * 64,
            user_scope="user-A",
        )


def test_object_new_forgery_has_no_release_authority() -> None:
    forged = object.__new__(WindowsLaunchLease)

    with pytest.raises(
        WindowsLaunchLeaseError,
        match="issuance authority is unavailable",
    ):
        forged.release()


def test_issued_lease_does_not_expose_mutable_raw_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []
    monkeypatch.setattr(
        lease_module,
        "_create_exclusive_windows_handle",
        lambda _path: 321,
    )
    monkeypatch.setattr(
        lease_module,
        "_close_windows_handle",
        closed.append,
    )

    lease = acquire_windows_launch_lease(
        lease_root=tmp_path,
        user_scope="user-A",
    )
    assert not hasattr(lease, "_handle")
    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(lease, "_handle", 999)

    lease.release()
    assert closed == [321]
    assert lease.released is True


def test_concurrent_release_closes_raw_handle_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lease_module,
        "_create_exclusive_windows_handle",
        lambda _path: 777,
    )
    entered = threading.Barrier(3)
    close_gate = threading.Event()
    closed: list[int] = []

    def close(handle: int) -> None:
        closed.append(handle)
        close_gate.wait(timeout=2)

    monkeypatch.setattr(lease_module, "_close_windows_handle", close)
    lease = acquire_windows_launch_lease(
        lease_root=tmp_path,
        user_scope="user-A",
    )

    def releaser() -> None:
        entered.wait()
        lease.release()

    threads = [threading.Thread(target=releaser) for _ in range(2)]
    for thread in threads:
        thread.start()
    entered.wait()
    close_gate.set()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert closed == [777]
    assert lease.released is True


def test_issued_lease_public_identity_is_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lease_module,
        "_create_exclusive_windows_handle",
        lambda _path: 654,
    )
    monkeypatch.setattr(
        lease_module,
        "_close_windows_handle",
        lambda _handle: None,
    )

    lease = acquire_windows_launch_lease(
        lease_root=tmp_path,
        user_scope="user-A",
    )
    try:
        with pytest.raises((AttributeError, TypeError)):
            lease.user_scope = "user-B"  # type: ignore[misc]
    finally:
        lease.release()
