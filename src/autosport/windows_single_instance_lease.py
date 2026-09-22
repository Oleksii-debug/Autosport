from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Final


_LEASE_DOMAIN: Final[bytes] = b"AUTOSPORT_WINDOWS_SINGLE_INSTANCE_LEASE_V1\x00"
_MAX_USER_SCOPE_UTF8: Final[int] = 512

_ERROR_SHARING_VIOLATION: Final[int] = 32
_ERROR_LOCK_VIOLATION: Final[int] = 33


class WindowsLaunchLeaseError(RuntimeError):
    """Raised when process-lifetime Windows launch exclusivity cannot be proven."""


class WindowsLaunchLeaseHeldError(WindowsLaunchLeaseError):
    """Raised when another process already owns the exact per-user launch lease."""


def _canonical_user_scope(value: object) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise WindowsLaunchLeaseError("user_scope must be a non-empty trimmed string")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise WindowsLaunchLeaseError("user_scope contains control characters")
    encoded = value.encode("utf-8")
    if len(encoded) > _MAX_USER_SCOPE_UTF8:
        raise WindowsLaunchLeaseError("user_scope exceeds the supported identity bound")
    return value


def _canonical_lease_path(
    *,
    lease_root: str | Path,
    user_scope: str,
) -> tuple[Path, str]:
    user_scope = _canonical_user_scope(user_scope)
    root = Path(lease_root)
    if not root.is_absolute():
        raise WindowsLaunchLeaseError("lease_root must be an absolute path")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise WindowsLaunchLeaseError(
            "lease_root must resolve to an existing directory"
        ) from exc
    if not root.is_dir():
        raise WindowsLaunchLeaseError("lease_root must be an existing directory")

    lease_key = hashlib.sha256(
        _LEASE_DOMAIN + user_scope.encode("utf-8")
    ).hexdigest()
    return root / f"autosport-single-instance-{lease_key}.lease", lease_key


def _create_exclusive_windows_handle(path: Path) -> int:
    """Open one kernel-enforced no-share file handle for the process lifetime."""

    if os.name != "nt":
        raise WindowsLaunchLeaseError(
            "Windows single-instance lease is available only on Windows"
        )

    import ctypes
    from ctypes import wintypes

    generic_read = 0x80000000
    generic_write = 0x40000000
    open_always = 4
    file_attribute_normal = 0x00000080

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE

    handle = create_file(
        str(path),
        generic_read | generic_write,
        0,
        None,
        open_always,
        file_attribute_normal,
        None,
    )

    if isinstance(handle, int):
        raw_handle = handle
    elif handle is None:
        raw_handle = None
    else:
        raw_handle = ctypes.cast(handle, ctypes.c_void_p).value

    invalid_handle = ctypes.c_void_p(-1).value
    if raw_handle in {None, 0, invalid_handle}:
        error = ctypes.get_last_error()
        if error in {_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION}:
            raise WindowsLaunchLeaseHeldError(
                "Autosport is already running for this user scope"
            )
        raise WindowsLaunchLeaseError(
            f"CreateFileW launch lease failed with Win32 error {error}"
        )
    return int(raw_handle)


def _close_windows_handle(handle: int) -> None:
    if os.name != "nt":
        raise WindowsLaunchLeaseError(
            "Windows launch lease handle cannot be closed off Windows"
        )

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    if not close_handle(wintypes.HANDLE(handle)):
        error = ctypes.get_last_error()
        raise WindowsLaunchLeaseError(
            f"CloseHandle launch lease failed with Win32 error {error}"
        )


@dataclass(slots=True)
class WindowsLaunchLease:
    """One held process-lifetime per-user launch lease.

    The object must remain reachable for as long as the canonical launcher/runtime
    is allowed to own the single-instance slot. Windows releases the underlying
    kernel handle automatically on process termination; explicit release() is
    idempotent and closes it at most once.
    """

    path: Path
    lease_key_sha256: str
    user_scope: str
    _handle: int
    _released: bool = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        _close_windows_handle(self._handle)

    def __enter__(self) -> "WindowsLaunchLease":
        if self._released:
            raise WindowsLaunchLeaseError("released launch lease cannot be re-entered")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False


def acquire_windows_launch_lease(
    *,
    lease_root: str | Path,
    user_scope: str,
) -> WindowsLaunchLease:
    """Atomically acquire one Windows single-instance slot for user_scope.

    lease_root is deliberately not discovered here. Canonical Windows entry/
    installer composition must provide its already-qualified per-user launcher
    state root. This primitive derives an opaque, domain-separated filename from
    the canonical user scope and uses CreateFileW with dwShareMode=0.
    There is no separate check-then-act window: the kernel open is the acquisition.

    PID/process-start metadata from windows_launch_identity remains useful
    diagnostic/recovery evidence but is not mutual-exclusion authority.
    """

    path, lease_key = _canonical_lease_path(
        lease_root=lease_root,
        user_scope=user_scope,
    )
    handle = _create_exclusive_windows_handle(path)
    return WindowsLaunchLease(
        path=path,
        lease_key_sha256=lease_key,
        user_scope=_canonical_user_scope(user_scope),
        _handle=handle,
    )
