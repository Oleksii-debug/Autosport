from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import stat
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .integrity import atomic_write_json as _path_atomic_write_json
from .workspace_lock import WorkspaceEconomicLock, WorkspaceEconomicLockError


_SCHEMA_VERSION = 1
_KIND = "autosport-workspace-evidence-manifest"
_CHUNK_SIZE = 1024 * 1024
_FIXED_EVIDENCE_NAMES = (
    "decisions.jsonl",
    "paper_book.json",
    "run_registry.json",
    "source_health.json",
)
_MANIFEST_KEYS = {
    "schema_version",
    "kind",
    "file_count",
    "files",
    "expected_fixed_evidence_paths",
    "missing_fixed_evidence_paths",
    "fixed_evidence_set_complete",
    "run_summary_count",
    "file_contents_included",
    "market_database_included",
    "raw_historical_or_provider_bytes_included",
    "environment_or_credential_values_included",
    "arbitrary_workspace_files_included",
    "real_money_execution",
    "manifest_sha256",
}
_FILE_KEYS = {"path", "size_bytes", "sha256"}
_FALSE_TRUTH_FIELDS = (
    "file_contents_included",
    "market_database_included",
    "raw_historical_or_provider_bytes_included",
    "environment_or_credential_values_included",
    "arbitrary_workspace_files_included",
    "real_money_execution",
)
_BOUND_POSIX_OUTPUT: ContextVar[tuple[int, int, Path] | None] = ContextVar(
    "autosport_evidence_bound_posix_output",
    default=None,
)
_BOUND_WINDOWS_OUTPUT: ContextVar[tuple[int, int, Path] | None] = ContextVar(
    "autosport_evidence_bound_windows_output",
    default=None,
)
_RETAINED_SOURCE_SNAPSHOTS: ContextVar[
    list[tuple[Path, int, os.stat_result, os.stat_result, int, str]] | None
] = ContextVar(
    "autosport_evidence_retained_source_snapshots",
    default=None,
)


def _is_canonical_run_summary_name(name: str) -> bool:
    if not name.startswith("run-") or not name.endswith(".json"):
        return False
    raw_id = name[len("run-") : -len(".json")]
    try:
        parsed = uuid.UUID(raw_id)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == raw_id


def _is_canonical_evidence_name(name: str) -> bool:
    return name in _FIXED_EVIDENCE_NAMES or _is_canonical_run_summary_name(name)


def _canonical_source_names(workspace: Path) -> tuple[str, ...]:
    names: set[str] = set()
    for name in _FIXED_EVIDENCE_NAMES:
        if os.path.lexists(workspace / name):
            names.add(name)
    for entry in workspace.iterdir():
        if _is_canonical_run_summary_name(entry.name):
            names.add(entry.name)
    return tuple(sorted(names))


def _stable_stat_metadata(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_mode == right.st_mode
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _read_only_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_BINARY", 0)


def _open_retained_source_descriptor(path: Path) -> int:
    """Open a source descriptor that stays authoritative through snapshot close.

    Windows V1 needs a real common exclusion interval, not another finite recheck
    sweep.  A retained CreateFile handle shares READ only, so once acquired it
    denies later WRITE and DELETE/rename opens until every retained descriptor is
    closed.  Existing incompatible write/delete handles also make acquisition fail
    closed with ERROR_SHARING_VIOLATION.  Non-Windows keeps the existing descriptor
    snapshot semantics under WorkspaceEconomicLock; its external namespace model is
    unchanged by this Windows release hardening.
    """

    if os.name != "nt":
        return os.open(path, _read_only_open_flags())

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    generic_read = 0x80000000
    file_share_read = 0x00000001
    open_existing = 3
    file_attribute_normal = 0x00000080
    file_flag_open_reparse_point = 0x00200000
    invalid_handle_value = ctypes.c_void_p(-1).value

    resolved_path = path.resolve(strict=True)
    kernel_handle = create_file(
        _windows_api_path(resolved_path),
        generic_read,
        file_share_read,
        None,
        open_existing,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    if kernel_handle == invalid_handle_value:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        return msvcrt.open_osfhandle(kernel_handle, _read_only_open_flags())
    except BaseException:
        close_handle(kernel_handle)
        raise


def _path_still_matches_open_file(
    path: Path,
    descriptor: int,
    expected_path_stat: os.stat_result,
) -> bool:
    """Bind one lexical regular-file path to an already-open file handle.

    Windows 3.12+ may obtain pathname stat data through a different filesystem
    API than fstat(), and that path API can omit identity fields such as st_dev.
    Comparing those two stat domains with os.path.samestat() can therefore reject
    an unchanged file. Compare path metadata only with fresh path metadata, and
    use two open handles for file identity instead.
    """

    current = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(current.st_mode):
        return False
    if not _stable_stat_metadata(expected_path_stat, current):
        return False

    verification_descriptor = os.open(path, _read_only_open_flags())
    try:
        if not os.path.sameopenfile(descriptor, verification_descriptor):
            return False
        current_after_open = os.stat(path, follow_symlinks=False)
        return stat.S_ISREG(current_after_open.st_mode) and _stable_stat_metadata(
            expected_path_stat,
            current_after_open,
        )
    finally:
        os.close(verification_descriptor)


def _hash_open_descriptor(descriptor: int) -> tuple[int, str]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = os.read(descriptor, _CHUNK_SIZE)
        if not chunk:
            break
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


def _open_and_hash_regular_file(path: Path) -> tuple[int, str]:
    """Hash one stable regular-file snapshot without following path symlinks."""

    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"canonical evidence path is not a regular file: {path.name}")

    retained_snapshots = _RETAINED_SOURCE_SNAPSHOTS.get()
    if retained_snapshots is None:
        descriptor = os.open(path, _read_only_open_flags())
    else:
        descriptor = _open_retained_source_descriptor(path)
    descriptor_retained = False
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _path_still_matches_open_file(
            path,
            descriptor,
            before,
        ):
            raise ValueError(f"canonical evidence path changed before snapshot: {path.name}")

        size, digest = _hash_open_descriptor(descriptor)
        opened_after = os.fstat(descriptor)
        if not _stable_stat_metadata(opened, opened_after):
            raise ValueError(f"canonical evidence file mutated during snapshot: {path.name}")
        if not _path_still_matches_open_file(path, descriptor, before):
            raise ValueError(f"canonical evidence path changed during snapshot: {path.name}")

        if retained_snapshots is not None:
            retained_snapshots.append(
                (path, descriptor, before, opened, size, digest)
            )
            descriptor_retained = True
        return size, digest
    finally:
        if not descriptor_retained:
            os.close(descriptor)


def _reprove_retained_source_snapshot(
    snapshot: tuple[Path, int, os.stat_result, os.stat_result, int, str],
) -> None:
    path, descriptor, expected_path_stat, expected_open_stat, expected_size, expected_digest = snapshot
    current_before = os.fstat(descriptor)
    if not _stable_stat_metadata(expected_open_stat, current_before):
        raise ValueError(f"canonical evidence file mutated during snapshot: {path.name}")

    size, digest = _hash_open_descriptor(descriptor)
    current_after = os.fstat(descriptor)
    if (
        not _stable_stat_metadata(expected_open_stat, current_after)
        or size != expected_size
        or digest != expected_digest
    ):
        raise ValueError(f"canonical evidence file mutated during snapshot: {path.name}")
    if not _path_still_matches_open_file(path, descriptor, expected_path_stat):
        raise ValueError(f"canonical evidence path changed during snapshot: {path.name}")


def _reprove_retained_source_path(
    snapshot: tuple[Path, int, os.stat_result, os.stat_result, int, str],
) -> None:
    path, descriptor, expected_path_stat, expected_open_stat, _, _ = snapshot
    if not _stable_stat_metadata(expected_open_stat, os.fstat(descriptor)):
        raise ValueError(f"canonical evidence file mutated during snapshot: {path.name}")
    if not _path_still_matches_open_file(path, descriptor, expected_path_stat):
        raise ValueError(f"canonical evidence path changed during snapshot: {path.name}")


def _manifest_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest_file_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _read_descriptor_bytes(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, _CHUNK_SIZE)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _resolved(path: Path, *, strict: bool) -> Path:
    try:
        return path.resolve(strict=strict)
    except OSError as exc:
        raise ValueError(f"cannot resolve path {path}: {exc}") from exc


def _resolve_output_destination(workspace: Path, output: Path) -> Path:
    workspace_root = _resolved(workspace, strict=True)
    output_path = _resolved(output, strict=False)
    try:
        output_path.relative_to(workspace_root)
    except ValueError:
        pass
    else:
        raise ValueError(
            "output path must be outside the Autosport workspace; "
            "must not overwrite canonical workspace evidence"
        )

    # A portable descriptor/handle does not make a directory namespace-immutable.
    # If an arbitrary external parent is accepted, another actor can reparent that
    # already-open directory under WORKSPACE between an ancestry check and the next
    # create/replace syscall. Requiring the resolved output parent to contain the
    # workspace makes that transition structurally impossible: moving an ancestor
    # into its own descendant is rejected by the filesystem as a directory cycle.
    output_parent = output_path.parent
    try:
        workspace_root.relative_to(output_parent)
    except ValueError as exc:
        raise ValueError(
            "output parent must be an ancestor of the Autosport workspace; "
            "safe publication must not use a reparentable sibling directory"
        ) from exc
    return output_path


def _current_caller_visible_destination(
    workspace: Path,
    requested_destination: Path,
    bound_destination: Path,
) -> Path:
    """Re-prove the caller-visible pathname before reporting export PASS."""

    current = _resolved(requested_destination, strict=True)
    _resolve_output_destination(workspace, requested_destination)
    if current != bound_destination:
        raise ValueError("caller-visible evidence output path changed before PASS")
    return current


def _posix_directory_open_flags() -> int:
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    no_follow_flag = getattr(os, "O_NOFOLLOW", 0)
    if not directory_flag or not no_follow_flag:
        raise OSError(
            errno.ENOTSUP,
            "platform lacks no-follow directory descriptor support for evidence export",
        )
    return (
        os.O_RDONLY
        | directory_flag
        | no_follow_flag
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_posix_directory(path: Path) -> int:
    descriptor = os.open(path, _posix_directory_open_flags())
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError(f"output path component is not a directory: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _posix_directory_is_within(candidate_descriptor: int, ancestor_descriptor: int) -> bool:
    if os.open not in os.supports_dir_fd:
        raise OSError(
            errno.ENOTSUP,
            "platform lacks descriptor-relative directory traversal for evidence export",
        )

    current = os.dup(candidate_descriptor)
    try:
        while True:
            if os.path.sameopenfile(current, ancestor_descriptor):
                return True
            parent = os.open("..", _posix_directory_open_flags(), dir_fd=current)
            try:
                if os.path.sameopenfile(current, parent):
                    os.close(parent)
                    return False
            except BaseException:
                os.close(parent)
                raise
            os.close(current)
            current = parent
    finally:
        os.close(current)


def _require_posix_output_parent_outside_workspace(
    parent_descriptor: int,
    workspace_descriptor: int,
) -> None:
    if _posix_directory_is_within(parent_descriptor, workspace_descriptor):
        raise ValueError(
            "bound evidence output parent moved into Autosport workspace before publication"
        )


def _open_or_create_posix_child_directory(parent_descriptor: int, name: str) -> int:
    if os.mkdir not in os.supports_dir_fd:
        raise OSError(
            errno.ENOTSUP,
            "platform lacks descriptor-relative directory creation for evidence export",
        )
    try:
        return os.open(name, _posix_directory_open_flags(), dir_fd=parent_descriptor)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o777, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        return os.open(name, _posix_directory_open_flags(), dir_fd=parent_descriptor)


def _atomic_write_json_at_directory(
    parent_descriptor: int,
    workspace_descriptor: int,
    destination_name: str,
    payload: dict[str, Any],
) -> None:
    if destination_name in {"", ".", ".."} or Path(destination_name).name != destination_name:
        raise ValueError("evidence export destination must name one file")
    if os.open not in os.supports_dir_fd:
        raise OSError(
            errno.ENOTSUP,
            "platform lacks descriptor-relative atomic publication for evidence export",
        )

    temporary_name = f".{destination_name}.{uuid.uuid4().hex}.tmp"
    open_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor: int | None = None
    temporary_exists = False
    try:
        _require_posix_output_parent_outside_workspace(
            parent_descriptor,
            workspace_descriptor,
        )
        descriptor = os.open(
            temporary_name,
            open_flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        temporary_exists = True
        _require_posix_output_parent_outside_workspace(
            parent_descriptor,
            workspace_descriptor,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _require_posix_output_parent_outside_workspace(
            parent_descriptor,
            workspace_descriptor,
        )
        try:
            os.replace(
                temporary_name,
                destination_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
        except NotImplementedError as exc:
            raise OSError(
                errno.ENOTSUP,
                "platform lacks descriptor-relative atomic replace for evidence export",
            ) from exc
        temporary_exists = False
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass


def _windows_api_path(path: Path) -> str:
    text = str(path)
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _windows_final_path_from_handle(handle: int) -> Path:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path_name = kernel32.GetFinalPathNameByHandleW
    get_final_path_name.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_final_path_name.restype = wintypes.DWORD

    buffer = ctypes.create_unicode_buffer(32768)
    length = get_final_path_name(handle, buffer, len(buffer), 0)
    if length == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    if length >= len(buffer):
        raise OSError(errno.ENAMETOOLONG, "Windows evidence directory path is too long")

    text = buffer.value
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[len("\\\\?\\UNC\\") :]
    elif text.startswith("\\\\?\\"):
        text = text[len("\\\\?\\") :]
    return Path(text)


def _windows_directory_is_within(candidate_handle: int, ancestor_handle: int) -> bool:
    candidate = os.path.normcase(os.path.normpath(str(_windows_final_path_from_handle(candidate_handle))))
    ancestor = os.path.normcase(os.path.normpath(str(_windows_final_path_from_handle(ancestor_handle))))
    try:
        return os.path.commonpath([candidate, ancestor]) == ancestor
    except ValueError:
        return False


def _require_windows_output_parent_outside_workspace(
    parent_handle: int,
    workspace_handle: int,
) -> None:
    if _windows_directory_is_within(parent_handle, workspace_handle):
        raise ValueError(
            "bound evidence output parent moved into Autosport workspace before publication"
        )


def _atomic_write_json_at_windows_directory(
    parent_handle: int,
    workspace_handle: int,
    destination_name: str,
    payload: dict[str, Any],
) -> None:
    import ctypes
    from ctypes import wintypes

    if destination_name in {"", ".", ".."} or Path(destination_name).name != destination_name:
        raise ValueError("evidence export destination must name one file")

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

    name_length = len(destination_name)

    class FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("ReplaceOrFlags", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * (name_length + 1)),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
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
        wintypes.ULONG,
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

    def relative_name(name: str) -> tuple[object, UnicodeString, ObjectAttributes]:
        buffer = ctypes.create_unicode_buffer(name)
        encoded_length = len(name.encode("utf-16-le"))
        unicode_name = UnicodeString(
            encoded_length,
            encoded_length + 2,
            ctypes.cast(buffer, wintypes.LPWSTR),
        )
        attributes = ObjectAttributes(
            ctypes.sizeof(ObjectAttributes),
            parent_handle,
            ctypes.pointer(unicode_name),
            obj_case_insensitive,
            None,
            None,
        )
        return buffer, unicode_name, attributes

    temporary_name = f".{destination_name}.{uuid.uuid4().hex}.tmp"
    buffer, unicode_name, object_attributes = relative_name(temporary_name)
    del buffer, unicode_name
    io_status = IoStatusBlock()
    temporary_handle = wintypes.HANDLE()
    _require_windows_output_parent_outside_workspace(parent_handle, workspace_handle)
    status = nt_create_file(
        ctypes.byref(temporary_handle),
        file_write_data | delete_access | synchronize,
        ctypes.byref(object_attributes),
        ctypes.byref(io_status),
        None,
        file_attribute_normal,
        file_share_read | file_share_write | file_share_delete,
        file_create,
        file_synchronous_io_nonalert | file_non_directory_file | file_open_reparse_point,
        None,
        0,
    )
    if status < 0:
        raise ctypes.WinError(int(rtl_status_to_dos_error(status)))

    primary_error: BaseException | None = None
    renamed = False
    try:
        _require_windows_output_parent_outside_workspace(parent_handle, workspace_handle)
        encoded = _manifest_file_bytes(payload)
        offset = 0
        while offset < len(encoded):
            chunk = encoded[offset : offset + _CHUNK_SIZE]
            chunk_buffer = ctypes.create_string_buffer(chunk)
            written = wintypes.DWORD()
            if not write_file(
                temporary_handle,
                chunk_buffer,
                len(chunk),
                ctypes.byref(written),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if written.value <= 0:
                raise OSError(errno.EIO, "Windows evidence publication wrote zero bytes")
            offset += int(written.value)

        if not flush_file_buffers(temporary_handle):
            raise ctypes.WinError(ctypes.get_last_error())

        _require_windows_output_parent_outside_workspace(parent_handle, workspace_handle)
        rename_info = FileRenameInfo()
        rename_info.ReplaceOrFlags = 1
        rename_info.RootDirectory = parent_handle
        rename_info.FileNameLength = len(destination_name.encode("utf-16-le"))
        rename_info.FileName = destination_name
        rename_io_status = IoStatusBlock()
        rename_status = nt_set_information_file(
            temporary_handle,
            ctypes.byref(rename_io_status),
            ctypes.byref(rename_info),
            ctypes.sizeof(rename_info),
            file_rename_information_class,
        )
        if rename_status < 0:
            raise ctypes.WinError(int(rtl_status_to_dos_error(rename_status)))
        renamed = True
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: BaseException | None = None
        if not renamed:
            disposition = FileDispositionInfo(1)
            if not set_file_information(
                temporary_handle,
                file_disposition_info_class,
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            ):
                cleanup_error = ctypes.WinError(ctypes.get_last_error())
        if not close_handle(temporary_handle) and cleanup_error is None:
            cleanup_error = ctypes.WinError(ctypes.get_last_error())
        if primary_error is not None and cleanup_error is not None:
            try:
                primary_error.add_note(f"temporary evidence cleanup also failed: {cleanup_error}")
            except BaseException:
                pass
        elif primary_error is None and cleanup_error is not None:
            raise cleanup_error


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> None:
    """Preserve the canonical writer seam while honoring a bound output parent."""

    destination = Path(path)
    posix_binding = _BOUND_POSIX_OUTPUT.get()
    if posix_binding is not None:
        parent_descriptor, workspace_descriptor, expected_destination = posix_binding
        if destination != expected_destination:
            raise RuntimeError("bound evidence output destination changed before publication")
        _atomic_write_json_at_directory(
            parent_descriptor,
            workspace_descriptor,
            destination.name,
            payload,
        )
        return

    windows_binding = _BOUND_WINDOWS_OUTPUT.get()
    if windows_binding is not None:
        parent_handle, workspace_handle, expected_destination = windows_binding
        if destination != expected_destination:
            raise RuntimeError("bound evidence output destination changed before publication")
        _atomic_write_json_at_windows_directory(
            parent_handle,
            workspace_handle,
            destination.name,
            payload,
        )
        return

    _path_atomic_write_json(destination, payload)


def _publish_posix_bound_output(
    workspace: Path,
    requested_destination: Path,
    destination: Path,
    payload: dict[str, Any],
) -> None:
    workspace_descriptor = _open_posix_directory(_resolved(workspace, strict=True))
    current_descriptor: int | None = None
    token = None
    try:
        parent = destination.parent
        if not parent.is_absolute() or not parent.anchor:
            raise ValueError("evidence export destination must resolve to an absolute path")
        current_descriptor = _open_posix_directory(Path(parent.anchor))
        for component in parent.parts[1:]:
            if _posix_directory_is_within(current_descriptor, workspace_descriptor):
                raise ValueError(
                    "output path must be outside the Autosport workspace; "
                    "must not overwrite canonical workspace evidence"
                )
            next_descriptor = _open_or_create_posix_child_directory(
                current_descriptor,
                component,
            )
            os.close(current_descriptor)
            current_descriptor = next_descriptor

        if _posix_directory_is_within(current_descriptor, workspace_descriptor):
            raise ValueError(
                "output path must be outside the Autosport workspace; "
                "must not overwrite canonical workspace evidence"
            )

        token = _BOUND_POSIX_OUTPUT.set(
            (current_descriptor, workspace_descriptor, destination)
        )
        atomic_write_json(destination, payload)

        caller_destination = _current_caller_visible_destination(
            workspace,
            requested_destination,
            destination,
        )
        caller_parent_descriptor = _open_posix_directory(caller_destination.parent)
        bound_file_descriptor: int | None = None
        caller_file_descriptor: int | None = None
        try:
            if not os.path.sameopenfile(current_descriptor, caller_parent_descriptor):
                raise ValueError("caller-visible evidence output parent changed before PASS")
            no_follow_flag = getattr(os, "O_NOFOLLOW", 0)
            if not no_follow_flag:
                raise OSError(
                    errno.ENOTSUP,
                    "platform lacks no-follow file verification for evidence export",
                )
            bound_file_descriptor = os.open(
                destination.name,
                _read_only_open_flags() | no_follow_flag,
                dir_fd=current_descriptor,
            )
            caller_file_descriptor = os.open(
                caller_destination,
                _read_only_open_flags() | no_follow_flag,
            )
            if not os.path.sameopenfile(bound_file_descriptor, caller_file_descriptor):
                raise ValueError("caller-visible evidence output file changed before PASS")
            if not stat.S_ISREG(os.fstat(caller_file_descriptor).st_mode):
                raise ValueError("caller-visible evidence output is not a regular file")
            if _read_descriptor_bytes(caller_file_descriptor) != _manifest_file_bytes(payload):
                raise ValueError("caller-visible evidence output content changed before PASS")
        finally:
            if caller_file_descriptor is not None:
                os.close(caller_file_descriptor)
            if bound_file_descriptor is not None:
                os.close(bound_file_descriptor)
            os.close(caller_parent_descriptor)
    finally:
        if token is not None:
            _BOUND_POSIX_OUTPUT.reset(token)
        if current_descriptor is not None:
            os.close(current_descriptor)
        os.close(workspace_descriptor)


def _publish_windows_bound_output(
    workspace: Path,
    requested_destination: Path,
    destination: Path,
    payload: dict[str, Any],
) -> None:
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
    file_open_if = 3
    file_attribute_directory = 0x00000010
    file_attribute_reparse_point = 0x00000400
    file_flag_open_reparse_point = 0x00200000
    file_flag_backup_semantics = 0x02000000
    file_directory_file = 0x00000001
    file_synchronous_io_nonalert = 0x00000020
    file_open_reparse_point = 0x00200000
    obj_case_insensitive = 0x00000040
    invalid_handle_value = ctypes.c_void_p(-1).value
    directory_access = file_list_directory | file_traverse | file_read_attributes | synchronize
    share_all = file_share_read | file_share_write | file_share_delete

    def directory_identity(handle: int) -> tuple[int, int, int]:
        information = ByHandleFileInformation()
        if not get_file_information(handle, ctypes.byref(information)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not information.dwFileAttributes & file_attribute_directory:
            raise ValueError("output path component is not a directory")
        if information.dwFileAttributes & file_attribute_reparse_point:
            raise ValueError("output directory ancestry contains a reparse point")
        return (
            int(information.dwVolumeSerialNumber),
            int(information.nFileIndexHigh),
            int(information.nFileIndexLow),
        )

    def open_directory_path(path: Path) -> tuple[int, tuple[int, int, int]]:
        handle = create_file(
            _windows_api_path(path),
            directory_access,
            share_all,
            None,
            open_existing,
            file_flag_backup_semantics | file_flag_open_reparse_point,
            None,
        )
        if handle == invalid_handle_value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return int(handle), directory_identity(handle)
        except BaseException:
            close_handle(handle)
            raise

    def open_or_create_child_directory(
        parent_handle: int,
        name: str,
    ) -> tuple[int, tuple[int, int, int]]:
        name_buffer = ctypes.create_unicode_buffer(name)
        name_bytes = len(name.encode("utf-16-le"))
        unicode_name = UnicodeString(
            name_bytes,
            name_bytes + 2,
            ctypes.cast(name_buffer, wintypes.LPWSTR),
        )
        object_attributes = ObjectAttributes(
            ctypes.sizeof(ObjectAttributes),
            parent_handle,
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
            ctypes.byref(object_attributes),
            ctypes.byref(io_status),
            None,
            file_attribute_directory,
            share_all,
            file_open_if,
            file_directory_file | file_synchronous_io_nonalert | file_open_reparse_point,
            None,
            0,
        )
        if status < 0:
            raise ctypes.WinError(int(rtl_status_to_dos_error(status)))
        child_value = int(child.value)
        try:
            return child_value, directory_identity(child_value)
        except BaseException:
            close_handle(child_value)
            raise

    workspace_handle, workspace_identity = open_directory_path(_resolved(workspace, strict=True))
    current_handle: int | None = None
    current_identity: tuple[int, int, int] | None = None
    token = None
    primary_error: BaseException | None = None
    try:
        parent = destination.parent
        if not parent.is_absolute() or not parent.anchor:
            raise ValueError("evidence export destination must resolve to an absolute path")

        current_handle, current_identity = open_directory_path(Path(parent.anchor))
        if current_identity == workspace_identity:
            raise ValueError(
                "output path must be outside the Autosport workspace; "
                "must not overwrite canonical workspace evidence"
            )

        for component in parent.parts[1:]:
            next_handle, next_identity = open_or_create_child_directory(
                current_handle,
                component,
            )
            if not close_handle(current_handle):
                close_handle(next_handle)
                raise ctypes.WinError(ctypes.get_last_error())
            current_handle = next_handle
            current_identity = next_identity
            if next_identity == workspace_identity:
                raise ValueError(
                    "output path must be outside the Autosport workspace; "
                    "must not overwrite canonical workspace evidence"
                )

        _require_windows_output_parent_outside_workspace(current_handle, workspace_handle)
        token = _BOUND_WINDOWS_OUTPUT.set(
            (current_handle, workspace_handle, destination)
        )
        atomic_write_json(destination, payload)

        caller_destination = _current_caller_visible_destination(
            workspace,
            requested_destination,
            destination,
        )
        caller_parent_handle: int | None = None
        caller_descriptor: int | None = None
        try:
            caller_parent_handle, caller_parent_identity = open_directory_path(
                caller_destination.parent
            )
            if caller_parent_identity != current_identity:
                raise ValueError("caller-visible evidence output parent changed before PASS")
            caller_descriptor = os.open(caller_destination, _read_only_open_flags())
            if not stat.S_ISREG(os.fstat(caller_descriptor).st_mode):
                raise ValueError("caller-visible evidence output is not a regular file")
            if _read_descriptor_bytes(caller_descriptor) != _manifest_file_bytes(payload):
                raise ValueError("caller-visible evidence output content changed before PASS")
        finally:
            if caller_descriptor is not None:
                os.close(caller_descriptor)
            if caller_parent_handle is not None and not close_handle(caller_parent_handle):
                raise ctypes.WinError(ctypes.get_last_error())
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if token is not None:
            _BOUND_WINDOWS_OUTPUT.reset(token)
        cleanup_error: BaseException | None = None
        if current_handle is not None and not close_handle(current_handle):
            cleanup_error = ctypes.WinError(ctypes.get_last_error())
        if not close_handle(workspace_handle) and cleanup_error is None:
            cleanup_error = ctypes.WinError(ctypes.get_last_error())
        if primary_error is not None and cleanup_error is not None:
            try:
                primary_error.add_note(f"evidence directory handle cleanup also failed: {cleanup_error}")
            except BaseException:
                pass
        elif primary_error is None and cleanup_error is not None:
            raise cleanup_error


def _publish_bound_output(
    workspace: Path,
    requested_destination: Path,
    payload: dict[str, Any],
) -> Path:
    destination = _resolve_output_destination(workspace, requested_destination)
    if os.name == "nt":
        _publish_windows_bound_output(
            workspace,
            requested_destination,
            destination,
            payload,
        )
    else:
        _publish_posix_bound_output(
            workspace,
            requested_destination,
            destination,
            payload,
        )
    return destination


def _reject_duplicate_manifest_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("evidence manifest contains duplicate JSON object keys")
        result[key] = value
    return result


def _reject_manifest_constant(value: str) -> None:
    raise ValueError("evidence manifest contains non-standard JSON constants")


def _is_sha256_text(value: object) -> bool:
    if type(value) is not str or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _validate_manifest_payload(raw: object) -> dict[str, Any]:
    if type(raw) is not dict:
        raise ValueError("evidence manifest root must be a JSON object")
    if set(raw) != _MANIFEST_KEYS:
        raise ValueError("evidence manifest fields do not match schema version 1")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("evidence manifest schema_version must be exact integer 1")
    if raw["kind"] != _KIND or type(raw["kind"]) is not str:
        raise ValueError("evidence manifest kind is invalid")
    if type(raw["file_count"]) is not int or raw["file_count"] <= 0:
        raise ValueError("evidence manifest file_count must be a positive integer")
    if type(raw["run_summary_count"]) is not int or raw["run_summary_count"] < 0:
        raise ValueError("evidence manifest run_summary_count must be a non-negative integer")
    if type(raw["files"]) is not list:
        raise ValueError("evidence manifest files must be a JSON array")
    if raw["expected_fixed_evidence_paths"] != list(_FIXED_EVIDENCE_NAMES):
        raise ValueError("evidence manifest expected fixed paths are invalid")
    if type(raw["missing_fixed_evidence_paths"]) is not list:
        raise ValueError("evidence manifest missing fixed paths must be a JSON array")
    if type(raw["fixed_evidence_set_complete"]) is not bool:
        raise ValueError("evidence manifest fixed completeness flag must be boolean")
    for field in _FALSE_TRUTH_FIELDS:
        if raw[field] is not False:
            raise ValueError(f"evidence manifest truth field must be false: {field}")
    if not _is_sha256_text(raw["manifest_sha256"]):
        raise ValueError("evidence manifest manifest_sha256 is invalid")

    files = raw["files"]
    if len(files) != raw["file_count"]:
        raise ValueError("evidence manifest file_count does not match files")

    paths: list[str] = []
    for item in files:
        if type(item) is not dict or set(item) != _FILE_KEYS:
            raise ValueError("evidence manifest file record is invalid")
        path = item["path"]
        if type(path) is not str or not _is_canonical_evidence_name(path):
            raise ValueError("evidence manifest contains a noncanonical evidence path")
        if type(item["size_bytes"]) is not int or item["size_bytes"] < 0:
            raise ValueError("evidence manifest file size must be a non-negative integer")
        if not _is_sha256_text(item["sha256"]):
            raise ValueError("evidence manifest file SHA-256 is invalid")
        paths.append(path)

    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValueError("evidence manifest file paths must be unique and sorted")

    missing_fixed = [name for name in _FIXED_EVIDENCE_NAMES if name not in paths]
    if raw["missing_fixed_evidence_paths"] != missing_fixed:
        raise ValueError("evidence manifest missing fixed paths do not match files")
    if raw["fixed_evidence_set_complete"] is not (not missing_fixed):
        raise ValueError("evidence manifest fixed completeness flag does not match files")
    run_summary_count = sum(_is_canonical_run_summary_name(name) for name in paths)
    if raw["run_summary_count"] != run_summary_count:
        raise ValueError("evidence manifest run_summary_count does not match files")

    payload_without_hash = dict(raw)
    manifest_digest = payload_without_hash.pop("manifest_sha256")
    if _manifest_sha256(payload_without_hash) != manifest_digest:
        raise ValueError("evidence manifest manifest_sha256 does not match payload")
    return raw


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8")
        raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_manifest_keys,
            parse_constant=_reject_manifest_constant,
        )
    except UnicodeDecodeError as exc:
        raise ValueError("evidence manifest is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("evidence manifest is not valid JSON") from exc
    except RecursionError as exc:
        raise ValueError("evidence manifest JSON nesting is too deep") from exc
    return _validate_manifest_payload(raw)


def export_evidence_manifest(workspace: str | Path, output: str | Path) -> dict[str, Any]:
    """Publish a deterministic metadata-only manifest for canonical workspace evidence.

    The export intentionally contains no workspace file contents, market database bytes,
    raw provider/history bytes, environment values, credentials, cookies, tokens or
    arbitrary workspace files. It records only canonical relative evidence names plus
    byte counts and SHA-256 digests.
    """

    root = Path(workspace)
    requested_destination = Path(output)
    if not root.exists() or not root.is_dir():
        raise ValueError("workspace must be an existing directory")
    _resolve_output_destination(root, requested_destination)

    # Refuse an empty/non-evidence directory before taking the economic lock so an
    # export attempt does not create lock metadata in an unrelated empty directory.
    if not _canonical_source_names(root):
        raise ValueError("workspace contains no canonical exportable evidence")

    # Keep the exclusive critical section limited to source discovery + hashing and
    # close-boundary reproof. Publication remains outside this economic lock.
    with WorkspaceEconomicLock(root):
        names = _canonical_source_names(root)
        if not names:
            raise ValueError("workspace canonical evidence disappeared before snapshot")

        retained_snapshots: list[
            tuple[Path, int, os.stat_result, os.stat_result, int, str]
        ] = []
        retention_token = _RETAINED_SOURCE_SNAPSHOTS.set(retained_snapshots)
        primary_error: BaseException | None = None
        try:
            files: list[dict[str, Any]] = []
            for name in names:
                size, digest = _open_and_hash_regular_file(root / name)
                files.append(
                    {
                        "path": name,
                        "size_bytes": size,
                        "sha256": digest,
                    }
                )

            missing_fixed = [name for name in _FIXED_EVIDENCE_NAMES if name not in names]
            run_summary_count = sum(_is_canonical_run_summary_name(name) for name in names)
            payload: dict[str, Any] = {
                "schema_version": _SCHEMA_VERSION,
                "kind": _KIND,
                "file_count": len(files),
                "files": files,
                "expected_fixed_evidence_paths": list(_FIXED_EVIDENCE_NAMES),
                "missing_fixed_evidence_paths": missing_fixed,
                "fixed_evidence_set_complete": not missing_fixed,
                "run_summary_count": run_summary_count,
                "file_contents_included": False,
                "market_database_included": False,
                "raw_historical_or_provider_bytes_included": False,
                "environment_or_credential_values_included": False,
                "arbitrary_workspace_files_included": False,
                "real_money_execution": False,
            }
            payload["manifest_sha256"] = _manifest_sha256(payload)

            # Re-read every originally hashed descriptor before snapshot close so
            # mutation of an earlier member while a later member is being hashed
            # cannot leave a stale digest in an otherwise unchanged name set.
            for snapshot in retained_snapshots:
                _reprove_retained_source_snapshot(snapshot)

            if _canonical_source_names(root) != names:
                raise ValueError("workspace canonical evidence set changed during snapshot")

            # On Windows each retained source handle denies WRITE and DELETE until
            # all descriptors close, making this final path reproof occur inside one
            # actual exclusion interval instead of relying on a finite sweep alone.
            for snapshot in retained_snapshots:
                _reprove_retained_source_path(snapshot)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            _RETAINED_SOURCE_SNAPSHOTS.reset(retention_token)
            cleanup_error: BaseException | None = None
            for snapshot in retained_snapshots:
                try:
                    os.close(snapshot[1])
                except OSError as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
            if primary_error is not None and cleanup_error is not None:
                try:
                    primary_error.add_note(
                        f"retained evidence descriptor cleanup also failed: {cleanup_error}"
                    )
                except BaseException:
                    pass
            elif primary_error is None and cleanup_error is not None:
                raise cleanup_error

    # Publication remains outside WorkspaceEconomicLock. Its resolved parent is a
    # workspace ancestor, so it cannot be reparented into that workspace descendant;
    # descriptor/handle-relative writes and caller-visible reproof remain defense in depth.
    _publish_bound_output(root, requested_destination, payload)
    return payload


def verify_evidence_manifest(manifest: str | Path, workspace: str | Path) -> dict[str, Any]:
    """Fail closed unless one manifest exactly matches current canonical workspace evidence."""

    manifest_path = Path(manifest)
    root = Path(workspace)
    payload = _load_manifest(manifest_path)
    if not root.exists() or not root.is_dir():
        raise ValueError("workspace must be an existing directory")

    expected_files = {item["path"]: item for item in payload["files"]}
    expected_names = tuple(expected_files)
    with WorkspaceEconomicLock(root):
        current_names = _canonical_source_names(root)
        if current_names != expected_names:
            raise ValueError("workspace canonical evidence set does not match manifest")
        for name in current_names:
            size, digest = _open_and_hash_regular_file(root / name)
            expected = expected_files[name]
            if size != expected["size_bytes"] or digest != expected["sha256"]:
                raise ValueError(f"workspace evidence does not match manifest: {name}")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autosport-export-evidence",
        description="Export a deterministic metadata-only Autosport workspace evidence manifest",
    )
    parser.add_argument("workspace", type=Path, help="existing Autosport workspace")
    parser.add_argument("--output", type=Path, required=True, help="destination JSON manifest")
    return parser


def build_verify_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autosport-verify-evidence",
        description="Verify an Autosport evidence manifest against current workspace evidence",
    )
    parser.add_argument("manifest", type=Path, help="evidence manifest JSON")
    parser.add_argument("--workspace", type=Path, required=True, help="existing Autosport workspace")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = export_evidence_manifest(args.workspace, args.output)
    except (OSError, ValueError, WorkspaceEconomicLockError) as exc:
        print(f"evidence_export=FAIL_CLOSED error={exc}")
        return 3

    print(
        f"evidence_export=PASS files={report['file_count']} "
        f"run_summaries={report['run_summary_count']} "
        f"fixed_evidence_set_complete={str(report['fixed_evidence_set_complete']).lower()} "
        f"manifest_sha256={report['manifest_sha256']}"
    )
    print(
        "file_contents_included=false market_database_included=false "
        "raw_historical_or_provider_bytes_included=false "
        "environment_or_credential_values_included=false real_money_execution=false"
    )
    print(f"output={args.output}")
    return 0


def verify_main(argv: list[str] | None = None) -> int:
    args = build_verify_parser().parse_args(argv)
    try:
        report = verify_evidence_manifest(args.manifest, args.workspace)
    except (OSError, ValueError, WorkspaceEconomicLockError) as exc:
        print(f"evidence_verify=FAIL_CLOSED error={exc}")
        return 3

    print(
        f"evidence_verify=PASS workspace_match=true files={report['file_count']} "
        f"run_summaries={report['run_summary_count']} "
        f"fixed_evidence_set_complete={str(report['fixed_evidence_set_complete']).lower()} "
        f"manifest_sha256={report['manifest_sha256']} real_money_execution=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
