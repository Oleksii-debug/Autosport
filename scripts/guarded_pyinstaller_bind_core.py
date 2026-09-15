from __future__ import annotations

import argparse
import builtins
import ctypes
import hashlib
import os
import pathlib
import re
import secrets
import shutil
import stat
import subprocess
import sys
from ctypes import wintypes
from typing import Any

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_WINDOWS_SID_RE = re.compile(r"S-\d-(?:\d+-)+\d+")
_EXPECTED_PYINSTALLER_VERSION = "6.22.3"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_DELETE_ACCESS = 0x00010000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ERROR_ACCESS_DENIED = 5
_ERROR_SHARING_VIOLATION = 32
_ERROR_FILE_EXISTS = 80
_ERROR_ALREADY_EXISTS = 183
_EXPECTED_RESOURCE_DENY_RIGHTS = "(WD,AD,WEA,WA,DE)"
_RESOURCE_API_TRANSITIONS = frozenset(
    {"remove-resources", "icon", "version-info", "resource", "manifest"}
)

_TRUSTED_VERIFIER_LAUNCHER = r'''
import hashlib
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
expected = sys.argv[2]
data = path.read_bytes()
actual = hashlib.sha256(data).hexdigest()
if actual != expected:
    raise SystemExit(f"trusted verifier SHA-256 mismatch: expected {expected}, got {actual}")
sys.argv = [str(path), *sys.argv[3:]]
namespace = {
    "__name__": "__main__",
    "__file__": str(path),
    "__package__": None,
    "__cached__": None,
}
exec(compile(data, str(path), "exec"), namespace)
'''


class _RetainedArtifactWriter:
    """Expose PyInstaller's pinned final ``wb`` rewrite without releasing our file fence."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def __enter__(self) -> Any:
        self._stream.seek(0)
        self._stream.truncate(0)
        return self._stream

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._stream.flush()
        os.fsync(self._stream.fileno())
        return False


class _RetainedArtifactAppender:
    """Expose a pinned append-only mutation without releasing the authoritative handle."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def __enter__(self) -> Any:
        self._stream.seek(0, os.SEEK_END)
        return self._stream

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._stream.flush()
        os.fsync(self._stream.fileno())
        return False


def _normalized_path(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _object_identity(value: os.stat_result) -> tuple[int, int]:
    return int(value.st_dev), int(value.st_ino)


def _stable_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _sha256_stream(stream: Any) -> str:
    position = stream.tell()
    try:
        stream.seek(0)
        digest = hashlib.sha256()
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
    finally:
        stream.seek(position)


def _require_expected_pyinstaller_version(actual: str) -> None:
    if actual != _EXPECTED_PYINSTALLER_VERSION:
        raise RuntimeError(
            "guarded PyInstaller producer version mismatch: "
            f"expected {_EXPECTED_PYINSTALLER_VERSION}, got {actual}"
        )


def _windows_kernel32() -> Any:
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _close_windows_handle(raw_handle: Any) -> None:
    kernel32 = _windows_kernel32()
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    if not close_handle(raw_handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_attributes(path: pathlib.Path) -> int:
    kernel32 = _windows_kernel32()
    get_attributes = kernel32.GetFileAttributesW
    get_attributes.argtypes = (wintypes.LPCWSTR,)
    get_attributes.restype = wintypes.DWORD
    ctypes.set_last_error(0)
    attributes = int(get_attributes(str(path)))
    if attributes == 0xFFFFFFFF:
        raise ctypes.WinError(ctypes.get_last_error())
    return attributes


def _require_regular_nonreparse(path: pathlib.Path, *, label: str) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"{label} is not readable: {path}") from exc
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        raise RuntimeError(f"{label} must be a regular non-symlink file: {path}")
    if os.name == "nt" and _windows_attributes(path) & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise RuntimeError(f"{label} must not be a Windows reparse point: {path}")
    return value


def _windows_system_binary(name: str) -> pathlib.Path:
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        raise RuntimeError("SystemRoot is unavailable for trusted Windows system binary lookup")
    candidate = pathlib.Path(system_root) / "System32" / name
    _require_regular_nonreparse(candidate, label=f"trusted Windows system binary {name}")
    return candidate


def _current_windows_user_sid() -> str:
    """Resolve the current token SID through the protected system ``whoami.exe``."""

    whoami = _windows_system_binary("whoami.exe")
    completed = subprocess.run(
        [str(whoami), "/user", "/fo", "csv", "/nh"],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"trusted current-user SID lookup exited {completed.returncode}"
        )
    match = _WINDOWS_SID_RE.search(completed.stdout)
    if match is None:
        raise RuntimeError("trusted current-user SID lookup returned no canonical SID")
    return match.group(0)


def _set_expected_snapshot_write_fence(path: pathlib.Path, sid: str) -> None:
    """Deny new same-token data writes/deletes across native resource commit return."""

    icacls = _windows_system_binary("icacls.exe")
    completed = subprocess.run(
        [
            str(icacls),
            str(path),
            "/deny",
            f"*{sid}:{_EXPECTED_RESOURCE_DENY_RIGHTS}",
            "/Q",
        ],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "trusted expected-snapshot post-commit write fence could not be installed: "
            f"icacls exited {completed.returncode}"
        )


def _remove_expected_snapshot_write_fence(path: pathlib.Path, sid: str) -> None:
    """Remove only the temporary explicit deny ACE from the disposable snapshot."""

    icacls = _windows_system_binary("icacls.exe")
    completed = subprocess.run(
        [str(icacls), str(path), "/remove:d", f"*{sid}", "/Q"],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "trusted expected-snapshot post-commit write fence could not be removed: "
            f"icacls exited {completed.returncode}"
        )


def _open_delete_denial_continuity_anchor(
    path: pathlib.Path,
) -> tuple[Any, tuple[int, int]]:
    """Pin the producer pathname outside Win32 resource commit windows.

    The zero-access handle shares READ/WRITE but deliberately not DELETE. It is
    retained whenever the producer API permits another handle. Win32 resource
    commits require every unrelated file handle to be closed, so those bounded
    transitions instead re-prove the same object identity and expected digest
    immediately after the resource API returns.
    """

    if os.name != "nt":
        raise RuntimeError("guarded PyInstaller artifact binding is Windows-only")

    kernel32 = _windows_kernel32()
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

    ctypes.set_last_error(0)
    raw_handle = create_file(
        str(path),
        0,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    handle_value = (
        raw_handle
        if isinstance(raw_handle, int)
        else ctypes.cast(raw_handle, ctypes.c_void_p).value
    )
    if handle_value in {None, _INVALID_HANDLE_VALUE}:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        current = _require_regular_nonreparse(path, label="initial PyInstaller producer output")
        return raw_handle, _object_identity(current)
    except BaseException:
        _close_windows_handle(raw_handle)
        raise


def _create_initial_producer_copy(
    source: str | os.PathLike[str],
    destination: pathlib.Path,
) -> tuple[Any, Any, tuple[int, int], str]:
    """Create and retain the authoritative producer object from its first byte.

    The creating handle requests READ/WRITE and shares only READ. Consequently no
    second writer or deleter can touch the producer between initial creation and
    the first trusted mutation transition. A separate zero-access no-DELETE handle
    pins the pathname except during Win32 resource commits, whose API contract
    requires all unrelated file handles to be closed before commit.
    """

    if os.name != "nt":
        raise RuntimeError("guarded PyInstaller artifact binding is Windows-only")

    import msvcrt

    kernel32 = _windows_kernel32()
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

    ctypes.set_last_error(0)
    raw_writer = create_file(
        str(destination),
        _GENERIC_READ | _GENERIC_WRITE,
        _FILE_SHARE_READ,
        None,
        _CREATE_NEW,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    writer_value = (
        raw_writer
        if isinstance(raw_writer, int)
        else ctypes.cast(raw_writer, ctypes.c_void_p).value
    )
    if writer_value in {None, _INVALID_HANDLE_VALUE}:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        descriptor = msvcrt.open_osfhandle(
            int(writer_value),
            os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        _close_windows_handle(raw_writer)
        raise

    try:
        writer = os.fdopen(descriptor, "r+b", buffering=0, closefd=True)
    except BaseException:
        os.close(descriptor)
        raise

    continuity_handle = None
    try:
        with builtins.open(source, "rb") as reader:
            while True:
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
        writer_identity = _object_identity(os.fstat(writer.fileno()))

        continuity_handle, continuity_identity = _open_delete_denial_continuity_anchor(destination)
        current = _require_regular_nonreparse(
            destination,
            label="initial PyInstaller producer output after bootloader copy",
        )
        if (
            continuity_identity != writer_identity
            or _object_identity(current) != writer_identity
        ):
            raise RuntimeError(
                "PyInstaller producer output changed during authoritative initial creation"
            )
        return writer, continuity_handle, writer_identity, _sha256_stream(writer)
    except BaseException:
        if continuity_handle is not None:
            _close_windows_handle(continuity_handle)
        writer.close()
        raise


def _open_producer_continuity_anchor(
    path: pathlib.Path,
) -> tuple[Any, tuple[int, int]]:
    """Open a read/write producer handle that denies every other writer/deleter."""

    if os.name != "nt":
        raise RuntimeError("guarded PyInstaller artifact binding is Windows-only")

    import msvcrt

    kernel32 = _windows_kernel32()
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

    ctypes.set_last_error(0)
    raw_handle = create_file(
        str(path),
        _GENERIC_READ | _GENERIC_WRITE,
        _FILE_SHARE_READ,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    handle_value = (
        raw_handle
        if isinstance(raw_handle, int)
        else ctypes.cast(raw_handle, ctypes.c_void_p).value
    )
    if handle_value in {None, _INVALID_HANDLE_VALUE}:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        descriptor = msvcrt.open_osfhandle(
            int(handle_value),
            os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        _close_windows_handle(raw_handle)
        raise

    try:
        stream = os.fdopen(descriptor, "r+b", buffering=0, closefd=True)
    except BaseException:
        os.close(descriptor)
        raise

    try:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeError(f"PyInstaller producer anchor is not a regular file: {path}")
        producer_identity = _object_identity(opened)
        after_path = _require_regular_nonreparse(path, label="PyInstaller producer output")
        if _object_identity(after_path) != producer_identity:
            raise RuntimeError(
                "PyInstaller output path changed while acquiring producer continuity anchor"
            )
        return stream, producer_identity
    except BaseException:
        stream.close()
        raise


def _open_expected_snapshot_oracle(
    path: pathlib.Path,
    *,
    label: str,
) -> tuple[Any, tuple[int, int]]:
    """Fence and bind the trusted mutator's current expected-snapshot object."""

    if os.name != "nt":
        raise RuntimeError("guarded PyInstaller artifact binding is Windows-only")

    import msvcrt

    kernel32 = _windows_kernel32()
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

    ctypes.set_last_error(0)
    raw_handle = create_file(
        str(path),
        _GENERIC_READ,
        _FILE_SHARE_READ,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    handle_value = (
        raw_handle
        if isinstance(raw_handle, int)
        else ctypes.cast(raw_handle, ctypes.c_void_p).value
    )
    if handle_value in {None, _INVALID_HANDLE_VALUE}:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        descriptor = msvcrt.open_osfhandle(
            int(handle_value),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        _close_windows_handle(raw_handle)
        raise

    try:
        stream = os.fdopen(descriptor, "rb", buffering=0, closefd=True)
    except BaseException:
        os.close(descriptor)
        raise

    try:
        opened = os.fstat(stream.fileno())
        opened_identity = _object_identity(opened)
        current = _require_regular_nonreparse(
            path,
            label=f"trusted {label} expected snapshot oracle",
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or _object_identity(current) != opened_identity
        ):
            raise RuntimeError(
                f"trusted {label} expected snapshot changed before oracle fencing"
            )
        return stream, opened_identity
    except BaseException:
        stream.close()
        raise


def _require_windows_access_denied(
    path: pathlib.Path,
    desired_access: int,
    *,
    label: str,
) -> None:
    """Prove a live native update handle or DACL rejects a competing opener."""

    kernel32 = _windows_kernel32()
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
    ctypes.set_last_error(0)
    raw_handle = create_file(
        str(path),
        desired_access,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    handle_value = (
        raw_handle
        if isinstance(raw_handle, int)
        else ctypes.cast(raw_handle, ctypes.c_void_p).value
    )
    if handle_value in {None, _INVALID_HANDLE_VALUE}:
        error = ctypes.get_last_error()
        if error in {_ERROR_ACCESS_DENIED, _ERROR_SHARING_VIOLATION}:
            return
        raise ctypes.WinError(error)
    _close_windows_handle(raw_handle)
    raise RuntimeError(f"{label} unexpectedly allowed a competing handle")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run PyInstaller and bind its exact final produced EXE before producer completion."
    )
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--bound-output", required=True)
    parser.add_argument("--digest-output", required=True)
    parser.add_argument("--verifier", required=True)
    parser.add_argument("--verifier-sha256", required=True)
    parser.add_argument("pyinstaller_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.pyinstaller_args and args.pyinstaller_args[0] == "--":
        args.pyinstaller_args = args.pyinstaller_args[1:]
    if not args.pyinstaller_args:
        parser.error("missing PyInstaller arguments after --")
    if _SHA256_RE.fullmatch(args.verifier_sha256) is None:
        parser.error("--verifier-sha256 must be canonical lowercase SHA-256")
    return args


def _invoke_trusted_verifier(
    *,
    verifier: pathlib.Path,
    verifier_sha256: str,
    artifact: pathlib.Path,
    bound_output: pathlib.Path,
    digest_output: pathlib.Path,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            _TRUSTED_VERIFIER_LAUNCHER,
            str(verifier),
            verifier_sha256,
            "--bind-artifact",
            str(artifact),
            "--bound-output",
            str(bound_output),
            "--digest-output",
            str(digest_output),
        ],
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"trusted artifact verifier exited {completed.returncode} during PyInstaller handoff"
        )


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if os.name != "nt":
        raise RuntimeError("guarded PyInstaller artifact binding requires Windows")

    artifact = pathlib.Path(args.artifact)
    bound_output = pathlib.Path(args.bound_output)
    digest_output = pathlib.Path(args.digest_output)
    verifier = pathlib.Path(args.verifier)
    artifact_key = _normalized_path(artifact)
    current_user_sid = _current_windows_user_sid()

    verifier_bytes = verifier.read_bytes()
    verifier_actual = hashlib.sha256(verifier_bytes).hexdigest()
    if verifier_actual != args.verifier_sha256:
        raise RuntimeError(
            "trusted verifier bytes changed before guarded PyInstaller handoff: "
            f"expected {args.verifier_sha256}, got {verifier_actual}"
        )

    import PyInstaller
    import PyInstaller.__main__
    import PyInstaller.building.api as building_api
    import PyInstaller.utils.misc as miscutils

    _require_expected_pyinstaller_version(PyInstaller.__version__)

    original_mtime = miscutils.mtime
    original_assemble = building_api.EXE.assemble
    original_update_checksum = building_api.winutils.update_exe_pe_checksum
    original_copyfile = shutil.copyfile
    original_remove_all_resources = building_api.winresource.remove_all_resources
    original_copy_icons = building_api.icon.CopyIcons
    original_write_version_info = building_api.versioninfo.write_version_info_to_executable
    original_copy_windows_resource = building_api.EXE._copy_windows_resource
    original_write_manifest = building_api.winmanifest.write_manifest_to_executable
    original_append_data = building_api.EXE._append_data_to_exe
    original_set_build_timestamp = building_api.winutils.set_exe_build_timestamp
    resource_win32api = building_api.winresource.win32api
    original_begin_update_resource = resource_win32api.BeginUpdateResource
    original_end_update_resource = resource_win32api.EndUpdateResource

    state: dict[str, Any] = {
        "producer_active": False,
        "creation_anchor_handle": None,
        "producer_anchor_stream": None,
        "producer_identity": None,
        "producer_progress_digest": None,
        "producer_digest": None,
        "trusted_transitions": [],
        "final_identity": None,
        "guard_stream": None,
        "guard_error": None,
        "bound": False,
        "test_pre_checksum_replacement_result": None,
        "test_pre_checksum_write_result": None,
        "test_pre_fence_write_result": None,
        "test_pre_fence_replacement_result": None,
        "test_replacement_result": None,
    }

    def poison_guard(message: str, exc: BaseException | None = None) -> RuntimeError:
        if state["guard_error"] is None:
            state["guard_error"] = message
        error = RuntimeError(str(state["guard_error"]))
        if exc is not None:
            error.__cause__ = exc
        return error

    def _make_expected_snapshot(
        anchor_stream: Any,
        label: str,
    ) -> tuple[pathlib.Path, Any, tuple[int, int], str]:
        """Create the expected copy under a retained writer from its first byte."""

        import msvcrt

        kernel32 = _windows_kernel32()
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

        snapshot = None
        raw_writer = None
        for _ in range(128):
            candidate = artifact.with_name(
                f".{artifact.name}.{label}-{secrets.token_hex(16)}.exe"
            )
            ctypes.set_last_error(0)
            candidate_handle = create_file(
                str(candidate),
                _GENERIC_READ | _GENERIC_WRITE,
                _FILE_SHARE_READ,
                None,
                _CREATE_NEW,
                _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            candidate_value = (
                candidate_handle
                if isinstance(candidate_handle, int)
                else ctypes.cast(candidate_handle, ctypes.c_void_p).value
            )
            if candidate_value not in {None, _INVALID_HANDLE_VALUE}:
                snapshot = candidate
                raw_writer = candidate_handle
                break
            error = ctypes.get_last_error()
            if error not in {_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS}:
                raise ctypes.WinError(error)
        if snapshot is None or raw_writer is None:
            raise RuntimeError(f"could not allocate trusted {label} expected snapshot")

        try:
            descriptor = msvcrt.open_osfhandle(
                int(raw_writer),
                os.O_RDWR | getattr(os, "O_BINARY", 0),
            )
        except BaseException:
            _close_windows_handle(raw_writer)
            try:
                snapshot.unlink()
            except FileNotFoundError:
                pass
            raise

        try:
            writer = os.fdopen(descriptor, "r+b", buffering=0, closefd=True)
        except BaseException:
            os.close(descriptor)
            try:
                snapshot.unlink()
            except FileNotFoundError:
                pass
            raise

        try:
            position = anchor_stream.tell()
            try:
                anchor_stream.seek(0)
                while True:
                    chunk = anchor_stream.read(1024 * 1024)
                    if not chunk:
                        break
                    writer.write(chunk)
            finally:
                anchor_stream.seek(position)
            writer.flush()
            os.fsync(writer.fileno())
            snapshot_identity = _object_identity(os.fstat(writer.fileno()))
            current = _require_regular_nonreparse(
                snapshot,
                label=f"trusted {label} expected snapshot",
            )
            if _object_identity(current) != snapshot_identity:
                raise RuntimeError(
                    f"trusted {label} expected snapshot changed during materialization"
                )
            return snapshot, writer, snapshot_identity, _sha256_stream(writer)
        except BaseException:
            writer.close()
            try:
                snapshot.unlink()
            except FileNotFoundError:
                pass
            raise

    def _trusted_byte_transition(label: str, expected_mutator, live_mutator):
        if state["guard_error"] is not None:
            raise RuntimeError(str(state["guard_error"]))

        anchor_stream = state["producer_anchor_stream"]
        producer_identity = state["producer_identity"]
        progress_digest = state["producer_progress_digest"]
        creation_anchor = state["creation_anchor_handle"]
        if (
            anchor_stream is None
            or producer_identity is None
            or progress_digest is None
            or creation_anchor is None
        ):
            raise poison_guard(
                f"PyInstaller {label} transition lacks authoritative producer continuity"
            )

        anchor_now = os.fstat(anchor_stream.fileno())
        current_path = _require_regular_nonreparse(
            artifact,
            label=f"PyInstaller producer before {label} transition",
        )
        current_digest = _sha256_stream(anchor_stream)
        if (
            _object_identity(anchor_now) != producer_identity
            or _object_identity(current_path) != producer_identity
            or current_digest != progress_digest
        ):
            raise poison_guard(
                f"PyInstaller producer bytes changed before trusted {label} transition"
            )

        snapshot, expected_stream, expected_identity, expected_input_digest = (
            _make_expected_snapshot(anchor_stream, label)
        )
        release_creation_anchor = label in _RESOURCE_API_TRANSITIONS
        oracle_stream = None
        resource_context: dict[str, Any] | None = None
        missing = object()
        original_building_open = getattr(building_api, "open", missing)
        original_winutils_open = getattr(building_api.winutils, "open", missing)
        try:
            if release_creation_anchor:
                for module in (
                    building_api.icon,
                    building_api.versioninfo,
                    building_api.winmanifest,
                ):
                    module_win32api = getattr(module, "win32api", resource_win32api)
                    if module_win32api is not resource_win32api:
                        raise poison_guard(
                            "pinned PyInstaller resource helpers no longer share one Win32 API module"
                        )

                resource_context = {
                    "stream": expected_stream,
                    "identity": expected_identity,
                    "digest": expected_input_digest,
                    "native_handle": None,
                    "begin_count": 0,
                    "end_count": 0,
                    "write_fenced": False,
                }
                expected_stream = None

                def _validate_resource_authority(phase: str) -> tuple[Any, tuple[int, int], str]:
                    assert resource_context is not None
                    authority_stream = resource_context["stream"]
                    authority_identity = resource_context["identity"]
                    authority_digest = resource_context["digest"]
                    if authority_stream is None:
                        raise poison_guard(
                            f"PyInstaller {label} expected resource authority missing at {phase}"
                        )
                    current = _require_regular_nonreparse(
                        snapshot,
                        label=f"trusted {label} expected snapshot at {phase}",
                    )
                    if (
                        _object_identity(os.fstat(authority_stream.fileno())) != authority_identity
                        or _object_identity(current) != authority_identity
                        or _sha256_stream(authority_stream) != authority_digest
                    ):
                        raise poison_guard(
                            f"PyInstaller {label} expected authority changed at {phase}"
                        )
                    return authority_stream, authority_identity, authority_digest

                def _discard_native_resource_update(native_handle: Any) -> None:
                    assert resource_context is not None
                    try:
                        original_end_update_resource(native_handle, True)
                    finally:
                        resource_context["native_handle"] = None

                def guarded_expected_begin_update_resource(
                    path,
                    *begin_args,
                    **begin_kwargs,
                ):
                    if _normalized_path(path) != _normalized_path(snapshot):
                        return original_begin_update_resource(
                            path,
                            *begin_args,
                            **begin_kwargs,
                        )
                    assert resource_context is not None
                    if resource_context["native_handle"] is not None:
                        raise poison_guard(
                            f"PyInstaller {label} nested expected resource update is unsupported"
                        )

                    authority_stream, authority_identity, authority_digest = (
                        _validate_resource_authority("before native BeginUpdateResource")
                    )
                    if resource_context["write_fenced"]:
                        try:
                            _remove_expected_snapshot_write_fence(
                                snapshot,
                                current_user_sid,
                            )
                        except BaseException as exc:
                            raise poison_guard(
                                f"PyInstaller {label} could not release the prior post-commit ACL fence under retained oracle: {exc}",
                                exc,
                            )
                        resource_context["write_fenced"] = False
                        authority_stream, authority_identity, authority_digest = (
                            _validate_resource_authority(
                                "after releasing prior post-commit ACL fence"
                            )
                        )
                    authority_stream.close()
                    resource_context["stream"] = None

                    try:
                        native_handle = original_begin_update_resource(
                            path,
                            *begin_args,
                            **begin_kwargs,
                        )
                    except BaseException as exc:
                        raise poison_guard(
                            f"PyInstaller {label} native BeginUpdateResource failed: {exc}",
                            exc,
                        )
                    resource_context["native_handle"] = native_handle
                    resource_context["begin_count"] += 1

                    try:
                        after_begin = _require_regular_nonreparse(
                            snapshot,
                            label=f"trusted {label} expected snapshot under native resource handle",
                        )
                        if _object_identity(after_begin) != authority_identity:
                            raise RuntimeError(
                                "expected snapshot identity changed before native resource handle acquired"
                            )
                        with builtins.open(snapshot, "rb", buffering=0) as reader:
                            after_begin_digest = _sha256_stream(reader)
                        if after_begin_digest != authority_digest:
                            raise RuntimeError(
                                "expected snapshot bytes changed before native resource handle acquired"
                            )
                        _require_windows_access_denied(
                            snapshot,
                            _GENERIC_WRITE,
                            label=f"PyInstaller {label} native resource write exclusion",
                        )
                        _require_windows_access_denied(
                            snapshot,
                            _DELETE_ACCESS,
                            label=f"PyInstaller {label} native resource delete exclusion",
                        )
                        _set_expected_snapshot_write_fence(snapshot, current_user_sid)
                        resource_context["write_fenced"] = True
                    except BaseException as exc:
                        try:
                            _discard_native_resource_update(native_handle)
                        finally:
                            raise poison_guard(
                                f"PyInstaller {label} native resource handle failed authority proof/fence installation: {exc}",
                                exc,
                            )

                    if os.environ.get("AUTOSPORT_TEST_WRITE_EXPECTED_DURING_RESOURCE_UPDATE") == "1":
                        try:
                            with snapshot.open("r+b") as writer:
                                writer.seek(0, os.SEEK_END)
                                writer.write(b"AUTOSPORT_EXPECTED_DURING_RESOURCE_WRITE")
                                writer.flush()
                                os.fsync(writer.fileno())
                        except OSError:
                            _discard_native_resource_update(native_handle)
                            raise RuntimeError(
                                "PyInstaller native resource update blocked hostile expected same-object write"
                            )
                        _discard_native_resource_update(native_handle)
                        raise poison_guard(
                            "PyInstaller hostile expected same-object write unexpectedly succeeded during native resource update"
                        )

                    if os.environ.get("AUTOSPORT_TEST_REPLACE_EXPECTED_DURING_RESOURCE_UPDATE") == "1":
                        replacement = snapshot.with_name(
                            f".{snapshot.name}.native-resource-replacement-{os.getpid()}"
                        )
                        try:
                            with builtins.open(snapshot, "rb") as reader, builtins.open(
                                replacement,
                                "wb",
                            ) as writer:
                                shutil.copyfileobj(reader, writer)
                                writer.write(b"AUTOSPORT_EXPECTED_DURING_RESOURCE_REPLACEMENT")
                                writer.flush()
                                os.fsync(writer.fileno())
                            try:
                                os.replace(replacement, snapshot)
                            except OSError:
                                _discard_native_resource_update(native_handle)
                                raise RuntimeError(
                                    "PyInstaller native resource update blocked hostile expected replacement"
                                )
                            _discard_native_resource_update(native_handle)
                            raise poison_guard(
                                "PyInstaller hostile expected replacement unexpectedly succeeded during native resource update"
                            )
                        finally:
                            try:
                                replacement.unlink()
                            except FileNotFoundError:
                                pass

                    return native_handle

                def guarded_expected_end_update_resource(
                    native_handle,
                    *end_args,
                    **end_kwargs,
                ):
                    assert resource_context is not None
                    if native_handle != resource_context["native_handle"]:
                        return original_end_update_resource(
                            native_handle,
                            *end_args,
                            **end_kwargs,
                        )
                    if not resource_context["write_fenced"]:
                        raise poison_guard(
                            f"PyInstaller {label} native EndUpdateResource reached commit without post-commit ACL fence"
                        )
                    if end_args:
                        discard_requested = bool(end_args[0])
                    else:
                        discard_requested = bool(end_kwargs.get("discard", False))
                    try:
                        result = original_end_update_resource(
                            native_handle,
                            *end_args,
                            **end_kwargs,
                        )
                    except BaseException as exc:
                        resource_context["native_handle"] = None
                        raise poison_guard(
                            f"PyInstaller {label} native EndUpdateResource failed: {exc}",
                            exc,
                        )
                    resource_context["native_handle"] = None
                    resource_context["end_count"] += 1
                    if discard_requested:
                        raise poison_guard(
                            f"PyInstaller {label} trusted expected resource mutation discarded its update"
                        )

                    try:
                        _require_windows_access_denied(
                            snapshot,
                            _GENERIC_WRITE,
                            label=f"PyInstaller {label} post-EndUpdateResource ACL write exclusion",
                        )
                        _require_windows_access_denied(
                            snapshot,
                            _DELETE_ACCESS,
                            label=f"PyInstaller {label} post-EndUpdateResource ACL delete exclusion",
                        )
                    except BaseException as exc:
                        raise poison_guard(
                            f"PyInstaller {label} lost write/delete exclusion after native resource commit: {exc}",
                            exc,
                        )

                    if os.environ.get("AUTOSPORT_TEST_WRITE_EXPECTED_AFTER_RESOURCE_END") == "1":
                        try:
                            with snapshot.open("r+b") as writer:
                                writer.seek(0, os.SEEK_END)
                                writer.write(b"AUTOSPORT_EXPECTED_POST_END_WRITE")
                                writer.flush()
                                os.fsync(writer.fileno())
                        except OSError:
                            raise RuntimeError(
                                "PyInstaller post-EndUpdateResource ACL fence blocked hostile expected same-object write before oracle"
                            )
                        raise poison_guard(
                            "PyInstaller hostile expected same-object write unexpectedly succeeded after native EndUpdateResource before oracle"
                        )

                    if os.environ.get("AUTOSPORT_TEST_REPLACE_EXPECTED_AFTER_RESOURCE_END") == "1":
                        replacement = snapshot.with_name(
                            f".{snapshot.name}.post-end-replacement-{os.getpid()}"
                        )
                        try:
                            with builtins.open(snapshot, "rb") as reader, builtins.open(
                                replacement,
                                "wb",
                            ) as writer:
                                shutil.copyfileobj(reader, writer)
                                writer.write(b"AUTOSPORT_EXPECTED_POST_END_REPLACEMENT")
                                writer.flush()
                                os.fsync(writer.fileno())
                            try:
                                os.replace(replacement, snapshot)
                            except OSError:
                                raise RuntimeError(
                                    "PyInstaller post-EndUpdateResource ACL fence blocked hostile expected replacement before oracle"
                                )
                            raise poison_guard(
                                "PyInstaller hostile expected replacement unexpectedly succeeded after native EndUpdateResource before oracle"
                            )
                        finally:
                            try:
                                replacement.unlink()
                            except FileNotFoundError:
                                pass

                    try:
                        next_oracle, next_identity = _open_expected_snapshot_oracle(
                            snapshot,
                            label=label,
                        )
                    except BaseException as exc:
                        raise poison_guard(
                            f"PyInstaller {label} could not fence expected resource result inside EndUpdateResource: {exc}",
                            exc,
                        )
                    resource_context["stream"] = next_oracle
                    resource_context["identity"] = next_identity
                    resource_context["digest"] = _sha256_stream(next_oracle)
                    return result

                resource_win32api.BeginUpdateResource = guarded_expected_begin_update_resource
                resource_win32api.EndUpdateResource = guarded_expected_end_update_resource
                try:
                    expected_mutator(snapshot)
                finally:
                    resource_win32api.EndUpdateResource = original_end_update_resource
                    resource_win32api.BeginUpdateResource = original_begin_update_resource

                if resource_context["native_handle"] is not None:
                    try:
                        _discard_native_resource_update(resource_context["native_handle"])
                    finally:
                        raise poison_guard(
                            f"PyInstaller {label} expected mutator returned with a native resource update still open"
                        )
                if (
                    resource_context["begin_count"] == 0
                    or resource_context["begin_count"] != resource_context["end_count"]
                ):
                    raise poison_guard(
                        f"PyInstaller {label} expected mutator bypassed the pinned Win32 resource handoff"
                    )
                oracle_stream, expected_identity, expected_digest = (
                    _validate_resource_authority("after native EndUpdateResource")
                )
                if resource_context["write_fenced"]:
                    try:
                        _remove_expected_snapshot_write_fence(
                            snapshot,
                            current_user_sid,
                        )
                    except BaseException as exc:
                        raise poison_guard(
                            f"PyInstaller {label} could not release post-commit ACL fence under retained oracle: {exc}",
                            exc,
                        )
                    resource_context["write_fenced"] = False
                    oracle_stream, expected_identity, expected_digest = (
                        _validate_resource_authority(
                            "after releasing final post-commit ACL fence under retained oracle"
                        )
                    )
                resource_context["stream"] = None
            else:
                def retained_expected_building_open(file, mode="r", *open_args, **open_kwargs):
                    if label == "append" and mode == "ab" and _normalized_path(file) == _normalized_path(snapshot):
                        if open_args or open_kwargs:
                            raise RuntimeError(
                                "unexpected arguments for pinned PyInstaller expected append"
                            )
                        return _RetainedArtifactAppender(expected_stream)
                    opener = builtins.open if original_building_open is missing else original_building_open
                    return opener(file, mode, *open_args, **open_kwargs)

                def retained_expected_winutils_open(file, mode="r", *open_args, **open_kwargs):
                    if label == "timestamp" and mode == "wb" and _normalized_path(file) == _normalized_path(snapshot):
                        if open_args or open_kwargs:
                            raise RuntimeError(
                                "unexpected arguments for pinned PyInstaller expected timestamp rewrite"
                            )
                        return _RetainedArtifactWriter(expected_stream)
                    opener = builtins.open if original_winutils_open is missing else original_winutils_open
                    return opener(file, mode, *open_args, **open_kwargs)

                if label == "append":
                    building_api.open = retained_expected_building_open
                elif label == "timestamp":
                    building_api.winutils.open = retained_expected_winutils_open
                else:
                    raise poison_guard(
                        f"PyInstaller {label} lacks a continuous expected-authority mutation adapter"
                    )
                try:
                    expected_mutator(snapshot)
                finally:
                    if label == "append":
                        if original_building_open is missing:
                            try:
                                delattr(building_api, "open")
                            except AttributeError:
                                pass
                        else:
                            building_api.open = original_building_open
                    if label == "timestamp":
                        if original_winutils_open is missing:
                            try:
                                delattr(building_api.winutils, "open")
                            except AttributeError:
                                pass
                        else:
                            building_api.winutils.open = original_winutils_open

                expected_stream.flush()
                os.fsync(expected_stream.fileno())
                expected_identity_now = _object_identity(os.fstat(expected_stream.fileno()))
                expected_path_now = _require_regular_nonreparse(
                    snapshot,
                    label=f"trusted {label} expected snapshot after retained mutation",
                )
                if (
                    expected_identity_now != expected_identity
                    or _object_identity(expected_path_now) != expected_identity
                ):
                    raise poison_guard(
                        f"PyInstaller {label} expected snapshot lost retained-object continuity"
                    )
                expected_digest = _sha256_stream(expected_stream)
                oracle_stream = expected_stream
                expected_stream = None

            if os.environ.get("AUTOSPORT_TEST_WRITE_EXPECTED_BEFORE_ORACLE") == "1":
                try:
                    with snapshot.open("r+b") as writer:
                        writer.seek(0, os.SEEK_END)
                        writer.write(b"AUTOSPORT_EXPECTED_PRE_ORACLE_WRITE")
                        writer.flush()
                        os.fsync(writer.fileno())
                except OSError:
                    raise RuntimeError(
                        "PyInstaller continuous expected authority blocked pre-publication same-object write"
                    )
                raise poison_guard(
                    "PyInstaller expected same-object write unexpectedly escaped continuous authority"
                )

            if os.environ.get("AUTOSPORT_TEST_REPLACE_EXPECTED_BEFORE_ORACLE") == "1":
                replacement = snapshot.with_name(
                    f".{snapshot.name}.pre-publication-replacement-{os.getpid()}"
                )
                try:
                    original_copyfile(snapshot, replacement)
                    try:
                        os.replace(replacement, snapshot)
                    except OSError:
                        raise RuntimeError(
                            "PyInstaller continuous expected authority blocked pre-publication replacement"
                        )
                    raise poison_guard(
                        "PyInstaller expected replacement unexpectedly escaped continuous authority"
                    )
                finally:
                    try:
                        replacement.unlink()
                    except FileNotFoundError:
                        pass

            if os.environ.get("AUTOSPORT_TEST_WRITE_EXPECTED_SNAPSHOT") == "1":
                try:
                    with snapshot.open("r+b") as writer:
                        writer.seek(0, os.SEEK_END)
                        writer.write(b"AUTOSPORT_EXPECTED_SNAPSHOT_WRITE")
                        writer.flush()
                        os.fsync(writer.fileno())
                except OSError:
                    raise RuntimeError(
                        "PyInstaller expected-snapshot same-object write blocked by retained oracle fence"
                    )
                raise poison_guard(
                    "PyInstaller expected-snapshot same-object write unexpectedly succeeded"
                )

            if os.environ.get("AUTOSPORT_TEST_REPLACE_EXPECTED_SNAPSHOT") == "1":
                replacement = snapshot.with_name(
                    f".{snapshot.name}.oracle-replacement-{os.getpid()}"
                )
                try:
                    original_copyfile(snapshot, replacement)
                    try:
                        os.replace(replacement, snapshot)
                    except OSError:
                        raise RuntimeError(
                            "PyInstaller expected-snapshot replacement blocked by retained oracle fence"
                        )
                    raise poison_guard(
                        "PyInstaller expected-snapshot replacement unexpectedly succeeded"
                    )
                finally:
                    try:
                        replacement.unlink()
                    except FileNotFoundError:
                        pass

            oracle_now = os.fstat(oracle_stream.fileno())
            oracle_path = _require_regular_nonreparse(
                snapshot,
                label=f"trusted {label} expected snapshot after authoritative digest",
            )
            if (
                _object_identity(oracle_now) != expected_identity
                or _object_identity(oracle_path) != expected_identity
                or _sha256_stream(oracle_stream) != expected_digest
            ):
                raise poison_guard(
                    f"PyInstaller {label} expected snapshot changed across authoritative digest"
                )

            anchor_stream.close()
            state["producer_anchor_stream"] = None
            if release_creation_anchor:
                _close_windows_handle(creation_anchor)
                state["creation_anchor_handle"] = None

            live_error: BaseException | None = None
            live_result = None
            try:
                live_result = live_mutator()
            except BaseException as exc:
                live_error = exc

            try:
                next_anchor, next_identity = _open_producer_continuity_anchor(artifact)
            except BaseException as exc:
                raise poison_guard(
                    f"PyInstaller {label} transition could not reacquire exclusive producer fence: {exc}",
                    exc,
                )
            state["producer_anchor_stream"] = next_anchor

            next_creation_anchor = None
            if release_creation_anchor:
                try:
                    next_creation_anchor, next_creation_identity = (
                        _open_delete_denial_continuity_anchor(artifact)
                    )
                except BaseException as exc:
                    next_anchor.close()
                    state["producer_anchor_stream"] = None
                    raise poison_guard(
                        f"PyInstaller {label} transition could not restore pathname continuity fence: {exc}",
                        exc,
                    )
                state["creation_anchor_handle"] = next_creation_anchor
                if next_creation_identity != next_identity:
                    _close_windows_handle(next_creation_anchor)
                    state["creation_anchor_handle"] = None
                    next_anchor.close()
                    state["producer_anchor_stream"] = None
                    raise poison_guard(
                        f"PyInstaller {label} transition raced while restoring producer fences"
                    )

            identity_rotated = next_identity != producer_identity
            if identity_rotated and not release_creation_anchor:
                next_anchor.close()
                state["producer_anchor_stream"] = None
                raise poison_guard(
                    f"PyInstaller {label} transition changed authoritative producer identity"
                )

            actual_digest = _sha256_stream(next_anchor)
            if live_error is not None:
                if identity_rotated or actual_digest != current_digest:
                    raise poison_guard(
                        f"PyInstaller {label} transition failed after changing producer bytes or identity",
                        live_error,
                    )
                raise live_error

            if actual_digest != expected_digest:
                raise poison_guard(
                    f"PyInstaller {label} transition produced bytes outside the trusted expected progression"
                )

            if identity_rotated:
                state["producer_identity"] = next_identity
            state["producer_progress_digest"] = actual_digest
            state["trusted_transitions"].append(label)
            return live_result
        finally:
            active_exception = sys.exc_info()[0] is not None
            cleanup_error: BaseException | None = None
            resource_win32api.EndUpdateResource = original_end_update_resource
            resource_win32api.BeginUpdateResource = original_begin_update_resource
            if resource_context is not None:
                dangling_native = resource_context.get("native_handle")
                if dangling_native is not None:
                    try:
                        original_end_update_resource(dangling_native, True)
                    except BaseException as exc:
                        cleanup_error = exc
                    finally:
                        resource_context["native_handle"] = None
                if resource_context.get("write_fenced"):
                    try:
                        _remove_expected_snapshot_write_fence(
                            snapshot,
                            current_user_sid,
                        )
                    except BaseException as exc:
                        if cleanup_error is None:
                            cleanup_error = exc
                    else:
                        resource_context["write_fenced"] = False
                dangling_stream = resource_context.get("stream")
                if dangling_stream is not None and dangling_stream is not oracle_stream:
                    dangling_stream.close()
            if expected_stream is not None:
                expected_stream.close()
            if oracle_stream is not None:
                oracle_stream.close()
            try:
                snapshot.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                if cleanup_error is None:
                    cleanup_error = exc
            if cleanup_error is not None and not active_exception:
                raise poison_guard(
                    f"PyInstaller {label} expected-snapshot authority cleanup failed: {cleanup_error}",
                    cleanup_error,
                )

    def _guarded_path_transition(label: str, original, path, *call_args, **call_kwargs):
        if not state["producer_active"] or _normalized_path(path) != artifact_key:
            return original(path, *call_args, **call_kwargs)
        return _trusted_byte_transition(
            label,
            lambda snapshot: original(str(snapshot), *call_args, **call_kwargs),
            lambda: original(path, *call_args, **call_kwargs),
        )

    def guarded_remove_all_resources(path, *call_args, **call_kwargs):
        return _guarded_path_transition(
            "remove-resources",
            original_remove_all_resources,
            path,
            *call_args,
            **call_kwargs,
        )

    def guarded_copy_icons(path, *call_args, **call_kwargs):
        return _guarded_path_transition(
            "icon",
            original_copy_icons,
            path,
            *call_args,
            **call_kwargs,
        )

    def guarded_write_version_info(path, *call_args, **call_kwargs):
        return _guarded_path_transition(
            "version-info",
            original_write_version_info,
            path,
            *call_args,
            **call_kwargs,
        )

    def guarded_copy_windows_resource(exe_self, path, *call_args, **call_kwargs):
        if not state["producer_active"] or _normalized_path(path) != artifact_key:
            return original_copy_windows_resource(exe_self, path, *call_args, **call_kwargs)
        return _trusted_byte_transition(
            "resource",
            lambda snapshot: original_copy_windows_resource(
                exe_self,
                str(snapshot),
                *call_args,
                **call_kwargs,
            ),
            lambda: original_copy_windows_resource(
                exe_self,
                path,
                *call_args,
                **call_kwargs,
            ),
        )

    def guarded_write_manifest(path, *call_args, **call_kwargs):
        return _guarded_path_transition(
            "manifest",
            original_write_manifest,
            path,
            *call_args,
            **call_kwargs,
        )

    def guarded_append_data(exe_self, path, *call_args, **call_kwargs):
        if not state["producer_active"] or _normalized_path(path) != artifact_key:
            return original_append_data(exe_self, path, *call_args, **call_kwargs)
        return _trusted_byte_transition(
            "append",
            lambda snapshot: original_append_data(
                exe_self,
                str(snapshot),
                *call_args,
                **call_kwargs,
            ),
            lambda: original_append_data(
                exe_self,
                path,
                *call_args,
                **call_kwargs,
            ),
        )

    def guarded_set_build_timestamp(path, *call_args, **call_kwargs):
        return _guarded_path_transition(
            "timestamp",
            original_set_build_timestamp,
            path,
            *call_args,
            **call_kwargs,
        )

    def guarded_copyfile(source, destination, *copy_args, **copy_kwargs):
        if not state["producer_active"] or _normalized_path(destination) != artifact_key:
            return original_copyfile(source, destination, *copy_args, **copy_kwargs)
        if state["guard_error"] is not None:
            raise RuntimeError(str(state["guard_error"]))
        if copy_args or copy_kwargs:
            raise poison_guard(
                "unexpected arguments for authoritative PyInstaller bootloader copy"
            )
        if (
            state["creation_anchor_handle"] is not None
            or state["producer_anchor_stream"] is not None
            or state["producer_identity"] is not None
        ):
            raise poison_guard("PyInstaller attempted to recreate an already-pinned producer output")

        try:
            producer_stream, continuity_handle, producer_identity, producer_digest = (
                _create_initial_producer_copy(
                    source,
                    pathlib.Path(destination),
                )
            )
        except BaseException as exc:
            raise poison_guard(f"PyInstaller authoritative initial copy failed: {exc}", exc)
        state["creation_anchor_handle"] = continuity_handle
        state["producer_anchor_stream"] = producer_stream
        state["producer_identity"] = producer_identity
        state["producer_progress_digest"] = producer_digest
        return destination

    def guarded_update_exe_pe_checksum(path, *checksum_args, **checksum_kwargs):
        if not state["producer_active"] or _normalized_path(path) != artifact_key:
            return original_update_checksum(path, *checksum_args, **checksum_kwargs)
        if state["guard_error"] is not None:
            raise RuntimeError(str(state["guard_error"]))

        creation_anchor = state["creation_anchor_handle"]
        producer_identity = state["producer_identity"]
        anchor_stream = state["producer_anchor_stream"]
        progress_digest = state["producer_progress_digest"]
        transitions = state["trusted_transitions"]
        if (
            creation_anchor is None
            or producer_identity is None
            or anchor_stream is None
            or progress_digest is None
        ):
            raise poison_guard(
                "PyInstaller reached final checksum without authoritative byte progression"
            )
        required_transitions = {"remove-resources", "manifest", "append", "timestamp"}
        if not required_transitions.issubset(set(transitions)) or transitions[-1:] != ["timestamp"]:
            raise poison_guard(
                "PyInstaller reached final checksum without the pinned 6.22.3 trusted mutation sequence"
            )

        current_before_upgrade = _require_regular_nonreparse(
            artifact,
            label="PyInstaller producer output before final checksum fence",
        )
        if (
            _object_identity(os.fstat(anchor_stream.fileno())) != producer_identity
            or _object_identity(current_before_upgrade) != producer_identity
            or _sha256_stream(anchor_stream) != progress_digest
        ):
            raise poison_guard(
                "PyInstaller producer lost authoritative byte progression before checksum"
            )

        if (
            os.environ.get("AUTOSPORT_TEST_REPLACE_BEFORE_CHECKSUM_ANCHOR") == "1"
            and state["test_pre_checksum_replacement_result"] is None
        ):
            replacement = artifact.with_name(
                f".{artifact.name}.pre-checksum-replacement-{os.getpid()}"
            )
            try:
                original_copyfile(artifact, replacement)
                with replacement.open("ab") as replacement_handle:
                    replacement_handle.write(b"AUTOSPORT_PRE_CHECKSUM_REPLACEMENT")
                try:
                    os.replace(replacement, artifact)
                except OSError:
                    state["test_pre_checksum_replacement_result"] = "blocked"
                else:
                    state["test_pre_checksum_replacement_result"] = "succeeded"
            finally:
                try:
                    replacement.unlink()
                except FileNotFoundError:
                    pass

        if (
            os.environ.get("AUTOSPORT_TEST_WRITE_BEFORE_CHECKSUM_ANCHOR") == "1"
            and state["test_pre_checksum_write_result"] is None
        ):
            try:
                with artifact.open("r+b") as writer:
                    writer.seek(0, os.SEEK_END)
                    writer.write(b"AUTOSPORT_PRE_CHECKSUM_SAME_OBJECT_WRITE")
                    writer.flush()
                    os.fsync(writer.fileno())
            except OSError:
                state["test_pre_checksum_write_result"] = "blocked"
            else:
                state["test_pre_checksum_write_result"] = "succeeded"

        missing = object()
        original_winutils_open = getattr(building_api.winutils, "open", missing)

        def retained_winutils_open(file, mode="r", *open_args, **open_kwargs):
            if mode == "wb" and _normalized_path(file) == artifact_key:
                if open_args or open_kwargs:
                    raise RuntimeError(
                        "unexpected arguments for pinned PyInstaller final checksum rewrite"
                    )
                return _RetainedArtifactWriter(anchor_stream)
            opener = builtins.open if original_winutils_open is missing else original_winutils_open
            return opener(file, mode, *open_args, **open_kwargs)

        try:
            building_api.winutils.open = retained_winutils_open
            result = original_update_checksum(path, *checksum_args, **checksum_kwargs)
        except BaseException as exc:
            if state["guard_error"] is not None:
                raise
            raise poison_guard(f"PyInstaller retained checksum rewrite failed: {exc}", exc)
        finally:
            if original_winutils_open is missing:
                try:
                    delattr(building_api.winutils, "open")
                except AttributeError:
                    pass
            else:
                building_api.winutils.open = original_winutils_open

        try:
            anchor_after = os.fstat(anchor_stream.fileno())
            current_after = _require_regular_nonreparse(
                artifact,
                label="PyInstaller producer output after final checksum",
            )
            if (
                _object_identity(anchor_after) != producer_identity
                or _object_identity(current_after) != producer_identity
            ):
                raise RuntimeError(
                    "PyInstaller producer output lost continuity during final checksum"
                )
            state["producer_digest"] = _sha256_stream(anchor_stream)
            state["producer_progress_digest"] = state["producer_digest"]
            state["trusted_transitions"].append("checksum")
            return result
        except BaseException as exc:
            raise poison_guard(f"PyInstaller final checksum binding failed: {exc}", exc)

    def guarded_mtime(path):
        if (
            state["producer_active"]
            and _normalized_path(path) == artifact_key
            and state["guard_stream"] is None
        ):
            if state["guard_error"] is not None:
                raise RuntimeError(str(state["guard_error"]))

            anchor_stream = state["producer_anchor_stream"]
            producer_identity = state["producer_identity"]
            producer_digest = state["producer_digest"]
            if anchor_stream is None or producer_identity is None or producer_digest is None:
                raise poison_guard(
                    "PyInstaller reached final fence without a byte-bound producer anchor"
                )

            if (
                os.environ.get("AUTOSPORT_TEST_WRITE_BEFORE_FINAL_FENCE") == "1"
                and state["test_pre_fence_write_result"] is None
            ):
                try:
                    with artifact.open("r+b") as writer:
                        writer.seek(0, os.SEEK_END)
                        writer.write(b"AUTOSPORT_PRE_FENCE_SAME_OBJECT_WRITE")
                        writer.flush()
                        os.fsync(writer.fileno())
                except OSError:
                    state["test_pre_fence_write_result"] = "blocked"
                else:
                    state["test_pre_fence_write_result"] = "succeeded"

            if (
                os.environ.get("AUTOSPORT_TEST_REPLACE_BEFORE_FINAL_FENCE") == "1"
                and state["test_pre_fence_replacement_result"] is None
            ):
                replacement = artifact.with_name(
                    f".{artifact.name}.pre-fence-replacement-{os.getpid()}"
                )
                try:
                    original_copyfile(artifact, replacement)
                    with replacement.open("ab") as replacement_handle:
                        replacement_handle.write(b"AUTOSPORT_PRE_FENCE_REPLACEMENT")
                    try:
                        os.replace(replacement, artifact)
                    except OSError:
                        state["test_pre_fence_replacement_result"] = "blocked"
                    else:
                        state["test_pre_fence_replacement_result"] = "succeeded"
                finally:
                    try:
                        replacement.unlink()
                    except FileNotFoundError:
                        pass

            try:
                anchor_now = os.fstat(anchor_stream.fileno())
                current_path = _require_regular_nonreparse(
                    artifact,
                    label="final PyInstaller output",
                )
                if (
                    _object_identity(anchor_now) != producer_identity
                    or _object_identity(current_path) != producer_identity
                    or _sha256_stream(anchor_stream) != producer_digest
                ):
                    raise RuntimeError(
                        "final PyInstaller bytes do not match the retained producer output"
                    )
            except BaseException as exc:
                raise poison_guard(
                    f"PyInstaller final fence integrity failure: {exc}",
                    exc,
                )

            state["guard_stream"] = anchor_stream
            state["final_identity"] = producer_identity

            if (
                os.environ.get("AUTOSPORT_TEST_REPLACE_PYINSTALLER_OUTPUT") == "1"
                and state["test_replacement_result"] is None
            ):
                replacement = artifact.with_name(
                    f".{artifact.name}.replacement-{os.getpid()}"
                )
                try:
                    original_copyfile(artifact, replacement)
                    try:
                        os.replace(replacement, artifact)
                    except OSError:
                        state["test_replacement_result"] = "blocked"
                    else:
                        state["test_replacement_result"] = "succeeded"
                finally:
                    try:
                        replacement.unlink()
                    except FileNotFoundError:
                        pass

            return os.fstat(anchor_stream.fileno()).st_mtime
        if state["guard_error"] is not None and state["producer_active"]:
            raise RuntimeError(str(state["guard_error"]))
        return original_mtime(path)

    def guarded_assemble(self):
        is_target = _normalized_path(self.name) == artifact_key
        if not is_target:
            return original_assemble(self)
        if state["producer_active"]:
            raise RuntimeError("guarded PyInstaller producer re-entered unexpectedly")

        state["producer_active"] = True
        try:
            result = original_assemble(self)
        finally:
            state["producer_active"] = False

        if state["guard_error"] is not None:
            raise RuntimeError(str(state["guard_error"]))

        if os.environ.get("AUTOSPORT_TEST_REPLACE_BEFORE_CHECKSUM_ANCHOR") == "1":
            replacement_result = state["test_pre_checksum_replacement_result"]
            if replacement_result == "blocked":
                raise RuntimeError(
                    "PyInstaller pre-checksum output replacement blocked by creation continuity anchor"
                )
            if replacement_result == "succeeded":
                raise RuntimeError(
                    "PyInstaller output replacement unexpectedly succeeded before checksum fence"
                )
            raise RuntimeError(
                "PyInstaller pre-checksum replacement test hook was not exercised"
            )

        if os.environ.get("AUTOSPORT_TEST_WRITE_BEFORE_CHECKSUM_ANCHOR") == "1":
            write_result = state["test_pre_checksum_write_result"]
            if write_result == "blocked":
                raise RuntimeError(
                    "PyInstaller pre-checksum same-object write blocked by authoritative producer byte progression"
                )
            if write_result == "succeeded":
                raise RuntimeError(
                    "PyInstaller same-object write unexpectedly succeeded before checksum anchor"
                )
            raise RuntimeError("PyInstaller pre-checksum write test hook was not exercised")

        if os.environ.get("AUTOSPORT_TEST_WRITE_BEFORE_FINAL_FENCE") == "1":
            write_result = state["test_pre_fence_write_result"]
            if write_result == "blocked":
                raise RuntimeError(
                    "PyInstaller same-object write blocked by retained producer artifact fence"
                )
            if write_result == "succeeded":
                raise RuntimeError(
                    "PyInstaller same-object write unexpectedly succeeded before trusted bind"
                )
            raise RuntimeError("PyInstaller pre-fence write test hook was not exercised")

        if os.environ.get("AUTOSPORT_TEST_REPLACE_BEFORE_FINAL_FENCE") == "1":
            replacement_result = state["test_pre_fence_replacement_result"]
            if replacement_result == "blocked":
                raise RuntimeError(
                    "PyInstaller output replacement blocked by producer continuity anchor"
                )
            if replacement_result == "succeeded":
                raise RuntimeError(
                    "PyInstaller output replacement unexpectedly succeeded before final artifact fence"
                )
            raise RuntimeError(
                "PyInstaller pre-fence replacement test hook was not exercised"
            )

        if os.environ.get("AUTOSPORT_TEST_REPLACE_PYINSTALLER_OUTPUT") == "1":
            replacement_result = state["test_replacement_result"]
            if replacement_result == "blocked":
                raise RuntimeError(
                    "PyInstaller output replacement blocked by retained final artifact fence"
                )
            if replacement_result == "succeeded":
                raise RuntimeError(
                    "PyInstaller output replacement unexpectedly succeeded after final artifact fence"
                )
            raise RuntimeError(
                "PyInstaller output replacement test hook was not exercised at final artifact fence"
            )

        producer_identity = state["producer_identity"]
        producer_digest = state["producer_digest"]
        anchor_stream = state["producer_anchor_stream"]
        final_identity = state["final_identity"]
        guard_stream = state["guard_stream"]
        if producer_identity is None or producer_digest is None or anchor_stream is None:
            raise RuntimeError(
                "PyInstaller producer completed without retained byte-bound producer anchor"
            )
        if final_identity is None or guard_stream is None:
            raise RuntimeError(
                "PyInstaller producer completed without retained final artifact identity fence"
            )

        anchor_before = os.fstat(anchor_stream.fileno())
        opened_before = os.fstat(guard_stream.fileno())
        if (
            _object_identity(anchor_before) != producer_identity
            or _object_identity(opened_before) != final_identity
            or final_identity != producer_identity
            or _sha256_stream(anchor_stream) != producer_digest
        ):
            raise RuntimeError(
                "retained PyInstaller producer/final artifact bytes changed before trusted bind"
            )
        current_path = _require_regular_nonreparse(
            artifact,
            label="PyInstaller output before trusted bind",
        )
        if _object_identity(current_path) != final_identity:
            raise RuntimeError("PyInstaller output pathname no longer names the final produced object")

        _invoke_trusted_verifier(
            verifier=verifier,
            verifier_sha256=args.verifier_sha256,
            artifact=artifact,
            bound_output=bound_output,
            digest_output=digest_output,
        )

        anchor_after = os.fstat(anchor_stream.fileno())
        opened_after = os.fstat(guard_stream.fileno())
        current_after = _require_regular_nonreparse(
            artifact,
            label="PyInstaller output after trusted bind",
        )
        if (
            _object_identity(anchor_after) != producer_identity
            or _stable_identity(opened_after) != _stable_identity(opened_before)
            or _object_identity(current_after) != final_identity
            or _sha256_stream(anchor_stream) != producer_digest
        ):
            raise RuntimeError("PyInstaller output changed across trusted artifact binding")
        state["bound"] = True
        return result

    building_api.shutil.copyfile = guarded_copyfile
    building_api.winresource.remove_all_resources = guarded_remove_all_resources
    building_api.icon.CopyIcons = guarded_copy_icons
    building_api.versioninfo.write_version_info_to_executable = guarded_write_version_info
    building_api.EXE._copy_windows_resource = guarded_copy_windows_resource
    building_api.winmanifest.write_manifest_to_executable = guarded_write_manifest
    building_api.EXE._append_data_to_exe = guarded_append_data
    building_api.winutils.set_exe_build_timestamp = guarded_set_build_timestamp
    miscutils.mtime = guarded_mtime
    building_api.winutils.update_exe_pe_checksum = guarded_update_exe_pe_checksum
    building_api.EXE.assemble = guarded_assemble
    try:
        PyInstaller.__main__.run(pyi_args=list(args.pyinstaller_args))
        if not state["bound"]:
            raise RuntimeError("PyInstaller exited without binding the exact final produced artifact")
        digest = digest_output.read_text(encoding="utf-8").strip()
        if _SHA256_RE.fullmatch(digest) is None:
            raise RuntimeError("trusted artifact verifier emitted invalid digest")
        return 0
    finally:
        resource_win32api.EndUpdateResource = original_end_update_resource
        resource_win32api.BeginUpdateResource = original_begin_update_resource
        building_api.EXE.assemble = original_assemble
        building_api.winutils.update_exe_pe_checksum = original_update_checksum
        miscutils.mtime = original_mtime
        building_api.winutils.set_exe_build_timestamp = original_set_build_timestamp
        building_api.EXE._append_data_to_exe = original_append_data
        building_api.winmanifest.write_manifest_to_executable = original_write_manifest
        building_api.EXE._copy_windows_resource = original_copy_windows_resource
        building_api.versioninfo.write_version_info_to_executable = original_write_version_info
        building_api.icon.CopyIcons = original_copy_icons
        building_api.winresource.remove_all_resources = original_remove_all_resources
        building_api.shutil.copyfile = original_copyfile
        guard_stream = state.get("guard_stream")
        anchor_stream = state.get("producer_anchor_stream")
        creation_anchor = state.get("creation_anchor_handle")
        if guard_stream is not None and guard_stream is not anchor_stream:
            guard_stream.close()
        if anchor_stream is not None:
            anchor_stream.close()
        if creation_anchor is not None:
            _close_windows_handle(creation_anchor)


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except Exception as exc:
        print(f"guarded PyInstaller handoff failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
