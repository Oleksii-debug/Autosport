from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .json_integrity import strict_json_loads
from .monotonic_workspace_authority import (
    AuthorityPhase,
    AuthorityRecord,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)


SCHEMA = "autosport.campaign_precommit_manifest"
SCHEMA_VERSION = 1
PUBLICATION_AUTHORITY_DOMAIN = "campaign-precommit-publication"
PUBLICATION_WITNESS_SCHEMA = "autosport.campaign_precommit_publication_witness"
PUBLICATION_WITNESS_SCHEMA_VERSION = 1
RAW_MANIFEST_IS_PROSPECTIVE_AUTHORITY = False

_STRUCTURAL_TX_PREFIX = "campaign-precommit-state-v1|"
_WITNESS_TX_PREFIX = "campaign-precommit-witness-v1|"

_POSIX_LINK_DIR_FD_SUPPORTED = os.link in os.supports_dir_fd
_POSIX_UNLINK_DIR_FD_SUPPORTED = os.unlink in os.supports_dir_fd
_POSIX_LINK_NOFOLLOW_SUPPORTED = os.link in os.supports_follow_symlinks
_HEX = frozenset("0123456789abcdef")
_RECORD_FIELDS = {
    "schema",
    "schema_version",
    "campaign_id",
    "source_id",
    "source_snapshot_sha256",
    "committed_at",
    "observation_not_before",
    "observation_not_after",
    "evaluation_universe_sha256",
    "strategy_version_id",
    "champion_version_id",
    "baseline_version_id",
    "cost_contract_sha256",
    "multiplicity_policy_sha256",
    "stopping_policy_sha256",
    "restart_policy_sha256",
    "causal_evidence_policy_sha256",
    "config_sha256",
    "manifest_sha256",
}


class CampaignPrecommitManifestError(ValueError):
    """Invalid, conflicting, or tampered campaign precommit evidence."""


def _text(value: object, field_name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise CampaignPrecommitManifestError(
            f"{field_name} must be non-empty canonical text"
        )
    value.encode("utf-8")
    return value


def _sha(value: object, field_name: str) -> str:
    raw = _text(value, field_name)
    if len(raw) != 64 or raw != raw.lower() or any(ch not in _HEX for ch in raw):
        raise CampaignPrecommitManifestError(
            f"{field_name} must be lowercase SHA-256 hex"
        )
    return raw


def _utc_timestamp(value: object, field_name: str) -> str:
    raw = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CampaignPrecommitManifestError(
            f"{field_name} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CampaignPrecommitManifestError(
            f"{field_name} must include a timezone"
        )
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if raw != canonical:
        raise CampaignPrecommitManifestError(
            f"{field_name} must use canonical UTC Z form"
        )
    return canonical


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _publication_now() -> datetime:
    """Return the product-observed publication clock in UTC."""

    return datetime.now(timezone.utc)


def _publication_deadline_reached(observation_not_before: str) -> bool:
    """Return whether a new canonical publication would no longer be prospective."""

    return _publication_now() >= _instant(observation_not_before)


def _publication_now_text() -> str:
    observed = _publication_now()
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise CampaignPrecommitManifestError(
            "publication clock must return a timezone-aware instant"
        )
    return (
        observed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _publication_observed_instant(value: str) -> datetime:
    if type(value) is not str:
        raise CampaignPrecommitManifestError(
            "publication witness time must be canonical text"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CampaignPrecommitManifestError(
            "publication witness time must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CampaignPrecommitManifestError(
            "publication witness time must include a timezone"
        )
    canonical = (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    if value != canonical:
        raise CampaignPrecommitManifestError(
            "publication witness time must use canonical UTC microsecond form"
        )
    return parsed.astimezone(timezone.utc)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _open_bound_posix_parent_directory(path: Path) -> tuple[int, Path]:
    """Open one symlink-free parent lineage and bind later publication to its fd."""

    if os.name == "nt":
        raise CampaignPrecommitManifestError(
            "POSIX campaign precommit parent binding is unavailable on Windows"
        )
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
    ):
        raise CampaignPrecommitManifestError(
            "platform lacks descriptor-relative campaign precommit primitives"
        )

    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(absolute.anchor, flags)
        try:
            for component in absolute.parts[1:]:
                next_descriptor = os.open(
                    component,
                    flags,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor
        except BaseException:
            os.close(descriptor)
            raise
    except OSError as exc:
        raise CampaignPrecommitManifestError(
            "campaign precommit parent directory must already exist "
            "without symlink redirection"
        ) from exc
    return descriptor, absolute


def _assert_bound_posix_parent_identity(path: Path, descriptor: int) -> None:
    """Fail closed if the requested parent path stopped naming the bound directory."""

    try:
        current = os.stat(path, follow_symlinks=False)
        bound = os.fstat(descriptor)
    except OSError as exc:
        raise CampaignPrecommitManifestError(
            "cannot revalidate campaign precommit parent identity"
        ) from exc
    if (current.st_dev, current.st_ino) != (bound.st_dev, bound.st_ino):
        raise CampaignPrecommitManifestError(
            "campaign precommit parent identity changed during publication"
        )


def _read_bound_posix_file_bytes(directory_fd: int, name: str) -> bytes:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
    except OSError as exc:
        raise CampaignPrecommitManifestError(
            "cannot verify existing campaign precommit manifest"
        ) from exc
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            return handle.read()
    except OSError as exc:
        raise CampaignPrecommitManifestError(
            "cannot verify existing campaign precommit manifest"
        ) from exc


def _fsync_bound_parent_directory(directory_fd: int) -> None:
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        raise CampaignPrecommitManifestError(
            "cannot fsync campaign precommit directory"
        ) from exc


def _publish_bound_posix_file_once(
    parent_fd: int,
    name: str,
    encoded: bytes,
    observation_not_before: str,
) -> bool:
    """Publish complete bytes atomically without exposing a partial canonical leaf."""

    if (
        not _POSIX_LINK_DIR_FD_SUPPORTED
        or not _POSIX_UNLINK_DIR_FD_SUPPORTED
        or not _POSIX_LINK_NOFOLLOW_SUPPORTED
    ):
        raise CampaignPrecommitManifestError(
            "platform lacks descriptor-relative no-clobber precommit publication"
        )

    temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    temporary_exists = False
    primary_error: BaseException | None = None

    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        temporary_exists = True
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

        if _publication_deadline_reached(observation_not_before):
            raise CampaignPrecommitManifestError(
                "first campaign precommit publication must precede prospective observation"
            )

        try:
            os.link(
                temporary_name,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            return False
        except OSError as exc:
            raise CampaignPrecommitManifestError(
                "cannot atomically publish campaign precommit manifest"
            ) from exc

        return True
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                if primary_error is not None:
                    try:
                        primary_error.add_note(
                            f"campaign precommit temp handle close also failed: {exc}"
                        )
                    except BaseException:
                        pass
                else:
                    raise CampaignPrecommitManifestError(
                        "cannot close campaign precommit temp file"
                    ) from exc

        cleanup_error: BaseException | None = None
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                cleanup_error = exc

        if cleanup_error is not None:
            if primary_error is not None:
                try:
                    primary_error.add_note(
                        f"campaign precommit temp cleanup also failed: {cleanup_error}"
                    )
                except BaseException:
                    pass
            else:
                if isinstance(cleanup_error, CampaignPrecommitManifestError):
                    raise cleanup_error
                raise CampaignPrecommitManifestError(
                    "cannot finalize campaign precommit temp cleanup"
                ) from cleanup_error


def _windows_api_path(path: Path) -> str:
    text = str(path)
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _close_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    if not close_handle(handle):
        raise CampaignPrecommitManifestError(
            "cannot close campaign precommit Windows handle"
        )


def _open_bound_windows_parent_directory(
    path: Path,
) -> tuple[int, Path, tuple[int, int, int]]:
    """Bind one existing reparse-free Windows parent lineage to a directory handle."""

    if os.name != "nt":
        raise CampaignPrecommitManifestError(
            "Windows campaign precommit parent binding is unavailable on this platform"
        )

    import ctypes
    from ctypes import wintypes

    class UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class IoStatusUnion(ctypes.Union):
        _fields_ = [("Status", wintypes.LONG), ("Pointer", wintypes.LPVOID)]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("u", IoStatusUnion), ("Information", ctypes.c_size_t)]

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_file_information = kernel32.GetFileInformationByHandle
    get_file_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    )
    get_file_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    nt_create_file = ntdll.NtCreateFile
    nt_create_file.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(ObjectAttributes),
        ctypes.POINTER(IoStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    )
    nt_create_file.restype = wintypes.LONG
    nt_set_information_file = ntdll.NtSetInformationFile
    nt_set_information_file.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
    )
    nt_set_information_file.restype = wintypes.LONG
    rtl_status_to_dos_error = ntdll.RtlNtStatusToDosError
    rtl_status_to_dos_error.argtypes = (wintypes.LONG,)
    rtl_status_to_dos_error.restype = wintypes.ULONG

    file_list_directory = 0x00000001
    file_traverse = 0x00000020
    file_read_attributes = 0x00000080
    synchronize = 0x00100000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    open_existing = 3
    file_attribute_directory = 0x00000010
    file_attribute_reparse_point = 0x00000400
    file_flag_open_reparse_point = 0x00200000
    file_flag_backup_semantics = 0x02000000
    file_open = 1
    file_directory_file = 0x00000001
    file_synchronous_io_nonalert = 0x00000020
    file_open_reparse_point = 0x00200000
    obj_case_insensitive = 0x00000040
    invalid_handle_value = ctypes.c_void_p(-1).value
    directory_access = (
        file_list_directory | file_traverse | file_read_attributes | synchronize
    )
    share_all = file_share_read | file_share_write | file_share_delete

    def directory_identity(handle: int) -> tuple[int, int, int]:
        information = ByHandleFileInformation()
        if not get_file_information(handle, ctypes.byref(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not information.dwFileAttributes & file_attribute_directory:
            raise CampaignPrecommitManifestError(
                "campaign precommit parent component is not a directory"
            )
        if information.dwFileAttributes & file_attribute_reparse_point:
            raise CampaignPrecommitManifestError(
                "campaign precommit parent directory contains reparse redirection"
            )
        return (
            int(information.dwVolumeSerialNumber),
            int(information.nFileIndexHigh),
            int(information.nFileIndexLow),
        )

    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or not absolute.anchor:
        raise CampaignPrecommitManifestError(
            "campaign precommit parent directory must be absolute"
        )

    root = create_file(
        _windows_api_path(Path(absolute.anchor)),
        directory_access,
        share_all,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    if root == invalid_handle_value:
        raise CampaignPrecommitManifestError(
            "campaign precommit parent directory must already exist "
            "without reparse redirection"
        )
    current_handle = int(root)
    try:
        current_identity = directory_identity(current_handle)
        for component in absolute.parts[1:]:
            name_buffer = ctypes.create_unicode_buffer(component)
            name_bytes = len(component.encode("utf-16-le"))
            unicode_name = UnicodeString(
                name_bytes,
                name_bytes + 2,
                ctypes.cast(name_buffer, wintypes.LPWSTR),
            )
            attributes = ObjectAttributes(
                ctypes.sizeof(ObjectAttributes),
                current_handle,
                ctypes.pointer(unicode_name),
                obj_case_insensitive,
                None,
                None,
            )
            io_status = IoStatusBlock()
            child = wintypes.HANDLE()
            status = nt_create_file(
                ctypes.byref(child),
                directory_access,
                ctypes.byref(attributes),
                ctypes.byref(io_status),
                None,
                file_attribute_directory,
                share_all,
                file_open,
                file_directory_file
                | file_synchronous_io_nonalert
                | file_open_reparse_point,
                None,
                0,
            )
            if status < 0:
                raise ctypes.WinError(int(rtl_status_to_dos_error(status)))
            child_handle = int(child.value)
            try:
                child_identity = directory_identity(child_handle)
            except BaseException:
                close_handle(child_handle)
                raise
            if not close_handle(current_handle):
                close_handle(child_handle)
                raise ctypes.WinError(ctypes.get_last_error())
            current_handle = child_handle
            current_identity = child_identity
        return current_handle, absolute, current_identity
    except BaseException as exc:
        if not close_handle(current_handle):
            try:
                exc.add_note("campaign precommit Windows parent handle cleanup also failed")
            except BaseException:
                pass
        if isinstance(exc, CampaignPrecommitManifestError):
            raise
        raise CampaignPrecommitManifestError(
            "campaign precommit parent directory must already exist "
            "without reparse redirection"
        ) from exc


def _assert_bound_windows_parent_identity(
    path: Path,
    expected_identity: tuple[int, int, int],
) -> None:
    current_handle, _, current_identity = _open_bound_windows_parent_directory(path)
    try:
        if current_identity != expected_identity:
            raise CampaignPrecommitManifestError(
                "campaign precommit parent identity changed during publication"
            )
    finally:
        _close_windows_handle(current_handle)


def _read_bound_windows_file_bytes(parent_handle: int, name: str) -> bytes:
    import ctypes
    from ctypes import wintypes

    class UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class IoStatusUnion(ctypes.Union):
        _fields_ = [("Status", wintypes.LONG), ("Pointer", wintypes.LPVOID)]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("u", IoStatusUnion), ("Information", ctypes.c_size_t)]

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    ntdll = ctypes.WinDLL("ntdll")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    nt_create_file = ntdll.NtCreateFile
    nt_create_file.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(ObjectAttributes),
        ctypes.POINTER(IoStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    )
    nt_create_file.restype = wintypes.LONG
    rtl_status_to_dos_error = ntdll.RtlNtStatusToDosError
    rtl_status_to_dos_error.argtypes = (wintypes.LONG,)
    rtl_status_to_dos_error.restype = wintypes.ULONG
    get_file_information = kernel32.GetFileInformationByHandle
    get_file_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    )
    get_file_information.restype = wintypes.BOOL
    read_file = kernel32.ReadFile
    read_file.argtypes = (
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    )
    read_file.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    file_read_data = 0x00000001
    file_read_attributes = 0x00000080
    synchronize = 0x00100000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    file_attribute_reparse_point = 0x00000400
    file_attribute_directory = 0x00000010
    file_open = 1
    file_synchronous_io_nonalert = 0x00000020
    file_non_directory_file = 0x00000040
    file_open_reparse_point = 0x00200000
    obj_case_insensitive = 0x00000040

    name_buffer = ctypes.create_unicode_buffer(name)
    name_bytes = len(name.encode("utf-16-le"))
    unicode_name = UnicodeString(
        name_bytes,
        name_bytes + 2,
        ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attributes = ObjectAttributes(
        ctypes.sizeof(ObjectAttributes),
        parent_handle,
        ctypes.pointer(unicode_name),
        obj_case_insensitive,
        None,
        None,
    )
    io_status = IoStatusBlock()
    file_handle = wintypes.HANDLE()
    status = nt_create_file(
        ctypes.byref(file_handle),
        file_read_data | file_read_attributes | synchronize,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        0,
        file_share_read | file_share_write | file_share_delete,
        file_open,
        file_synchronous_io_nonalert
        | file_non_directory_file
        | file_open_reparse_point,
        None,
        0,
    )
    if status < 0:
        raise CampaignPrecommitManifestError(
            "cannot verify existing campaign precommit manifest"
        ) from ctypes.WinError(int(rtl_status_to_dos_error(status)))

    handle = int(file_handle.value)
    primary_error: BaseException | None = None
    try:
        information = ByHandleFileInformation()
        if not get_file_information(handle, ctypes.byref(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        if (
            information.dwFileAttributes & file_attribute_reparse_point
            or information.dwFileAttributes & file_attribute_directory
        ):
            raise CampaignPrecommitManifestError(
                "cannot verify existing campaign precommit manifest"
            )

        chunks: list[bytes] = []
        while True:
            buffer = ctypes.create_string_buffer(65536)
            read = wintypes.DWORD()
            if not read_file(
                handle,
                buffer,
                len(buffer),
                ctypes.byref(read),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if read.value == 0:
                break
            chunks.append(buffer.raw[: read.value])
        return b"".join(chunks)
    except BaseException as exc:
        primary_error = exc
        if isinstance(exc, CampaignPrecommitManifestError):
            raise
        raise CampaignPrecommitManifestError(
            "cannot verify existing campaign precommit manifest"
        ) from exc
    finally:
        if not close_handle(handle) and primary_error is None:
            raise CampaignPrecommitManifestError(
                "cannot close campaign precommit manifest handle"
            )


def _publish_bound_windows_file_once(
    parent_handle: int,
    name: str,
    encoded: bytes,
    observation_not_before: str,
) -> bool:
    """Publish complete bytes by handle-relative no-clobber rename on Windows."""

    import ctypes
    from ctypes import wintypes

    class UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class IoStatusUnion(ctypes.Union):
        _fields_ = [("Status", wintypes.LONG), ("Pointer", wintypes.LPVOID)]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("u", IoStatusUnion), ("Information", ctypes.c_size_t)]

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", ctypes.c_ubyte)]

    name_length = len(name)

    class FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("ReplaceOrFlags", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * (name_length + 1)),
        ]

    ntdll = ctypes.WinDLL("ntdll")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    nt_create_file = ntdll.NtCreateFile
    nt_create_file.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(ObjectAttributes),
        ctypes.POINTER(IoStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    )
    nt_create_file.restype = wintypes.LONG
    nt_set_information_file = ntdll.NtSetInformationFile
    nt_set_information_file.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
    )
    nt_set_information_file.restype = wintypes.LONG
    rtl_status_to_dos_error = ntdll.RtlNtStatusToDosError
    rtl_status_to_dos_error.argtypes = (wintypes.LONG,)
    rtl_status_to_dos_error.restype = wintypes.ULONG
    write_file = kernel32.WriteFile
    write_file.argtypes = (
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    )
    write_file.restype = wintypes.BOOL
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = (wintypes.HANDLE,)
    flush_file_buffers.restype = wintypes.BOOL
    set_file_information = kernel32.SetFileInformationByHandle
    set_file_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    set_file_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    file_write_data = 0x00000002
    delete_access = 0x00010000
    synchronize = 0x00100000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    file_attribute_normal = 0x00000080
    file_create = 2
    file_synchronous_io_nonalert = 0x00000020
    file_non_directory_file = 0x00000040
    file_open_reparse_point = 0x00200000
    obj_case_insensitive = 0x00000040
    file_rename_information_class = 10
    file_disposition_info_class = 4
    error_file_exists = 80
    error_already_exists = 183

    temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
    temporary_buffer = ctypes.create_unicode_buffer(temporary_name)
    temporary_bytes = len(temporary_name.encode("utf-16-le"))
    temporary_unicode = UnicodeString(
        temporary_bytes,
        temporary_bytes + 2,
        ctypes.cast(temporary_buffer, wintypes.LPWSTR),
    )
    attributes = ObjectAttributes(
        ctypes.sizeof(ObjectAttributes),
        parent_handle,
        ctypes.pointer(temporary_unicode),
        obj_case_insensitive,
        None,
        None,
    )
    io_status = IoStatusBlock()
    file_handle = wintypes.HANDLE()
    status = nt_create_file(
        ctypes.byref(file_handle),
        file_write_data | delete_access | synchronize,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        file_attribute_normal,
        file_share_read | file_share_write | file_share_delete,
        file_create,
        file_synchronous_io_nonalert
        | file_non_directory_file
        | file_open_reparse_point,
        None,
        0,
    )
    if status < 0:
        raise CampaignPrecommitManifestError(
            "cannot create campaign precommit temporary file"
        ) from ctypes.WinError(int(rtl_status_to_dos_error(status)))

    handle = int(file_handle.value)
    primary_error: BaseException | None = None
    published = False
    collision = False
    try:
        offset = 0
        while offset < len(encoded):
            chunk = encoded[offset : offset + 65536]
            buffer = ctypes.create_string_buffer(chunk)
            written = wintypes.DWORD()
            if not write_file(
                handle,
                buffer,
                len(chunk),
                ctypes.byref(written),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if written.value <= 0:
                raise OSError("Windows campaign precommit wrote zero bytes")
            offset += int(written.value)

        if not flush_file_buffers(handle):
            raise ctypes.WinError(ctypes.get_last_error())

        rename_info = FileRenameInfo()
        rename_info.ReplaceOrFlags = 0
        rename_info.RootDirectory = parent_handle
        rename_info.FileNameLength = len(name.encode("utf-16-le"))
        rename_info.FileName = name
        if _publication_deadline_reached(observation_not_before):
            raise CampaignPrecommitManifestError(
                "first campaign precommit publication must precede prospective observation"
            )
        rename_io_status = IoStatusBlock()
        rename_status = nt_set_information_file(
            handle,
            ctypes.byref(rename_io_status),
            ctypes.byref(rename_info),
            ctypes.sizeof(rename_info),
            file_rename_information_class,
        )
        if rename_status < 0:
            error_code = int(rtl_status_to_dos_error(rename_status))
            if error_code in {error_file_exists, error_already_exists}:
                collision = True
            else:
                raise ctypes.WinError(error_code)
        else:
            published = True
            if not flush_file_buffers(handle):
                raise ctypes.WinError(ctypes.get_last_error())

        return published
    except BaseException as exc:
        primary_error = exc
        if isinstance(exc, CampaignPrecommitManifestError):
            raise
        raise CampaignPrecommitManifestError(
            "cannot atomically publish campaign precommit manifest"
        ) from exc
    finally:
        cleanup_error: BaseException | None = None
        if not published:
            disposition = FileDispositionInfo(1)
            if not set_file_information(
                handle,
                file_disposition_info_class,
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            ):
                cleanup_error = ctypes.WinError(ctypes.get_last_error())
        if not close_handle(handle) and cleanup_error is None:
            cleanup_error = ctypes.WinError(ctypes.get_last_error())

        if primary_error is not None and cleanup_error is not None:
            try:
                primary_error.add_note(
                    f"campaign precommit temp cleanup also failed: {cleanup_error}"
                )
            except BaseException:
                pass
        elif primary_error is None and cleanup_error is not None:
            raise CampaignPrecommitManifestError(
                "cannot finalize campaign precommit temporary file"
            ) from cleanup_error

        if collision and cleanup_error is not None and primary_error is None:
            raise CampaignPrecommitManifestError(
                "cannot remove losing campaign precommit temporary file"
            ) from cleanup_error


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class CampaignPrecommitPublicationWitness:
    """Committed product-owned witness that exact precommit bytes were prospective."""

    campaign_id: str
    manifest_sha256: str
    observation_not_before: str
    post_publish_observed_at: str
    target_relative_path: str
    workspace_instance_id: str
    authority_id: str
    authority_generation: int
    authority_record_sha256: str
    semantic_binding_sha256: str


@dataclass(frozen=True, slots=True)
class CampaignPrecommitManifest:
    """Immutable prospective PAPER-campaign configuration evidence.

    The manifest freezes identities that may affect evaluation before prospective
    observation starts. It is evidence of precommitment only: it does not grant
    provider-write, execution, settlement, promotion, readiness, or real-money
    authority, and write-once file persistence is not an anti-rollback authority.
    """

    campaign_id: str
    source_id: str
    source_snapshot_sha256: str
    committed_at: str
    observation_not_before: str
    observation_not_after: str
    evaluation_universe_sha256: str
    strategy_version_id: str
    champion_version_id: str
    baseline_version_id: str
    cost_contract_sha256: str
    multiplicity_policy_sha256: str
    stopping_policy_sha256: str
    restart_policy_sha256: str
    causal_evidence_policy_sha256: str
    config_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "campaign_id",
            "source_id",
            "strategy_version_id",
            "champion_version_id",
            "baseline_version_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        for name in (
            "source_snapshot_sha256",
            "evaluation_universe_sha256",
            "cost_contract_sha256",
            "multiplicity_policy_sha256",
            "stopping_policy_sha256",
            "restart_policy_sha256",
            "causal_evidence_policy_sha256",
            "config_sha256",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        for name in (
            "committed_at",
            "observation_not_before",
            "observation_not_after",
        ):
            object.__setattr__(
                self,
                name,
                _utc_timestamp(getattr(self, name), name),
            )

        committed = _instant(self.committed_at)
        first_observation = _instant(self.observation_not_before)
        last_observation = _instant(self.observation_not_after)
        if committed >= first_observation:
            raise CampaignPrecommitManifestError(
                "precommit must be committed before prospective observation begins"
            )
        if first_observation >= last_observation:
            raise CampaignPrecommitManifestError(
                "observation_not_before must precede observation_not_after"
            )
        if self.champion_version_id == self.baseline_version_id:
            raise CampaignPrecommitManifestError(
                "champion_version_id and baseline_version_id must be distinct"
            )

    def payload(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "campaign_id": self.campaign_id,
            "source_id": self.source_id,
            "source_snapshot_sha256": self.source_snapshot_sha256,
            "committed_at": self.committed_at,
            "observation_not_before": self.observation_not_before,
            "observation_not_after": self.observation_not_after,
            "evaluation_universe_sha256": self.evaluation_universe_sha256,
            "strategy_version_id": self.strategy_version_id,
            "champion_version_id": self.champion_version_id,
            "baseline_version_id": self.baseline_version_id,
            "cost_contract_sha256": self.cost_contract_sha256,
            "multiplicity_policy_sha256": self.multiplicity_policy_sha256,
            "stopping_policy_sha256": self.stopping_policy_sha256,
            "restart_policy_sha256": self.restart_policy_sha256,
            "causal_evidence_policy_sha256": self.causal_evidence_policy_sha256,
            "config_sha256": self.config_sha256,
        }

    @property
    def manifest_sha256(self) -> str:
        return _digest(self.payload())

    def to_record(self) -> dict[str, object]:
        return {
            **self.payload(),
            "manifest_sha256": self.manifest_sha256,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> "CampaignPrecommitManifest":
        if type(record) is not dict:
            raise CampaignPrecommitManifestError(
                "precommit record must be an exact JSON object"
            )
        if set(record) != _RECORD_FIELDS:
            raise CampaignPrecommitManifestError(
                "precommit record fields do not match schema"
            )
        if record.get("schema") != SCHEMA:
            raise CampaignPrecommitManifestError("precommit schema mismatch")
        version = record.get("schema_version")
        if type(version) is not int or version != SCHEMA_VERSION:
            raise CampaignPrecommitManifestError("precommit schema version mismatch")

        manifest = cls(
            campaign_id=record["campaign_id"],
            source_id=record["source_id"],
            source_snapshot_sha256=record["source_snapshot_sha256"],
            committed_at=record["committed_at"],
            observation_not_before=record["observation_not_before"],
            observation_not_after=record["observation_not_after"],
            evaluation_universe_sha256=record["evaluation_universe_sha256"],
            strategy_version_id=record["strategy_version_id"],
            champion_version_id=record["champion_version_id"],
            baseline_version_id=record["baseline_version_id"],
            cost_contract_sha256=record["cost_contract_sha256"],
            multiplicity_policy_sha256=record["multiplicity_policy_sha256"],
            stopping_policy_sha256=record["stopping_policy_sha256"],
            restart_policy_sha256=record["restart_policy_sha256"],
            causal_evidence_policy_sha256=record["causal_evidence_policy_sha256"],
            config_sha256=record["config_sha256"],
        )
        claimed = _sha(record["manifest_sha256"], "manifest_sha256")
        if claimed != manifest.manifest_sha256:
            raise CampaignPrecommitManifestError(
                "precommit manifest digest mismatch"
            )
        return manifest

def load_campaign_precommit_manifest(
    path: str | os.PathLike[str],
) -> CampaignPrecommitManifest:
    target = Path(path)
    name = target.name
    if name in {"", ".", ".."} or Path(name).name != name:
        raise CampaignPrecommitManifestError(
            "campaign precommit target must name one file"
        )

    if os.name == "nt":
        parent_handle, absolute_parent, parent_identity = (
            _open_bound_windows_parent_directory(target.parent)
        )
        try:
            raw_bytes = _read_bound_windows_file_bytes(parent_handle, name)
            _assert_bound_windows_parent_identity(
                absolute_parent,
                parent_identity,
            )
        finally:
            _close_windows_handle(parent_handle)
    else:
        parent_fd, absolute_parent = _open_bound_posix_parent_directory(target.parent)
        try:
            raw_bytes = _read_bound_posix_file_bytes(parent_fd, name)
            _assert_bound_posix_parent_identity(absolute_parent, parent_fd)
        finally:
            os.close(parent_fd)

    try:
        text = raw_bytes.decode("utf-8", errors="strict")
        raw = strict_json_loads(text)
    except (UnicodeError, ValueError) as exc:
        raise CampaignPrecommitManifestError(
            "cannot read campaign precommit manifest"
        ) from exc

    manifest = CampaignPrecommitManifest.from_record(raw)
    canonical_bytes = _canonical_bytes(manifest.to_record()) + b"\n"
    if raw_bytes != canonical_bytes:
        raise CampaignPrecommitManifestError(
            "campaign precommit manifest bytes are not canonical"
        )
    return manifest


def write_campaign_precommit_manifest_once(
    path: str | os.PathLike[str],
    manifest: CampaignPrecommitManifest,
) -> str:
    """Persist exact precommit bytes once, with byte-identical idempotent retry.

    This protects the local artifact against silent in-place replacement and makes
    later byte tampering detectable. The parent directory must already exist and be
    provisioned by the canonical workspace/storage authority: this writer will not
    silently create a directory lineage whose crash durability it cannot prove. On
    POSIX, publication is descriptor-relative to one symlink-free parent identity.

    This low-level byte writer is deliberately NOT standalone prospective authority:
    callers making a prospective claim must use the monotonic publication-witness
    composition below. A scheduler pause can always occur between a wall-clock check
    and the filesystem publication primitive, and this function does not claim
    rollback protection if the whole workspace is deleted and recreated.
    """

    if type(manifest) is not CampaignPrecommitManifest:
        raise CampaignPrecommitManifestError(
            "manifest must be CampaignPrecommitManifest"
        )
    target = Path(path)
    name = target.name
    if name in {"", ".", ".."} or Path(name).name != name:
        raise CampaignPrecommitManifestError(
            "campaign precommit target must name one file"
        )
    encoded = _canonical_bytes(manifest.to_record()) + b"\n"

    if os.name == "nt":
        parent_handle, absolute_parent, parent_identity = (
            _open_bound_windows_parent_directory(target.parent)
        )
        try:
            if _publication_deadline_reached(manifest.observation_not_before):
                try:
                    existing = _read_bound_windows_file_bytes(parent_handle, name)
                except CampaignPrecommitManifestError as exc:
                    raise CampaignPrecommitManifestError(
                        "first campaign precommit publication must precede prospective observation"
                    ) from exc
                if existing != encoded:
                    raise CampaignPrecommitManifestError(
                        "existing campaign precommit manifest conflicts with precommit"
                    )
                published = False
            else:
                published = _publish_bound_windows_file_once(
                    parent_handle,
                    name,
                    encoded,
                    manifest.observation_not_before,
                )
            if not published:
                existing = _read_bound_windows_file_bytes(parent_handle, name)
                if existing != encoded:
                    raise CampaignPrecommitManifestError(
                        "existing campaign precommit manifest conflicts with precommit"
                    )

            _assert_bound_windows_parent_identity(
                absolute_parent,
                parent_identity,
            )
            if _read_bound_windows_file_bytes(parent_handle, name) != encoded:
                raise CampaignPrecommitManifestError(
                    "persisted campaign precommit manifest failed exact re-read"
                )
            return manifest.manifest_sha256
        finally:
            _close_windows_handle(parent_handle)

    parent_fd, absolute_parent = _open_bound_posix_parent_directory(target.parent)
    try:
        if _publication_deadline_reached(manifest.observation_not_before):
            try:
                existing = _read_bound_posix_file_bytes(parent_fd, name)
            except CampaignPrecommitManifestError as exc:
                raise CampaignPrecommitManifestError(
                    "first campaign precommit publication must precede prospective observation"
                ) from exc
            if existing != encoded:
                raise CampaignPrecommitManifestError(
                    "existing campaign precommit manifest conflicts with precommit"
                )
            published = False
        else:
            published = _publish_bound_posix_file_once(
                parent_fd,
                name,
                encoded,
                manifest.observation_not_before,
            )
        if not published:
            existing = _read_bound_posix_file_bytes(parent_fd, name)
            if existing != encoded:
                raise CampaignPrecommitManifestError(
                    "existing campaign precommit manifest conflicts with precommit"
                )

        _fsync_bound_parent_directory(parent_fd)
        _assert_bound_posix_parent_identity(absolute_parent, parent_fd)
        if _read_bound_posix_file_bytes(parent_fd, name) != encoded:
            raise CampaignPrecommitManifestError(
                "persisted campaign precommit manifest failed exact re-read"
            )
        return manifest.manifest_sha256
    finally:
        os.close(parent_fd)



def _publication_target_context(
    path: str | os.PathLike[str],
    *,
    workspace: str | os.PathLike[str],
) -> tuple[Path, Path, str]:
    workspace_path = Path(workspace).expanduser()
    if not workspace_path.is_absolute():
        raise CampaignPrecommitManifestError(
            "campaign precommit workspace must be an absolute path"
        )
    absolute_workspace = Path(os.path.abspath(workspace_path))
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = absolute_workspace / target
    absolute_target = Path(os.path.abspath(target))
    try:
        relative = absolute_target.relative_to(absolute_workspace)
    except ValueError as exc:
        raise CampaignPrecommitManifestError(
            "campaign precommit target must be inside the protected workspace"
        ) from exc
    if relative == Path(".") or absolute_target.name in {"", ".", ".."}:
        raise CampaignPrecommitManifestError(
            "campaign precommit target must name one workspace file"
        )
    return absolute_workspace, absolute_target, relative.as_posix()


def _publication_structural_binding_sha256(
    manifest: CampaignPrecommitManifest,
    target_relative_path: str,
) -> str:
    return _digest(
        {
            "schema": "autosport.campaign_precommit_publication_state",
            "schema_version": 1,
            "campaign_id": manifest.campaign_id,
            "manifest_sha256": manifest.manifest_sha256,
            "observation_not_before": manifest.observation_not_before,
            "target_relative_path": target_relative_path,
        }
    )


def _publication_witness_binding_sha256(
    manifest: CampaignPrecommitManifest,
    target_relative_path: str,
    post_publish_observed_at: str,
) -> str:
    observed = _publication_observed_instant(post_publish_observed_at)
    if observed >= _instant(manifest.observation_not_before):
        raise CampaignPrecommitManifestError(
            "publication witness must observe exact manifest bytes before prospective observation"
        )
    return _digest(
        {
            "schema": PUBLICATION_WITNESS_SCHEMA,
            "schema_version": PUBLICATION_WITNESS_SCHEMA_VERSION,
            "campaign_id": manifest.campaign_id,
            "manifest_sha256": manifest.manifest_sha256,
            "observation_not_before": manifest.observation_not_before,
            "post_publish_observed_at": post_publish_observed_at,
            "target_relative_path": target_relative_path,
        }
    )


def _publication_structural_tx_id() -> str:
    return f"{_STRUCTURAL_TX_PREFIX}{uuid.uuid4().hex}"


def _publication_witness_tx_id(post_publish_observed_at: str) -> str:
    _publication_observed_instant(post_publish_observed_at)
    return (
        f"{_WITNESS_TX_PREFIX}{post_publish_observed_at}|{uuid.uuid4().hex}"
    )


def _witness_time_from_tx_id(tx_id: str) -> str:
    if not tx_id.startswith(_WITNESS_TX_PREFIX):
        raise CampaignPrecommitManifestError(
            "campaign precommit authority tip is not a publication witness"
        )
    suffix = tx_id[len(_WITNESS_TX_PREFIX) :]
    parts = suffix.split("|")
    if len(parts) != 2 or not parts[1]:
        raise CampaignPrecommitManifestError(
            "campaign precommit publication witness transaction is malformed"
        )
    observed = parts[0]
    _publication_observed_instant(observed)
    return observed


def _local_manifest_digest(path: Path) -> str | None:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CampaignPrecommitManifestError(
            "cannot inspect campaign precommit manifest path"
        ) from exc
    return load_campaign_precommit_manifest(path).manifest_sha256


def _latest_commit_record(
    history: tuple[AuthorityRecord, ...],
) -> AuthorityRecord | None:
    return next(
        (
            record
            for record in reversed(history)
            if record.phase is AuthorityPhase.COMMIT
        ),
        None,
    )


def _publication_authority(
    *,
    workspace: Path,
    campaign_id: str,
    workspace_instance_id: str | None,
    authority_root: str | os.PathLike[str] | None,
) -> MonotonicWorkspaceAuthority:
    try:
        return MonotonicWorkspaceAuthority(
            workspace=workspace,
            workspace_instance_id=workspace_instance_id,
            domain=PUBLICATION_AUTHORITY_DOMAIN,
            key=campaign_id,
            authority_root=authority_root,
        )
    except MonotonicWorkspaceAuthorityError as exc:
        raise CampaignPrecommitManifestError(
            "cannot resolve campaign precommit publication authority"
        ) from exc


def _validate_structural_record(
    record: AuthorityRecord,
    *,
    manifest: CampaignPrecommitManifest,
    structural_binding_sha256: str,
) -> None:
    if (
        record.intended_state_sha256 != manifest.manifest_sha256
        or record.semantic_binding_sha256 != structural_binding_sha256
        or not record.tx_id.startswith(_STRUCTURAL_TX_PREFIX)
    ):
        raise CampaignPrecommitManifestError(
            "campaign precommit structural authority conflicts with manifest"
        )


def _witness_from_record(
    record: AuthorityRecord,
    *,
    manifest: CampaignPrecommitManifest,
    target_relative_path: str,
) -> CampaignPrecommitPublicationWitness:
    if record.phase is not AuthorityPhase.COMMIT:
        raise CampaignPrecommitManifestError(
            "campaign precommit publication witness is not committed"
        )
    observed_at = _witness_time_from_tx_id(record.tx_id)
    expected_binding = _publication_witness_binding_sha256(
        manifest,
        target_relative_path,
        observed_at,
    )
    if (
        record.intended_state_sha256 != manifest.manifest_sha256
        or record.semantic_binding_sha256 != expected_binding
    ):
        raise CampaignPrecommitManifestError(
            "campaign precommit publication witness conflicts with manifest"
        )
    return CampaignPrecommitPublicationWitness(
        campaign_id=manifest.campaign_id,
        manifest_sha256=manifest.manifest_sha256,
        observation_not_before=manifest.observation_not_before,
        post_publish_observed_at=observed_at,
        target_relative_path=target_relative_path,
        workspace_instance_id=record.workspace_instance_id,
        authority_id=record.authority_id,
        authority_generation=record.generation,
        authority_record_sha256=record.record_sha256,
        semantic_binding_sha256=record.semantic_binding_sha256,
    )


def _commit_publication_witness(
    authority: MonotonicWorkspaceAuthority,
    *,
    manifest: CampaignPrecommitManifest,
    target_relative_path: str,
    post_publish_observed_at: str,
) -> CampaignPrecommitPublicationWitness:
    binding = _publication_witness_binding_sha256(
        manifest,
        target_relative_path,
        post_publish_observed_at,
    )
    tx_id = _publication_witness_tx_id(post_publish_observed_at)
    try:
        authority.prepare(
            tx_id=tx_id,
            observed_state_sha256=manifest.manifest_sha256,
            intended_state_sha256=manifest.manifest_sha256,
            semantic_binding_sha256=binding,
        )
        record = authority.commit(
            tx_id=tx_id,
            observed_state_sha256=manifest.manifest_sha256,
            semantic_binding_sha256=binding,
        )
    except MonotonicWorkspaceAuthorityError as exc:
        raise CampaignPrecommitManifestError(
            "cannot commit campaign precommit publication witness"
        ) from exc
    return _witness_from_record(
        record,
        manifest=manifest,
        target_relative_path=target_relative_path,
    )


def resolve_campaign_precommit_publication_witness(
    path: str | os.PathLike[str],
    *,
    workspace: str | os.PathLike[str],
    workspace_instance_id: str | None = None,
    authority_root: str | os.PathLike[str] | None = None,
) -> CampaignPrecommitPublicationWitness:
    """Resolve exact manifest + committed monotonic witness without issuing authority."""
    absolute_workspace, target, relative = _publication_target_context(
        path,
        workspace=workspace,
    )
    manifest = load_campaign_precommit_manifest(target)
    authority = _publication_authority(
        workspace=absolute_workspace,
        campaign_id=manifest.campaign_id,
        workspace_instance_id=workspace_instance_id,
        authority_root=authority_root,
    )
    try:
        history = authority.read_history()
    except MonotonicWorkspaceAuthorityError as exc:
        raise CampaignPrecommitManifestError(
            "cannot read campaign precommit publication authority"
        ) from exc
    if history and history[-1].phase is AuthorityPhase.PREPARE:
        raise CampaignPrecommitManifestError(
            "campaign precommit publication witness requires recovery"
        )
    latest = _latest_commit_record(history)
    if latest is None:
        raise CampaignPrecommitManifestError(
            "campaign precommit manifest has no committed publication witness"
        )
    witness = _witness_from_record(
        latest,
        manifest=manifest,
        target_relative_path=relative,
    )
    try:
        recovery = authority.recover(
            observed_state_sha256=manifest.manifest_sha256,
        )
    except MonotonicWorkspaceAuthorityError as exc:
        raise CampaignPrecommitManifestError(
            "campaign precommit publication authority does not match local manifest"
        ) from exc
    if (
        recovery.record is None
        or recovery.record.record_sha256 != witness.authority_record_sha256
    ):
        raise CampaignPrecommitManifestError(
            "campaign precommit publication witness is not the current authority tip"
        )
    return witness


def publish_campaign_precommit_manifest(
    path: str | os.PathLike[str],
    manifest: CampaignPrecommitManifest,
    *,
    workspace: str | os.PathLike[str],
    workspace_instance_id: str | None = None,
    authority_root: str | os.PathLike[str] | None = None,
) -> CampaignPrecommitPublicationWitness:
    """Publish and monotonically witness one prospective campaign precommit.

    Raw manifest bytes are only structural evidence. Prospective authority is issued
    only after exact bytes are visible, the product clock observes them strictly
    before observation_not_before, and the existing independent monotonic workspace
    authority commits a witness carrying that product-observed instant.
    """

    if type(manifest) is not CampaignPrecommitManifest:
        raise CampaignPrecommitManifestError(
            "manifest must be CampaignPrecommitManifest"
        )
    absolute_workspace, target, relative = _publication_target_context(
        path,
        workspace=workspace,
    )
    authority = _publication_authority(
        workspace=absolute_workspace,
        campaign_id=manifest.campaign_id,
        workspace_instance_id=workspace_instance_id,
        authority_root=authority_root,
    )
    structural_binding = _publication_structural_binding_sha256(manifest, relative)

    local_digest = _local_manifest_digest(target)
    try:
        history = authority.read_history()
    except MonotonicWorkspaceAuthorityError as exc:
        raise CampaignPrecommitManifestError(
            "cannot read campaign precommit publication authority"
        ) from exc

    if history and history[-1].phase is AuthorityPhase.PREPARE:
        pending = history[-1]
        if pending.tx_id.startswith(_WITNESS_TX_PREFIX):
            observed_at = _witness_time_from_tx_id(pending.tx_id)
            expected_binding = _publication_witness_binding_sha256(
                manifest,
                relative,
                observed_at,
            )
            if (
                pending.intended_state_sha256 != manifest.manifest_sha256
                or pending.semantic_binding_sha256 != expected_binding
                or local_digest != manifest.manifest_sha256
            ):
                raise CampaignPrecommitManifestError(
                    "pending publication witness conflicts with local manifest"
                )
            try:
                record = authority.commit(
                    tx_id=pending.tx_id,
                    observed_state_sha256=manifest.manifest_sha256,
                    semantic_binding_sha256=expected_binding,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise CampaignPrecommitManifestError(
                    "cannot recover campaign precommit publication witness"
                ) from exc
            if load_campaign_precommit_manifest(target).manifest_sha256 != (
                manifest.manifest_sha256
            ):
                raise CampaignPrecommitManifestError(
                    "campaign precommit changed while recovering publication witness"
                )
            return _witness_from_record(
                record,
                manifest=manifest,
                target_relative_path=relative,
            )

        _validate_structural_record(
            pending,
            manifest=manifest,
            structural_binding_sha256=structural_binding,
        )
        if local_digest is None:
            try:
                authority.abort(
                    tx_id=pending.tx_id,
                    observed_state_sha256=pending.previous_committed_state_sha256,
                    semantic_binding_sha256=structural_binding,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise CampaignPrecommitManifestError(
                    "cannot abort incomplete campaign precommit publication"
                ) from exc
            history = authority.read_history()
        elif local_digest == manifest.manifest_sha256:
            # A prior writer may have crashed after canonical visibility but before
            # proving the platform durability/identity barrier. Re-run the exact
            # idempotent byte writer before upgrading structural PREPARE to COMMIT.
            write_campaign_precommit_manifest_once(target, manifest)
            loaded = load_campaign_precommit_manifest(target)
            observed_at = _publication_now_text()
            try:
                authority.commit(
                    tx_id=pending.tx_id,
                    observed_state_sha256=manifest.manifest_sha256,
                    semantic_binding_sha256=structural_binding,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise CampaignPrecommitManifestError(
                    "cannot recover campaign precommit structural publication"
                ) from exc
            if _publication_observed_instant(observed_at) >= _instant(
                loaded.observation_not_before
            ):
                raise CampaignPrecommitManifestError(
                    "manifest exists but no prospective publication witness was issued before observation"
                )
            return _commit_publication_witness(
                authority,
                manifest=loaded,
                target_relative_path=relative,
                post_publish_observed_at=observed_at,
            )
        else:
            raise CampaignPrecommitManifestError(
                "pending campaign precommit authority conflicts with local state"
            )

    latest = _latest_commit_record(history)
    if latest is not None:
        if latest.tx_id.startswith(_WITNESS_TX_PREFIX):
            if local_digest != manifest.manifest_sha256:
                raise CampaignPrecommitManifestError(
                    "committed publication witness does not match local manifest"
                )
            witness = _witness_from_record(
                latest,
                manifest=manifest,
                target_relative_path=relative,
            )
            try:
                authority.recover(
                    observed_state_sha256=manifest.manifest_sha256,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise CampaignPrecommitManifestError(
                    "campaign precommit publication authority is not current"
                ) from exc
            return witness

        _validate_structural_record(
            latest,
            manifest=manifest,
            structural_binding_sha256=structural_binding,
        )
        if local_digest != manifest.manifest_sha256:
            raise CampaignPrecommitManifestError(
                "structural publication authority does not match local manifest"
            )
        try:
            authority.recover(
                observed_state_sha256=manifest.manifest_sha256,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise CampaignPrecommitManifestError(
                "campaign precommit structural authority is not current"
            ) from exc
        observed_at = _publication_now_text()
        if _publication_observed_instant(observed_at) >= _instant(
            manifest.observation_not_before
        ):
            raise CampaignPrecommitManifestError(
                "manifest exists but prospective publication was not witnessed before observation"
            )
        return _commit_publication_witness(
            authority,
            manifest=manifest,
            target_relative_path=relative,
            post_publish_observed_at=observed_at,
        )

    if local_digest is not None:
        raise CampaignPrecommitManifestError(
            "existing manifest without monotonic publication history cannot be retroactively qualified"
        )
    if _publication_deadline_reached(manifest.observation_not_before):
        raise CampaignPrecommitManifestError(
            "first campaign precommit publication must precede prospective observation"
        )

    tx_id = _publication_structural_tx_id()
    try:
        authority.prepare(
            tx_id=tx_id,
            observed_state_sha256=None,
            intended_state_sha256=manifest.manifest_sha256,
            semantic_binding_sha256=structural_binding,
        )
    except MonotonicWorkspaceAuthorityError as exc:
        raise CampaignPrecommitManifestError(
            "cannot prepare campaign precommit publication authority"
        ) from exc

    try:
        write_campaign_precommit_manifest_once(target, manifest)
    except BaseException:
        local_after_failure = _local_manifest_digest(target)
        if local_after_failure is None:
            try:
                authority.abort(
                    tx_id=tx_id,
                    observed_state_sha256=None,
                    semantic_binding_sha256=structural_binding,
                )
            except MonotonicWorkspaceAuthorityError:
                pass
        raise

    loaded = load_campaign_precommit_manifest(target)
    if loaded.manifest_sha256 != manifest.manifest_sha256:
        raise CampaignPrecommitManifestError(
            "published campaign precommit manifest changed before witnessing"
        )
    post_publish_observed_at = _publication_now_text()
    try:
        authority.commit(
            tx_id=tx_id,
            observed_state_sha256=manifest.manifest_sha256,
            semantic_binding_sha256=structural_binding,
        )
    except MonotonicWorkspaceAuthorityError as exc:
        raise CampaignPrecommitManifestError(
            "cannot commit campaign precommit structural publication"
        ) from exc

    if _publication_observed_instant(post_publish_observed_at) >= _instant(
        manifest.observation_not_before
    ):
        raise CampaignPrecommitManifestError(
            "manifest was not product-observed before prospective observation; no publication witness issued"
        )

    return _commit_publication_witness(
        authority,
        manifest=loaded,
        target_relative_path=relative,
        post_publish_observed_at=post_publish_observed_at,
    )