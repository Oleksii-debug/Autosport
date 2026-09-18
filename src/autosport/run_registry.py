from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path

from .integrity import atomic_write_json, sha256_file
from .run_transaction import RunTransactionError, _require_portable_run_id
from .workspace_lock import (
    WorkspaceEconomicLock,
    WorkspaceEconomicLockBusyError,
    _open_read_only_descriptor,
    _stable_stat_metadata,
)
from .outcome_trust import (
    OutcomeLineageBinding,
    OutcomeLineageTrustError,
    assert_compatible_outcome_lineages,
    outcome_lineage_binding_from_payload,
    outcome_lineage_payload,
)


_HEX_DIGITS = frozenset("0123456789abcdef")
_ALLOWED_STATUSES = frozenset({"in_progress", "completed", "aborted"})
_REQUIRED_ENTRY_FIELDS = frozenset(
    {
        "base_identity",
        "run_id",
        "market_sha256",
        "results_sha256",
        "strategy_id",
        "status",
    }
)
_OPTIONAL_ENTRY_FIELDS = frozenset(
    {
        "base_paper_book_sha256",
        "base_decision_ledger_sha256",
        "result_path",
        "paper_book_sha256",
        "decision_ledger_sha256",
        "abort_reason",
        "reconciled_from_summary",
        "outcome_lineage",
    }
)
_HASH_EVIDENCE_FIELDS = (
    "base_paper_book_sha256",
    "base_decision_ledger_sha256",
    "paper_book_sha256",
    "decision_ledger_sha256",
)
_FINAL_ONLY_FIELDS = frozenset(
    {
        "result_path",
        "paper_book_sha256",
        "decision_ledger_sha256",
        "abort_reason",
        "reconciled_from_summary",
    }
)
_FIRST_OPEN_RETRY_SECONDS = 0.01
_FIRST_OPEN_MAX_WAIT_SECONDS = 5.0
_LEGACY_SCHEMA_VERSION = 1
_LINEAGE_TRUST_SCHEMA_VERSION = 2
_LINEAGE_TRUST_FIELD = "outcome_lineage_trust"


def _is_canonical_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX_DIGITS for character in value)
    )


def _require_canonical_sha256(name: str, value: object) -> str:
    if not _is_canonical_sha256(value):
        raise ValueError(f"{name} must be a canonical SHA-256 hex string")
    return value


def _require_nonempty_string(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _portable_basename(value: str) -> str:
    """Return a durable path basename independent of POSIX/Windows separators."""
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _path_matches_open_descriptor(
    path: Path,
    descriptor: int,
    expected_path_stat: os.stat_result,
) -> bool:
    """Prove the current no-follow pathname still names the already-open descriptor."""

    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or not _stable_stat_metadata(expected_path_stat, current)
    ):
        return False

    try:
        verification_descriptor = _open_read_only_descriptor(path)
    except OSError:
        return False
    matched = False
    try:
        try:
            same_file = os.path.sameopenfile(descriptor, verification_descriptor)
            current_after_open = os.stat(path, follow_symlinks=False)
        except OSError:
            return False
        matched = (
            same_file
            and stat.S_ISREG(current_after_open.st_mode)
            and current_after_open.st_nlink == 1
            and _stable_stat_metadata(expected_path_stat, current_after_open)
        )
    finally:
        try:
            os.close(verification_descriptor)
        except OSError:
            return False
    return matched



def _require_single_path_component(value: str, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError(f"{label} is not a safe path component")
    return value


def _read_stable_regular_file_bytes(path: Path, *, label: str) -> bytes:
    """Capture one exact regular-file snapshot without following the final alias.

    The descriptor/path identity must remain stable for the duration of the captured
    read. Later pathname mutation is observed by the next reopen; downstream callers
    consume only these captured bytes and never re-read the path for the same decision.
    """

    path_before = _lstat_or_none(path)
    if path_before is None:
        raise FileNotFoundError(path)
    if not stat.S_ISREG(path_before.st_mode) or path_before.st_nlink != 1:
        raise ValueError(f"{label} is not a regular non-aliased file")

    try:
        descriptor = _open_read_only_descriptor(path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError(f"{label} is unreadable") from exc

    primary_error: BaseException | None = None
    try:
        try:
            opened_before = os.fstat(descriptor)
        except OSError as exc:
            raise ValueError(f"{label} changed while validating") from exc
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or opened_before.st_nlink != 1
            or not _path_matches_open_descriptor(path, descriptor, path_before)
        ):
            raise ValueError(f"{label} changed while validating")

        chunks: list[bytes] = []
        try:
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            opened_after = os.fstat(descriptor)
        except OSError as exc:
            raise ValueError(f"{label} changed or became unreadable while validating") from exc

        if (
            not stat.S_ISREG(opened_after.st_mode)
            or opened_after.st_nlink != 1
            or not _stable_stat_metadata(opened_before, opened_after)
            or not _path_matches_open_descriptor(path, descriptor, path_before)
        ):
            raise ValueError(f"{label} changed while validating")
        return b"".join(chunks)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError as close_error:
            if primary_error is None:
                raise ValueError(f"{label} descriptor cleanup failed") from close_error
            try:
                primary_error.add_note(
                    f"{label} descriptor cleanup also failed: "
                    f"{type(close_error).__name__}: {close_error}"
                )
            except BaseException:
                pass


def _read_posix_nested_regular_file_bytes(
    root: Path,
    components: tuple[str, ...],
    *,
    label: str,
) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not no_follow or not directory_flag:
        raise ValueError(f"{label} cannot be verified on this platform")

    directory_descriptors: list[int] = []
    leaf_descriptor: int | None = None
    try:
        try:
            current = os.open(root, os.O_RDONLY | directory_flag | no_follow)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ValueError(f"{label} parent namespace is unsafe or unreadable") from exc
        directory_descriptors.append(current)

        for component in components[:-1]:
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | directory_flag | no_follow,
                    dir_fd=current,
                )
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise ValueError(f"{label} parent namespace is unsafe or unreadable") from exc
            try:
                child_stat = os.fstat(child)
            except OSError as exc:
                os.close(child)
                raise ValueError(f"{label} parent namespace changed while validating") from exc
            if not stat.S_ISDIR(child_stat.st_mode):
                os.close(child)
                raise ValueError(f"{label} parent namespace is not a directory")
            directory_descriptors.append(child)
            current = child

        try:
            leaf_descriptor = os.open(
                components[-1],
                os.O_RDONLY | getattr(os, "O_BINARY", 0) | no_follow,
                dir_fd=current,
            )
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise ValueError(f"{label} is unsafe or unreadable") from exc

        try:
            opened_before = os.fstat(leaf_descriptor)
        except OSError as exc:
            raise ValueError(f"{label} changed while validating") from exc
        if not stat.S_ISREG(opened_before.st_mode) or opened_before.st_nlink != 1:
            raise ValueError(f"{label} is not a regular non-aliased file")

        chunks: list[bytes] = []
        try:
            while True:
                chunk = os.read(leaf_descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            opened_after = os.fstat(leaf_descriptor)
        except OSError as exc:
            raise ValueError(f"{label} changed or became unreadable while validating") from exc
        if (
            not stat.S_ISREG(opened_after.st_mode)
            or opened_after.st_nlink != 1
            or not _stable_stat_metadata(opened_before, opened_after)
        ):
            raise ValueError(f"{label} changed while validating")

        verification_descriptors: list[int] = []
        verification_leaf: int | None = None
        try:
            verification_parent = directory_descriptors[0]
            for index, component in enumerate(components[:-1], start=1):
                try:
                    verification = os.open(
                        component,
                        os.O_RDONLY | directory_flag | no_follow,
                        dir_fd=verification_parent,
                    )
                except OSError as exc:
                    raise ValueError(
                        f"{label} canonical parent namespace changed while validating"
                    ) from exc
                verification_descriptors.append(verification)
                try:
                    verification_stat = os.fstat(verification)
                    same_directory = os.path.sameopenfile(
                        directory_descriptors[index],
                        verification,
                    )
                except OSError as exc:
                    raise ValueError(
                        f"{label} canonical parent namespace changed while validating"
                    ) from exc
                if not stat.S_ISDIR(verification_stat.st_mode) or not same_directory:
                    raise ValueError(
                        f"{label} canonical parent namespace changed while validating"
                    )
                verification_parent = verification

            try:
                verification_leaf = os.open(
                    components[-1],
                    os.O_RDONLY | getattr(os, "O_BINARY", 0) | no_follow,
                    dir_fd=verification_parent,
                )
                same_leaf = os.path.sameopenfile(leaf_descriptor, verification_leaf)
            except OSError as exc:
                raise ValueError(f"{label} canonical leaf changed while validating") from exc
            if not same_leaf:
                raise ValueError(f"{label} canonical leaf changed while validating")
        finally:
            if verification_leaf is not None:
                try:
                    os.close(verification_leaf)
                except OSError:
                    pass
            for verification in reversed(verification_descriptors):
                try:
                    os.close(verification)
                except OSError:
                    pass
        return b"".join(chunks)
    finally:
        if leaf_descriptor is not None:
            try:
                os.close(leaf_descriptor)
            except OSError:
                pass
        for descriptor in reversed(directory_descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _read_windows_nested_regular_file_bytes(
    root: Path,
    components: tuple[str, ...],
    *,
    label: str,
) -> bytes:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", ctypes.c_void_p),
            ("SecurityQualityOfService", ctypes.c_void_p),
        ]

    class _IoStatusBlockUnion(ctypes.Union):
        _fields_ = [("Status", wintypes.LONG), ("Pointer", ctypes.c_void_p)]

    class _IoStatusBlock(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("u", _IoStatusBlockUnion), ("Information", ctypes.c_size_t)]

    class _ByHandleFileInformation(ctypes.Structure):
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
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

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

    get_file_information = kernel32.GetFileInformationByHandle
    get_file_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    get_file_information.restype = wintypes.BOOL

    nt_open_file = ntdll.NtOpenFile
    nt_open_file.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        wintypes.ULONG,
        wintypes.ULONG,
    )
    nt_open_file.restype = wintypes.LONG

    rtl_status_to_dos_error = ntdll.RtlNtStatusToDosError
    rtl_status_to_dos_error.argtypes = (wintypes.LONG,)
    rtl_status_to_dos_error.restype = wintypes.ULONG

    generic_read = 0x80000000
    file_list_directory = 0x0001
    file_read_data = 0x0001
    file_read_attributes = 0x0080
    synchronize = 0x00100000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    file_attribute_directory = 0x00000010
    file_attribute_reparse_point = 0x00000400
    file_directory_file = 0x00000001
    file_non_directory_file = 0x00000040
    file_synchronous_io_nonalert = 0x00000020
    file_open_reparse_point = 0x00200000
    obj_case_insensitive = 0x00000040
    invalid_handle_value = ctypes.c_void_p(-1).value

    def _information(handle: int) -> _ByHandleFileInformation:
        info = _ByHandleFileInformation()
        if not get_file_information(wintypes.HANDLE(handle), ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return info

    def _file_identity(handle: int) -> tuple[int, int, int]:
        info = _information(handle)
        return (
            int(info.dwVolumeSerialNumber),
            int(info.nFileIndexHigh),
            int(info.nFileIndexLow),
        )

    def _open_relative(parent_handle: int, component: str, *, directory: bool) -> int:
        encoded = component.encode("utf-16-le")
        buffer = ctypes.create_unicode_buffer(component)
        unicode_name = _UnicodeString(
            len(encoded),
            len(encoded) + 2,
            ctypes.cast(buffer, wintypes.LPWSTR),
        )
        attributes = _ObjectAttributes(
            ctypes.sizeof(_ObjectAttributes),
            wintypes.HANDLE(parent_handle),
            ctypes.pointer(unicode_name),
            obj_case_insensitive,
            None,
            None,
        )
        io_status = _IoStatusBlock()
        output = wintypes.HANDLE()
        desired_access = (
            (file_list_directory if directory else file_read_data)
            | file_read_attributes
            | synchronize
        )
        open_options = (
            (file_directory_file if directory else file_non_directory_file)
            | file_open_reparse_point
            | file_synchronous_io_nonalert
        )
        status = nt_open_file(
            ctypes.byref(output),
            desired_access,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            file_share_read | file_share_write | file_share_delete,
            open_options,
        )
        if status < 0:
            error = int(rtl_status_to_dos_error(status))
            raise ctypes.WinError(error)
        if output.value is None:
            raise ValueError(f"{label} relative open returned no handle")
        return int(output.value)

    directory_handles: list[int] = []
    leaf_handle: int | None = None
    leaf_descriptor: int | None = None
    try:
        root_handle = create_file(
            str(root),
            generic_read,
            file_share_read | file_share_write | file_share_delete,
            None,
            open_existing,
            file_flag_backup_semantics | file_flag_open_reparse_point,
            None,
        )
        if root_handle == invalid_handle_value:
            error = ctypes.get_last_error()
            if error in {2, 3}:
                raise FileNotFoundError(root)
            raise ctypes.WinError(error)
        directory_handles.append(int(root_handle))
        root_info = _information(int(root_handle))
        if (
            not (root_info.dwFileAttributes & file_attribute_directory)
            or root_info.dwFileAttributes & file_attribute_reparse_point
        ):
            raise ValueError(f"{label} parent namespace is aliased")

        current = int(root_handle)
        for component in components[:-1]:
            try:
                child = _open_relative(current, component, directory=True)
            except OSError as exc:
                if getattr(exc, "winerror", None) in {2, 3}:
                    raise FileNotFoundError(component) from exc
                raise ValueError(f"{label} parent namespace is unsafe or unreadable") from exc
            directory_handles.append(child)
            info = _information(child)
            if (
                not (info.dwFileAttributes & file_attribute_directory)
                or info.dwFileAttributes & file_attribute_reparse_point
            ):
                raise ValueError(f"{label} parent namespace is aliased")
            current = child

        try:
            leaf_handle = _open_relative(current, components[-1], directory=False)
        except OSError as exc:
            if getattr(exc, "winerror", None) in {2, 3}:
                raise FileNotFoundError(components[-1]) from exc
            raise ValueError(f"{label} is unsafe or unreadable") from exc
        leaf_info = _information(leaf_handle)
        if (
            leaf_info.dwFileAttributes & file_attribute_directory
            or leaf_info.dwFileAttributes & file_attribute_reparse_point
            or leaf_info.nNumberOfLinks != 1
        ):
            raise ValueError(f"{label} is not a regular non-aliased file")

        try:
            leaf_descriptor = msvcrt.open_osfhandle(
                leaf_handle,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
        except BaseException:
            close_handle(wintypes.HANDLE(leaf_handle))
            leaf_handle = None
            raise
        leaf_handle = None

        opened_before = os.fstat(leaf_descriptor)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(leaf_descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        opened_after = os.fstat(leaf_descriptor)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or not stat.S_ISREG(opened_after.st_mode)
            or opened_before.st_nlink != 1
            or opened_after.st_nlink != 1
            or not _stable_stat_metadata(opened_before, opened_after)
        ):
            raise ValueError(f"{label} changed while validating")

        verification_handles: list[int] = []
        verification_leaf: int | None = None
        try:
            verification_parent = directory_handles[0]
            for index, component in enumerate(components[:-1], start=1):
                try:
                    verification = _open_relative(
                        verification_parent,
                        component,
                        directory=True,
                    )
                except OSError as exc:
                    raise ValueError(
                        f"{label} canonical parent namespace changed while validating"
                    ) from exc
                verification_handles.append(verification)
                info = _information(verification)
                if (
                    not (info.dwFileAttributes & file_attribute_directory)
                    or info.dwFileAttributes & file_attribute_reparse_point
                    or _file_identity(verification) != _file_identity(directory_handles[index])
                ):
                    raise ValueError(
                        f"{label} canonical parent namespace changed while validating"
                    )
                verification_parent = verification

            try:
                verification_leaf = _open_relative(
                    verification_parent,
                    components[-1],
                    directory=False,
                )
            except OSError as exc:
                raise ValueError(f"{label} canonical leaf changed while validating") from exc
            current_leaf_handle = int(msvcrt.get_osfhandle(leaf_descriptor))
            if _file_identity(verification_leaf) != _file_identity(current_leaf_handle):
                raise ValueError(f"{label} canonical leaf changed while validating")
        finally:
            if verification_leaf is not None:
                close_handle(wintypes.HANDLE(verification_leaf))
            for verification in reversed(verification_handles):
                close_handle(wintypes.HANDLE(verification))
        return b"".join(chunks)
    except FileNotFoundError:
        raise
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"{label} is unsafe or unreadable") from exc
    finally:
        if leaf_descriptor is not None:
            try:
                os.close(leaf_descriptor)
            except OSError:
                pass
        if leaf_handle is not None:
            close_handle(wintypes.HANDLE(leaf_handle))
        for handle in reversed(directory_handles):
            close_handle(wintypes.HANDLE(handle))


def _read_nested_regular_file_bytes(
    root: Path,
    components: tuple[str, ...],
    *,
    label: str,
) -> bytes:
    if not components:
        raise ValueError(f"{label} path is empty")
    safe_components = tuple(
        _require_single_path_component(component, label=label)
        for component in components
    )
    if os.name == "nt":
        return _read_windows_nested_regular_file_bytes(root, safe_components, label=label)
    return _read_posix_nested_regular_file_bytes(root, safe_components, label=label)


def has_durable_workspace_history(workspace: str | Path) -> bool:
    """Return whether a missing registry would discard surviving economic/run evidence.

    A pristine readable zero-byte Decision Ledger and an empty transaction directory are
    allowed first-open artifacts. Everything else named here is durable product history
    or filesystem uncertainty and must make missing-registry initialization fail closed.
    """

    root = Path(workspace)
    transaction_root = root / ".run-transactions"
    transaction_stat = _lstat_or_none(transaction_root)
    if transaction_stat is not None:
        if not stat.S_ISDIR(transaction_stat.st_mode):
            return True
        try:
            next(transaction_root.iterdir())
        except StopIteration:
            pass
        else:
            return True

    for entry in root.iterdir():
        if entry.name.startswith("run-") and entry.name.endswith(".json"):
            return True

    if _lstat_or_none(root / "paper_book.json") is not None:
        return True

    ledger_path = root / "decisions.jsonl"
    ledger_stat = _lstat_or_none(ledger_path)
    if ledger_stat is not None:
        if (
            not stat.S_ISREG(ledger_stat.st_mode)
            or ledger_stat.st_nlink != 1
            or ledger_stat.st_size > 0
        ):
            return True
        try:
            descriptor = _open_read_only_descriptor(ledger_path)
        except OSError:
            return True

        ledger_is_pristine = False
        try:
            try:
                opened_before = os.fstat(descriptor)
            except OSError:
                return True
            if (
                not stat.S_ISREG(opened_before.st_mode)
                or opened_before.st_nlink != 1
                or not _path_matches_open_descriptor(ledger_path, descriptor, ledger_stat)
            ):
                return True

            try:
                first_byte = os.read(descriptor, 1)
                opened_after = os.fstat(descriptor)
            except OSError:
                return True
            if (
                first_byte
                or not stat.S_ISREG(opened_after.st_mode)
                or opened_after.st_nlink != 1
                or not _stable_stat_metadata(opened_before, opened_after)
                or not _path_matches_open_descriptor(ledger_path, descriptor, ledger_stat)
            ):
                return True
            ledger_is_pristine = True
        finally:
            try:
                os.close(descriptor)
            except OSError:
                ledger_is_pristine = False
        if not ledger_is_pristine:
            return True
    return False


class RepeatedExperimentError(RuntimeError):
    pass


class UnresolvedExperimentError(RuntimeError):
    pass


class ReconciliationError(RuntimeError):
    pass


class MixedStrategyWorkspaceError(RuntimeError):
    pass


class RunRegistry:
    """Fail-closed experiment ledger preventing accidental replay duplication after restart/crash."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self._read_existing()
        except FileNotFoundError as exc:
            raise ValueError("run registry is missing") from exc

    @classmethod
    def initialize_pristine(cls, path: str | Path) -> "RunRegistry":
        """Explicitly create the first registry only for a verified pristine workspace.

        Ordinary construction is a read/verification operation and never publishes missing
        durable state. Product startup is the sole first-open creation boundary and uses
        this method, which serializes publication with the canonical workspace economic lock.
        """

        registry = cls.__new__(cls)
        registry.path = Path(path)
        registry.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            registry._read_existing()
        except FileNotFoundError:
            registry._initialize_missing_registry()
        return registry

    def _read_existing_bytes(self) -> bytes:
        """Read bytes only from the exact stable regular object named by the registry path."""

        return _read_stable_regular_file_bytes(self.path, label="run registry path")

    def _read_existing(self) -> dict:
        """Read and validate only bytes bound to the current canonical registry object."""

        return self._parse_registry_payload(self._read_existing_bytes())

    def _initialize_missing_registry(self) -> None:
        """Serialize first publication against every cooperating economic writer.

        The initial missing-path observation is never publication authority. We first
        acquire the canonical workspace lock, then re-read the registry under that lock.
        If another process already owns the lock, a registry it has durably published can
        be adopted immediately; otherwise we briefly retry until that writer publishes or
        releases. The bounded retry also guarantees a caller that already owns the lock
        cannot deadlock itself if an external actor removed the registry unexpectedly.
        """

        deadline = time.monotonic() + _FIRST_OPEN_MAX_WAIT_SECONDS
        while True:
            lock = WorkspaceEconomicLock(self.path.parent)
            try:
                lock.acquire()
            except WorkspaceEconomicLockBusyError as contention:
                # Only typed native advisory-lock contention permits winner re-read.
                # Alias, identity, creation and backend failures remain fail-closed and
                # propagate without being reclassified as another writer's ownership.
                try:
                    self._read_existing()
                except FileNotFoundError:
                    if time.monotonic() >= deadline:
                        raise WorkspaceEconomicLockBusyError(
                            "run registry first-open could not serialize with the active economic writer"
                        ) from contention
                    time.sleep(_FIRST_OPEN_RETRY_SECONDS)
                    continue
                return

            primary_error: BaseException | None = None
            try:
                # The winner may have published while this process was waiting for
                # the OS lock. Existing bytes are authoritative and are never replaced
                # with a stale empty state.
                try:
                    self._read_existing()
                except FileNotFoundError:
                    pass
                else:
                    return
                try:
                    durable_history = has_durable_workspace_history(self.path.parent)
                except OSError as exc:
                    raise ValueError(
                        "cannot determine durable workspace history while run registry is missing"
                    ) from exc
                if durable_history:
                    raise ValueError("run registry is missing while durable run history exists")
                self._write({"schema_version": 1, "runs": {}})
                # Verify the exact published registry before exposing this object.
                self._read_existing()
                return
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                if primary_error is None:
                    lock.release()
                else:
                    try:
                        lock.release()
                    except BaseException as release_error:
                        try:
                            primary_error.add_note(
                                "WorkspaceEconomicLock release also failed during run registry first-open: "
                                f"{type(release_error).__name__}: {release_error}"
                            )
                        except BaseException:
                            pass

    @staticmethod
    def experiment_identity(market_sha256: str, results_sha256: str, strategy_id: str) -> str:
        canonical = f"{market_sha256}|{results_sha256}|{strategy_id}".encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def strategy_ids(self) -> tuple[str, ...]:
        """Return strategy identities already bound to economic runs in this workspace."""
        state = self._read()
        values: set[str] = set()
        for item in state["runs"].values():
            values.add(item["strategy_id"])
        return tuple(sorted(values))

    def assert_outcome_lineage_compatible(self, binding: OutcomeLineageBinding) -> None:
        """Reject a restart/fork before any new economic base is materialized."""
        if not isinstance(binding, OutcomeLineageBinding):
            raise ValueError("outcome lineage binding must be an OutcomeLineageBinding")
        self._assert_outcome_lineage_compatible_state(self._read(), binding)

    def begin(
        self,
        market_sha256: str,
        results_sha256: str,
        strategy_id: str,
        run_id: str,
        allow_repeat: bool = False,
        *,
        base_paper_book_sha256: str | None = None,
        base_decision_ledger_sha256: str | None = None,
        outcome_lineage: OutcomeLineageBinding | None = None,
    ) -> str:
        _require_canonical_sha256("market_sha256", market_sha256)
        _require_canonical_sha256("results_sha256", results_sha256)
        _require_nonempty_string("strategy_id", strategy_id)
        _require_nonempty_string("run_id", run_id)
        if not isinstance(allow_repeat, bool):
            raise ValueError("allow_repeat must be a boolean")
        if (base_paper_book_sha256 is None) != (base_decision_ledger_sha256 is None):
            raise ValueError("base transaction hashes must be supplied together")
        if base_paper_book_sha256 is not None:
            _require_canonical_sha256("base_paper_book_sha256", base_paper_book_sha256)
            _require_canonical_sha256("base_decision_ledger_sha256", base_decision_ledger_sha256)
        if outcome_lineage is not None and not isinstance(outcome_lineage, OutcomeLineageBinding):
            raise ValueError("outcome_lineage must be an OutcomeLineageBinding or null")

        state = self._read()
        if outcome_lineage is not None:
            self._assert_outcome_lineage_admissible_for_new_run_state(
                state, outcome_lineage
            )
        base_identity = self.experiment_identity(market_sha256, results_sha256, strategy_id)
        existing_pairs = [
            (key, item)
            for key, item in state["runs"].items()
            if item.get("base_identity") == base_identity
        ]
        unresolved = [
            item
            for item in state["runs"].values()
            if item.get("status") == "in_progress"
        ]
        if unresolved:
            raise UnresolvedExperimentError(
                "Workspace has an unresolved economic run; repair it before starting another paper experiment."
            )
        completed = [item for _key, item in existing_pairs if item.get("status") == "completed"]
        if completed and not allow_repeat:
            raise RepeatedExperimentError(
                "This dataset/strategy already completed in this workspace. Explicit allow_repeat is required for another experiment."
            )
        if any(item["run_id"] == run_id for item in state["runs"].values()):
            raise RepeatedExperimentError(
                "This run_id already has durable history in this workspace."
            )

        if not existing_pairs:
            key = base_identity
        elif completed:
            key = f"{base_identity}:repeat:{run_id}"
        else:
            key = f"{base_identity}:retry:{run_id}"
        if key in state["runs"]:
            raise RepeatedExperimentError(
                "This run_id already has durable history for this dataset/strategy."
            )

        entry = {
            "base_identity": base_identity,
            "run_id": run_id,
            "market_sha256": market_sha256,
            "results_sha256": results_sha256,
            "strategy_id": strategy_id,
            "status": "in_progress",
        }
        if base_paper_book_sha256 is not None:
            entry["base_paper_book_sha256"] = base_paper_book_sha256
            entry["base_decision_ledger_sha256"] = base_decision_ledger_sha256
        if outcome_lineage is not None:
            self._record_outcome_lineage_trust_state(state, outcome_lineage)
            entry["outcome_lineage"] = outcome_lineage_payload(outcome_lineage)
        state["runs"][key] = entry
        self._validate_entry(key, entry)
        self._write(state)
        return key

    def complete(
        self,
        key: str,
        result_path: str | None = None,
        *,
        paper_book_sha256: str | None = None,
        decision_ledger_sha256: str | None = None,
    ) -> None:
        if result_path is not None and not isinstance(result_path, str):
            raise ValueError("result_path must be a string or null")
        if paper_book_sha256 is not None:
            _require_canonical_sha256("paper_book_sha256", paper_book_sha256)
        if decision_ledger_sha256 is not None:
            _require_canonical_sha256("decision_ledger_sha256", decision_ledger_sha256)

        state = self._read()
        item = state["runs"].get(key)
        if item is None:
            raise KeyError(key)
        if item.get("status") != "in_progress":
            raise ValueError("run is not in progress")
        item["status"] = "completed"
        item["result_path"] = result_path
        if paper_book_sha256 is not None:
            item["paper_book_sha256"] = paper_book_sha256
        if decision_ledger_sha256 is not None:
            item["decision_ledger_sha256"] = decision_ledger_sha256
        self._validate_entry(key, item)
        self._write(state)

    def abort_uncommitted(
        self,
        key: str,
        *,
        reason: str,
        paper_book_sha256: str,
        decision_ledger_sha256: str,
    ) -> None:
        if not isinstance(reason, str):
            raise ValueError("abort reason must be a string")
        _require_canonical_sha256("paper_book_sha256", paper_book_sha256)
        _require_canonical_sha256("decision_ledger_sha256", decision_ledger_sha256)

        state = self._read()
        item = state["runs"].get(key)
        if item is None:
            raise KeyError(key)
        if item.get("status") != "in_progress":
            raise ReconciliationError("only an in-progress run can be aborted")
        expected_book = item.get("base_paper_book_sha256")
        expected_ledger = item.get("base_decision_ledger_sha256")
        if not isinstance(expected_book, str) or not isinstance(expected_ledger, str):
            raise ReconciliationError("registry lacks base hashes required for safe uncommitted abort")
        if paper_book_sha256 != expected_book or decision_ledger_sha256 != expected_ledger:
            raise ReconciliationError("canonical economic state does not match the recorded transaction base")
        item["status"] = "aborted"
        item["abort_reason"] = reason
        item["paper_book_sha256"] = paper_book_sha256
        item["decision_ledger_sha256"] = decision_ledger_sha256
        self._validate_entry(key, item)
        self._write(state)

    def in_progress(self) -> tuple[tuple[str, dict], ...]:
        state = self._read()
        return tuple(
            (key, dict(item))
            for key, item in state["runs"].items()
            if item.get("status") == "in_progress"
        )

    def get(self, key: str) -> dict:
        item = self._read()["runs"].get(key)
        if item is None:
            raise KeyError(key)
        return dict(item)

    def verified_completed_summary_for_run(self, run_id: str) -> tuple[dict, str]:
        """Return transaction-bound summary payload and SHA-256 for one completed run.

        This is a read-only historical evidence boundary. It does not require the
        current PaperBook to still equal the run's terminal snapshot, because later
        completed runs may legitimately have advanced the canonical workspace.
        Instead the exact run-summary bytes must still match the terminal
        RunTransaction manifest and the completed RunRegistry identity/hash evidence.
        """

        _require_nonempty_string("run_id", run_id)
        state = self._read()
        matches = [
            (key, item)
            for key, item in state["runs"].items()
            if item.get("run_id") == run_id
        ]
        if not matches:
            raise KeyError(run_id)
        if len(matches) != 1:
            raise ReconciliationError("run_id is not unique in the run registry")
        key, item = matches[0]
        if item.get("status") != "completed":
            raise ReconciliationError("run is not completed")

        expected_name = f"run-{run_id}.json"
        result_path = item.get("result_path")
        if (
            not isinstance(result_path, str)
            or not result_path
            or result_path.replace("\\", "/").rsplit("/", 1)[-1] != expected_name
        ):
            raise ReconciliationError("completed run result_path is not canonical")

        summary_path = self.path.parent / expected_name
        try:
            summary_bytes = _read_stable_regular_file_bytes(
                summary_path,
                label="completed run summary",
            )
            manifest_bytes = _read_nested_regular_file_bytes(
                self.path.parent,
                (".run-transactions", run_id, "manifest.json"),
                label="completed run transaction manifest",
            )
        except (FileNotFoundError, ValueError) as exc:
            raise ReconciliationError(
                "completed run lacks stable transaction-bound summary evidence"
            ) from exc

        try:
            summary = json.loads(
                summary_bytes.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json_constant,
            )
            manifest = json.loads(
                manifest_bytes.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json_constant,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ReconciliationError(
                "completed run summary or transaction manifest is invalid"
            ) from exc
        if not isinstance(summary, dict) or not isinstance(manifest, dict):
            raise ReconciliationError("completed run evidence must be JSON objects")
        if summary.get("schema_version") != 2:
            raise ReconciliationError("completed run summary schema_version is unsupported")
        if manifest.get("phase") not in {"canonical_committed", "completed"}:
            raise ReconciliationError("run transaction is not terminal")

        expected_identity = {
            "experiment_key": key,
            "run_id": run_id,
            "market_sha256": item.get("market_sha256"),
            "sealed_results_sha256": item.get("results_sha256"),
            "strategy_id": item.get("strategy_id"),
        }
        for field, expected_value in expected_identity.items():
            if summary.get(field) != expected_value or manifest.get(field) != expected_value:
                raise ReconciliationError(
                    f"completed run {field} does not match durable authorities"
                )
        if summary.get("real_money_execution") is not False:
            raise ReconciliationError("completed run truth boundary is invalid")
        if summary.get("transaction_run_id") != run_id:
            raise ReconciliationError("completed run transaction_run_id mismatch")
        if summary.get("transaction_schema_version") != manifest.get("schema_version"):
            raise ReconciliationError("completed run transaction schema mismatch")

        targets = manifest.get("targets")
        new_state = manifest.get("new")
        if not isinstance(targets, dict) or targets.get("summary") != expected_name:
            raise ReconciliationError("run transaction summary target is invalid")
        if not isinstance(new_state, dict):
            raise ReconciliationError("run transaction terminal evidence is incomplete")
        expected_summary_sha = new_state.get("summary_sha256")
        if not _is_canonical_sha256(expected_summary_sha):
            raise ReconciliationError("run transaction lacks summary SHA-256")
        actual_summary_sha = hashlib.sha256(summary_bytes).hexdigest()
        if actual_summary_sha != expected_summary_sha:
            raise ReconciliationError("completed run summary SHA-256 mismatch")

        for field in ("paper_book_sha256", "decision_ledger_sha256"):
            summary_hash = summary.get(field)
            registry_hash = item.get(field)
            manifest_hash = new_state.get(field)
            if (
                not _is_canonical_sha256(summary_hash)
                or summary_hash != registry_hash
                or summary_hash != manifest_hash
            ):
                raise ReconciliationError(
                    f"completed run {field} does not match durable authorities"
                )
        return dict(summary), actual_summary_sha

    def reconcile_completed_summary(
        self,
        key: str,
        result_path: str | Path,
        paper_book_path: str | Path,
    ) -> None:
        """Complete only a run whose durable summary and current PaperBook prove the same commit."""

        state = self._read()
        item = state["runs"].get(key)
        if item is None:
            raise KeyError(key)
        if item.get("status") != "in_progress":
            raise ReconciliationError("only an in-progress run can be reconciled")

        result = Path(result_path)
        book = Path(paper_book_path)
        workspace = self.path.parent.resolve()
        if result.resolve().parent != workspace:
            raise ReconciliationError("run summary must be inside the registry workspace")
        if book.resolve() != (workspace / "paper_book.json").resolve():
            raise ReconciliationError("PaperBook reconciliation path must be the canonical workspace paper_book.json")
        expected_name = f"run-{item['run_id']}.json"
        if result.name != expected_name:
            raise ReconciliationError("run summary filename does not match registry run_id")
        if not result.is_file():
            raise ReconciliationError("durable run summary not found")
        if not book.is_file():
            raise ReconciliationError("canonical PaperBook snapshot not found")

        try:
            summary = json.loads(
                result.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json_constant,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ReconciliationError("run summary is unreadable or invalid JSON") from exc
        schema_version = summary.get("schema_version") if isinstance(summary, dict) else None
        if (
            not isinstance(summary, dict)
            or isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != 2
        ):
            raise ReconciliationError("run summary lacks reconciliation schema version 2")

        expected = {
            "experiment_key": key,
            "run_id": item.get("run_id"),
            "market_sha256": item.get("market_sha256"),
            "sealed_results_sha256": item.get("results_sha256"),
            "strategy_id": item.get("strategy_id"),
        }
        mismatches = [
            field
            for field, value in expected.items()
            if summary.get(field) != value
        ]
        if mismatches:
            raise ReconciliationError(
                "run summary identity mismatch: " + ",".join(sorted(mismatches))
            )
        if summary.get("real_money_execution") is not False:
            raise ReconciliationError("run summary truth boundary is invalid")
        declared_book_hash = summary.get("paper_book_sha256")
        if not _is_canonical_sha256(declared_book_hash):
            raise ReconciliationError("run summary lacks canonical PaperBook SHA-256 reconciliation evidence")
        actual_book_hash = sha256_file(book)
        if actual_book_hash != declared_book_hash:
            raise ReconciliationError("current PaperBook SHA-256 does not match completed run summary")

        declared_ledger_hash = summary.get("decision_ledger_sha256")
        if declared_ledger_hash is not None:
            ledger_path = workspace / "decisions.jsonl"
            if not _is_canonical_sha256(declared_ledger_hash):
                raise ReconciliationError("run summary Decision Ledger SHA-256 evidence is invalid")
            if not ledger_path.is_file() or sha256_file(ledger_path) != declared_ledger_hash:
                raise ReconciliationError("current Decision Ledger SHA-256 does not match completed run summary")

        base_identity = self.experiment_identity(
            str(item.get("market_sha256")),
            str(item.get("results_sha256")),
            str(item.get("strategy_id")),
        )
        if item.get("base_identity") != base_identity:
            raise ReconciliationError("registry base identity is inconsistent")

        item["status"] = "completed"
        item["result_path"] = str(result)
        item["reconciled_from_summary"] = True
        item["paper_book_sha256"] = actual_book_hash
        if declared_ledger_hash is not None:
            item["decision_ledger_sha256"] = declared_ledger_hash
        self._validate_entry(key, item)
        self._write(state)

    def _durable_summary_lineage_bindings(
        self,
        *,
        active_legacy_run_ids: frozenset[str] = frozenset(),
    ) -> tuple[OutcomeLineageBinding, ...]:
        """Recover lineage trust from transaction-bound run summaries.

        Mutable registry status is never allowed to decide whether terminal transaction
        evidence is inspected. The nested manifest is read through a held component
        namespace first; only genuinely nonterminal transactions remain owned by the
        existing recovery path. Summary bytes are then captured once from the canonical
        direct workspace leaf and verified against the terminal manifest hash.
        """
        bindings: list[OutcomeLineageBinding] = []
        for summary_path in sorted(self.path.parent.glob("run-*.json")):
            filename_run_id = summary_path.name.removeprefix("run-").removesuffix(".json")
            try:
                run_id = _require_portable_run_id(filename_run_id)
            except RunTransactionError:
                # A nonportable run-* filename is never allowed to become durable trust.
                continue

            try:
                manifest_bytes = _read_nested_regular_file_bytes(
                    self.path.parent,
                    (".run-transactions", run_id, "manifest.json"),
                    label="durable lineage-trust transaction manifest",
                )
            except FileNotFoundError:
                # Genuine historical schema-one summaries may predate RunTransaction.
                # They remain acceptable only when they do not themselves claim lineage
                # trust; inspect the exact direct leaf once to distinguish that case.
                try:
                    summary_bytes = _read_stable_regular_file_bytes(
                        summary_path,
                        label="durable lineage-trust run summary",
                    )
                    summary = json.loads(
                        summary_bytes,
                        object_pairs_hook=_reject_duplicate_json_keys,
                        parse_constant=_reject_nonfinite_json_constant,
                    )
                except FileNotFoundError:
                    continue
                except (UnicodeError, json.JSONDecodeError, ValueError):
                    # Without a transaction manifest this summary is not durable lineage
                    # authority. Leave malformed legacy/unbound summaries to the normal
                    # reconciliation path instead of changing its established errors.
                    continue
                if not isinstance(summary, dict):
                    raise ValueError("durable lineage-trust run summary is invalid")
                if summary.get(_LINEAGE_TRUST_FIELD) is None:
                    continue
                raise ValueError("durable lineage-trust run summary lacks transaction manifest")
            except ValueError as exc:
                raise ValueError(
                    "transaction manifest path is not a regular file or canonical namespace changed"
                ) from exc

            try:
                manifest = json.loads(
                    manifest_bytes.decode("utf-8"),
                    object_pairs_hook=_reject_duplicate_json_keys,
                    parse_constant=_reject_nonfinite_json_constant,
                )
            except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError("durable lineage-trust transaction manifest is invalid") from exc
            if not isinstance(manifest, dict) or manifest.get("run_id") != run_id:
                raise ValueError("durable lineage-trust transaction manifest identity is invalid")

            phase = manifest.get("phase")
            terminal = phase in {"canonical_committed", "completed"}
            if not terminal and run_id in active_legacy_run_ids:
                # Recovery remains authoritative only after manifest evidence proves this
                # transaction is genuinely nonterminal. Mutable registry status alone
                # can no longer suppress a terminal durable witness.
                continue

            try:
                summary_bytes = _read_stable_regular_file_bytes(
                    summary_path,
                    label="durable lineage-trust run summary",
                )
            except FileNotFoundError as exc:
                if terminal:
                    raise ValueError(
                        "durable lineage-trust terminal transaction lacks run summary"
                    ) from exc
                continue
            try:
                summary = json.loads(
                    summary_bytes.decode("utf-8"),
                    object_pairs_hook=_reject_duplicate_json_keys,
                    parse_constant=_reject_nonfinite_json_constant,
                )
            except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError("durable lineage-trust run summary is invalid") from exc
            if not isinstance(summary, dict):
                raise ValueError("durable lineage-trust run summary is invalid")
            raw_binding = summary.get(_LINEAGE_TRUST_FIELD)
            summary_run_id = summary.get("run_id")
            if (
                not isinstance(summary_run_id, str)
                or summary_run_id != run_id
                or summary_path.name != f"run-{run_id}.json"
            ):
                if raw_binding is None and not terminal:
                    continue
                raise ValueError("durable lineage-trust run summary identity is invalid")

            if not terminal:
                if raw_binding is None:
                    continue
                raise ValueError(
                    "durable lineage-trust run summary lacks terminal transaction authority"
                )

            targets = manifest.get("targets")
            if not isinstance(targets, dict) or targets.get("summary") != summary_path.name:
                raise ValueError("durable lineage-trust transaction summary target is invalid")
            new_state = manifest.get("new")
            expected_summary_sha = (
                new_state.get("summary_sha256") if isinstance(new_state, dict) else None
            )
            if not _is_canonical_sha256(expected_summary_sha):
                raise ValueError("durable lineage-trust transaction lacks summary SHA-256")
            if hashlib.sha256(summary_bytes).hexdigest() != expected_summary_sha:
                raise ValueError(
                    "durable lineage-trust run summary identity mismatch; "
                    "run summary SHA-256 mismatch"
                )
            if raw_binding is None:
                continue
            try:
                binding = outcome_lineage_binding_from_payload(
                    raw_binding,
                    context="durable run summary outcome_lineage_trust",
                )
            except OutcomeLineageTrustError as exc:
                raise ValueError("durable run summary contains invalid outcome lineage trust") from exc
            bindings.append(binding)
        return tuple(bindings)

    @staticmethod
    def _outcome_lineage_trust_bindings(
        state: dict,
    ) -> dict[tuple[str, str], OutcomeLineageBinding]:
        if state.get("schema_version") != _LINEAGE_TRUST_SCHEMA_VERSION:
            return {}
        raw_trust = state.get(_LINEAGE_TRUST_FIELD)
        if not isinstance(raw_trust, list) or not raw_trust:
            raise ValueError("run registry lineage-trust schema requires durable trust bindings")
        bindings: dict[tuple[str, str], OutcomeLineageBinding] = {}
        for raw_binding in raw_trust:
            try:
                binding = outcome_lineage_binding_from_payload(
                    raw_binding,
                    context="run registry outcome_lineage_trust",
                )
            except OutcomeLineageTrustError as exc:
                raise ValueError("run registry contains invalid outcome lineage trust") from exc
            identity = (binding.source_identity, binding.record_id)
            if identity in bindings:
                raise ValueError("run registry contains duplicate outcome lineage trust identity")
            bindings[identity] = binding
        return bindings

    @classmethod
    def _record_outcome_lineage_trust_state(
        cls,
        state: dict,
        incoming: OutcomeLineageBinding,
    ) -> None:
        if state.get("schema_version") == _LEGACY_SCHEMA_VERSION:
            if any("outcome_lineage" in item for item in state["runs"].values()):
                raise ValueError(
                    "legacy run registry cannot migrate outcome lineage evidence without durable trust binding"
                )
            state["schema_version"] = _LINEAGE_TRUST_SCHEMA_VERSION
            state[_LINEAGE_TRUST_FIELD] = []
            bindings: dict[tuple[str, str], OutcomeLineageBinding] = {}
        else:
            bindings = cls._outcome_lineage_trust_bindings(state)

        identity = (incoming.source_identity, incoming.record_id)
        trusted = bindings.get(identity)
        if trusted is not None:
            assert_compatible_outcome_lineages(trusted, incoming)
            if len(incoming.revisions) <= len(trusted.revisions):
                return

        payload = outcome_lineage_payload(incoming)
        raw_trust = state[_LINEAGE_TRUST_FIELD]
        if trusted is None:
            raw_trust.append(payload)
        else:
            for index, raw_binding in enumerate(raw_trust):
                candidate = outcome_lineage_binding_from_payload(
                    raw_binding,
                    context="run registry outcome_lineage_trust",
                )
                if (candidate.source_identity, candidate.record_id) == identity:
                    raw_trust[index] = payload
                    break
            else:
                raise ValueError("run registry lost an accepted outcome lineage trust binding")
        raw_trust.sort(key=lambda value: (value["source_identity"], value["record_id"]))

    @classmethod
    def _assert_outcome_lineage_compatible_state(
        cls,
        state: dict,
        incoming: OutcomeLineageBinding,
    ) -> None:
        trusted_bindings = cls._outcome_lineage_trust_bindings(state)
        trusted = trusted_bindings.get((incoming.source_identity, incoming.record_id))
        if trusted is not None:
            assert_compatible_outcome_lineages(trusted, incoming)
        for item in state["runs"].values():
            raw_lineage = item.get("outcome_lineage")
            if raw_lineage is None:
                continue
            lineage = outcome_lineage_binding_from_payload(
                raw_lineage,
                context="run registry outcome_lineage",
            )
            assert_compatible_outcome_lineages(lineage, incoming)

    @classmethod
    def _assert_outcome_lineage_admissible_for_new_run_state(
        cls,
        state: dict,
        incoming: OutcomeLineageBinding,
    ) -> None:
        """Reject stale prefixes for new runs while preserving historical readability."""

        cls._assert_outcome_lineage_compatible_state(state, incoming)
        trusted = cls._outcome_lineage_trust_bindings(state).get(
            (incoming.source_identity, incoming.record_id)
        )
        if trusted is not None and len(incoming.revisions) < len(trusted.revisions):
            raise OutcomeLineageTrustError(
                "new economic run outcome lineage is older than the trusted current head"
            )

    def _validate_entry(self, key: object, item: object) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("run registry contains an invalid experiment key")
        if not isinstance(item, dict):
            raise ValueError("run registry contains an invalid run entry")
        fields = set(item)
        if not _REQUIRED_ENTRY_FIELDS.issubset(fields) or not fields.issubset(
            _REQUIRED_ENTRY_FIELDS | _OPTIONAL_ENTRY_FIELDS
        ):
            raise ValueError("run registry contains invalid run entry fields")

        status = item.get("status")
        if status not in _ALLOWED_STATUSES:
            raise ValueError("run registry contains an invalid status")
        market_sha256 = item.get("market_sha256")
        results_sha256 = item.get("results_sha256")
        strategy_id = item.get("strategy_id")
        run_id = item.get("run_id")
        if not _is_canonical_sha256(market_sha256) or not _is_canonical_sha256(results_sha256):
            raise ValueError("run registry contains invalid canonical SHA-256 identity fields")
        if (
            not isinstance(strategy_id, str)
            or not strategy_id
            or not isinstance(run_id, str)
            or not run_id
        ):
            raise ValueError("run registry contains invalid experiment identity fields")

        base_book_present = "base_paper_book_sha256" in item
        base_ledger_present = "base_decision_ledger_sha256" in item
        if base_book_present != base_ledger_present:
            raise ValueError("run registry contains incomplete base transaction evidence")
        for field_name in _HASH_EVIDENCE_FIELDS:
            if field_name in item and not _is_canonical_sha256(item[field_name]):
                raise ValueError(f"run registry contains invalid {field_name}")

        if "outcome_lineage" in item:
            try:
                outcome_lineage_binding_from_payload(
                    item["outcome_lineage"],
                    context="run registry outcome_lineage",
                )
            except OutcomeLineageTrustError as exc:
                raise ValueError("run registry contains invalid outcome lineage evidence") from exc
        if "result_path" in item and item["result_path"] is not None and not isinstance(item["result_path"], str):
            raise ValueError("run registry contains an invalid result_path")
        if "abort_reason" in item and not isinstance(item["abort_reason"], str):
            raise ValueError("run registry contains an invalid abort_reason")
        if "reconciled_from_summary" in item and item["reconciled_from_summary"] is not True:
            raise ValueError("run registry contains invalid reconciliation evidence")

        expected_base_identity = self.experiment_identity(
            market_sha256,
            results_sha256,
            strategy_id,
        )
        if item.get("base_identity") != expected_base_identity:
            raise ValueError("run registry contains an inconsistent base identity")
        expected_keys = {
            expected_base_identity,
            f"{expected_base_identity}:repeat:{run_id}",
            f"{expected_base_identity}:retry:{run_id}",
        }
        if key not in expected_keys:
            raise ValueError("run registry contains an inconsistent experiment key")

        if status == "in_progress":
            unexpected = fields & _FINAL_ONLY_FIELDS
            if unexpected:
                raise ValueError("in-progress run registry entry contains final-state evidence")
        elif status == "aborted":
            required_abort_fields = {
                "base_paper_book_sha256",
                "base_decision_ledger_sha256",
                "paper_book_sha256",
                "decision_ledger_sha256",
                "abort_reason",
            }
            if not required_abort_fields.issubset(fields):
                raise ValueError("aborted run registry entry lacks rollback evidence")
            if "result_path" in fields or "reconciled_from_summary" in fields:
                raise ValueError("aborted run registry entry contains completed-run evidence")
            if item["paper_book_sha256"] != item["base_paper_book_sha256"]:
                raise ValueError("aborted run registry PaperBook evidence does not match transaction base")
            if item["decision_ledger_sha256"] != item["base_decision_ledger_sha256"]:
                raise ValueError("aborted run registry Decision Ledger evidence does not match transaction base")
        else:
            if "abort_reason" in fields:
                raise ValueError("completed run registry entry contains abort evidence")
            if base_book_present:
                required_transaction_terminal_fields = {
                    "result_path",
                    "paper_book_sha256",
                    "decision_ledger_sha256",
                }
                if not required_transaction_terminal_fields.issubset(fields):
                    raise ValueError("completed transaction-aware run registry entry lacks terminal economic evidence")
                result_path = item.get("result_path")
                if not isinstance(result_path, str) or not result_path:
                    raise ValueError("completed transaction-aware run registry entry lacks result_path evidence")
                if _portable_basename(result_path) != f"run-{run_id}.json":
                    raise ValueError(
                        "completed transaction-aware run registry result_path does not match run_id"
                    )
            if item.get("reconciled_from_summary") is True:
                result_path = item.get("result_path")
                if not isinstance(result_path, str) or not result_path:
                    raise ValueError("reconciled run registry entry lacks result_path evidence")
                if _portable_basename(result_path) != f"run-{run_id}.json":
                    raise ValueError("reconciled run registry result_path does not match run_id")
                if "paper_book_sha256" not in fields:
                    raise ValueError("reconciled run registry entry lacks PaperBook hash evidence")

    def _parse_registry_payload(self, payload: bytes) -> dict:
        try:
            raw = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json_constant,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("invalid run registry") from exc

        schema_version = raw.get("schema_version") if isinstance(raw, dict) else None
        if not isinstance(raw, dict) or isinstance(schema_version, bool) or not isinstance(
            schema_version, int
        ):
            raise ValueError("invalid run registry")
        if schema_version == _LEGACY_SCHEMA_VERSION:
            if set(raw) != {"schema_version", "runs"}:
                raise ValueError("invalid run registry")
        elif schema_version == _LINEAGE_TRUST_SCHEMA_VERSION:
            if set(raw) != {"schema_version", "runs", _LINEAGE_TRUST_FIELD}:
                raise ValueError("invalid run registry")
        else:
            raise ValueError("invalid run registry")
        if not isinstance(raw.get("runs"), dict):
            raise ValueError("invalid run registry")

        # Always scan transaction-bound summaries.  A downgraded schema-one file may
        # retain unrelated legacy run entries specifically to avoid an empty-registry
        # check; durable hash-bound lineage evidence must still make that downgrade
        # impossible.  Active legacy summaries without lineage evidence remain outside
        # this trust check and are reconciled by the recovery path itself.
        active_legacy_run_ids = frozenset(
            item.get("run_id")
            for item in raw["runs"].values()
            if isinstance(item, dict)
            and isinstance(item.get("run_id"), str)
            and item.get("run_id")
            and item.get("status") == "in_progress"
        )
        durable_summary_bindings = self._durable_summary_lineage_bindings(
            active_legacy_run_ids=(
                active_legacy_run_ids
                if schema_version == _LEGACY_SCHEMA_VERSION
                else frozenset()
            ),
        )
        if schema_version == _LEGACY_SCHEMA_VERSION and durable_summary_bindings:
            raise ValueError(
                "run registry lineage-trust schema was downgraded despite durable run summary evidence"
            )

        trusted_bindings = self._outcome_lineage_trust_bindings(raw)
        if schema_version == _LINEAGE_TRUST_SCHEMA_VERSION:
            for durable in durable_summary_bindings:
                identity = (durable.source_identity, durable.record_id)
                trusted = trusted_bindings.get(identity)
                if trusted is None:
                    raise ValueError(
                        "run registry lost lineage trust preserved by durable run summary"
                    )
                try:
                    assert_compatible_outcome_lineages(trusted, durable)
                except OutcomeLineageTrustError as exc:
                    raise ValueError(
                        "run registry conflicts with lineage trust preserved by durable run summary"
                    ) from exc
                if len(trusted.revisions) < len(durable.revisions):
                    raise ValueError(
                        "run registry lineage trust is older than durable run summary evidence"
                    )

        seen_run_ids: set[str] = set()
        longest_lineage_by_identity: dict[tuple[str, str], OutcomeLineageBinding] = {}
        for key, item in raw["runs"].items():
            self._validate_entry(key, item)
            run_id = item["run_id"]
            if run_id in seen_run_ids:
                raise ValueError("run registry contains duplicate run_id evidence")
            seen_run_ids.add(run_id)
            raw_lineage = item.get("outcome_lineage")
            if raw_lineage is None:
                continue
            if schema_version != _LINEAGE_TRUST_SCHEMA_VERSION:
                raise ValueError("legacy run registry cannot contain outcome lineage evidence")
            try:
                lineage = outcome_lineage_binding_from_payload(
                    raw_lineage,
                    context="run registry outcome_lineage",
                )
                identity = (lineage.source_identity, lineage.record_id)
                durable_trust = trusted_bindings.get(identity)
                if durable_trust is None:
                    raise ValueError(
                        "run registry outcome lineage lacks registry-level trust binding"
                    )
                assert_compatible_outcome_lineages(durable_trust, lineage)
                if len(lineage.revisions) > len(durable_trust.revisions):
                    raise ValueError(
                        "run registry outcome lineage exceeds registry-level trust history"
                    )
                trusted = longest_lineage_by_identity.get(identity)
                if trusted is not None:
                    assert_compatible_outcome_lineages(trusted, lineage)
                if trusted is None or len(lineage.revisions) > len(trusted.revisions):
                    longest_lineage_by_identity[identity] = lineage
            except OutcomeLineageTrustError as exc:
                raise ValueError("run registry contains conflicting outcome lineage evidence") from exc
        return raw

    def _read(self) -> dict:
        return self._read_existing()

    def _write(self, raw: dict) -> None:
        atomic_write_json(self.path, raw)
