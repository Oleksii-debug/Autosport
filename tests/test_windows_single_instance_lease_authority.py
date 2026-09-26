from __future__ import annotations

from pathlib import Path

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
