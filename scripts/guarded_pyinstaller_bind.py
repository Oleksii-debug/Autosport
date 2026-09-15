from __future__ import annotations

import builtins
import ctypes
import importlib.util
import os
import pathlib
import shutil
import stat
import subprocess
import sys
from ctypes import wintypes
from typing import Any


_CORE_PATH = pathlib.Path(__file__).with_name("guarded_pyinstaller_bind_core.py")
_CORE_SPEC = importlib.util.spec_from_file_location(
    "_autosport_guarded_pyinstaller_bind_core",
    _CORE_PATH,
)
if _CORE_SPEC is None or _CORE_SPEC.loader is None:
    raise RuntimeError(f"could not load guarded PyInstaller core: {_CORE_PATH}")
_CORE = importlib.util.module_from_spec(_CORE_SPEC)
sys.modules[_CORE_SPEC.name] = _CORE
_CORE_SPEC.loader.exec_module(_CORE)

# Public compatibility surface used by the pinned-version regression.
_EXPECTED_PYINSTALLER_VERSION = _CORE._EXPECTED_PYINSTALLER_VERSION
_require_expected_pyinstaller_version = _CORE._require_expected_pyinstaller_version

_CREATE_ALWAYS = 2
_FILE_DELETE_CHILD = 0x00000040
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_DACL_SECURITY_INFORMATION = 0x00000004
_ERROR_INSUFFICIENT_BUFFER = 122


class _RetainedPackageWriter:
    """Let CArchiveWriter finish without releasing the producer-authority handle."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def __enter__(self) -> Any:
        return self._stream

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._stream.flush()
        os.fsync(self._stream.fileno())
        return False


def _open_retained_package_writer(path: pathlib.Path) -> tuple[Any, tuple[int, int]]:
    """Create/truncate a PKG while denying every competing writer/deleter."""

    if os.name != "nt":
        raise RuntimeError("guarded PyInstaller package authority requires Windows")

    import msvcrt

    kernel32 = _CORE._windows_kernel32()
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
        _CORE._GENERIC_READ | _CORE._GENERIC_WRITE,
        _CORE._FILE_SHARE_READ,
        None,
        _CREATE_ALWAYS,
        _CORE._FILE_ATTRIBUTE_NORMAL | _CORE._FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    handle_value = (
        raw_handle
        if isinstance(raw_handle, int)
        else ctypes.cast(raw_handle, ctypes.c_void_p).value
    )
    if handle_value in {None, _CORE._INVALID_HANDLE_VALUE}:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        descriptor = msvcrt.open_osfhandle(
            int(handle_value),
            os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        _CORE._close_windows_handle(raw_handle)
        raise

    try:
        stream = os.fdopen(descriptor, "r+b", buffering=0, closefd=True)
    except BaseException:
        os.close(descriptor)
        raise

    try:
        opened = os.fstat(stream.fileno())
        current = _CORE._require_regular_nonreparse(
            path,
            label="PyInstaller PKG producer output",
        )
        identity = _CORE._object_identity(opened)
        if _CORE._object_identity(current) != identity:
            raise RuntimeError("PyInstaller PKG path changed while acquiring producer authority")
        return stream, identity
    except BaseException:
        stream.close()
        raise


def _validate_package_authority(authority: dict[str, Any], *, phase: str) -> None:
    stream = authority["stream"]
    path = authority["path"]
    identity = authority["identity"]
    digest = authority["digest"]
    current = _CORE._require_regular_nonreparse(
        path,
        label=f"PyInstaller PKG producer output at {phase}",
    )
    if (
        _CORE._object_identity(os.fstat(stream.fileno())) != identity
        or _CORE._object_identity(current) != identity
        or _CORE._sha256_stream(stream) != digest
    ):
        raise RuntimeError(f"PyInstaller PKG producer authority changed at {phase}")


def _run_package_hostile_probes(authority: dict[str, Any]) -> None:
    if authority["probe_exercised"]:
        return
    authority["probe_exercised"] = True
    path: pathlib.Path = authority["path"]
    stream = authority["stream"]

    if os.environ.get("AUTOSPORT_TEST_WRITE_PKG_BEFORE_APPEND") == "1":
        try:
            with path.open("r+b") as writer:
                writer.seek(0, os.SEEK_END)
                writer.write(b"AUTOSPORT_HOSTILE_PKG_WRITE")
                writer.flush()
                os.fsync(writer.fileno())
        except OSError:
            raise RuntimeError(
                "PyInstaller PKG producer fence blocked hostile same-object write before append"
            )
        raise RuntimeError(
            "PyInstaller hostile PKG same-object write unexpectedly succeeded before append"
        )

    if os.environ.get("AUTOSPORT_TEST_REPLACE_PKG_BEFORE_APPEND") == "1":
        replacement = path.with_name(f".{path.name}.hostile-replacement-{os.getpid()}")
        position = stream.tell()
        try:
            stream.seek(0)
            with builtins.open(replacement, "wb") as writer:
                shutil.copyfileobj(stream, writer)
                writer.write(b"AUTOSPORT_HOSTILE_PKG_REPLACEMENT")
                writer.flush()
                os.fsync(writer.fileno())
            try:
                os.replace(replacement, path)
            except OSError:
                raise RuntimeError(
                    "PyInstaller PKG producer fence blocked hostile same-path replacement before append"
                )
            raise RuntimeError(
                "PyInstaller hostile PKG same-path replacement unexpectedly succeeded before append"
            )
        finally:
            stream.seek(position)
            try:
                replacement.unlink()
            except FileNotFoundError:
                pass


def _require_regular_directory_nonreparse(path: pathlib.Path, *, label: str) -> None:
    try:
        value = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"{label} is not readable: {path}") from exc
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise RuntimeError(f"{label} must be a regular non-symlink directory: {path}")
    if _CORE._windows_attributes(path) & _CORE._FILE_ATTRIBUTE_REPARSE_POINT:
        raise RuntimeError(f"{label} must not be a Windows reparse point: {path}")


def _directory_delete_child_available(path: pathlib.Path) -> bool:
    """Return whether this token can acquire parent FILE_DELETE_CHILD authority."""

    kernel32 = _CORE._windows_kernel32()
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
        _FILE_DELETE_CHILD,
        _CORE._FILE_SHARE_READ | _CORE._FILE_SHARE_WRITE | _CORE._FILE_SHARE_DELETE,
        None,
        _CORE._OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _CORE._FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    handle_value = (
        raw_handle
        if isinstance(raw_handle, int)
        else ctypes.cast(raw_handle, ctypes.c_void_p).value
    )
    if handle_value in {None, _CORE._INVALID_HANDLE_VALUE}:
        error = ctypes.get_last_error()
        if error == _CORE._ERROR_ACCESS_DENIED:
            return False
        raise ctypes.WinError(error)
    _CORE._close_windows_handle(raw_handle)
    return True


def _capture_windows_dacl(path: pathlib.Path) -> bytes:
    """Capture the exact parent DACL so the temporary namespace deny is reversible."""

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    get_file_security = advapi32.GetFileSecurityW
    get_file_security.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    get_file_security.restype = wintypes.BOOL

    needed = wintypes.DWORD(0)
    ctypes.set_last_error(0)
    if get_file_security(
        str(path),
        _DACL_SECURITY_INFORMATION,
        None,
        0,
        ctypes.byref(needed),
    ):
        raise RuntimeError("unexpected zero-length parent DACL capture")
    error = ctypes.get_last_error()
    if error != _ERROR_INSUFFICIENT_BUFFER or needed.value == 0:
        raise ctypes.WinError(error)

    buffer = ctypes.create_string_buffer(needed.value)
    if not get_file_security(
        str(path),
        _DACL_SECURITY_INFORMATION,
        ctypes.cast(buffer, ctypes.c_void_p),
        needed.value,
        ctypes.byref(needed),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return bytes(buffer.raw[: needed.value])


def _restore_windows_dacl(path: pathlib.Path, descriptor: bytes) -> None:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    set_file_security = advapi32.SetFileSecurityW
    set_file_security.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
    )
    set_file_security.restype = wintypes.BOOL
    buffer = ctypes.create_string_buffer(descriptor, len(descriptor))
    if not set_file_security(
        str(path),
        _DACL_SECURITY_INFORMATION,
        ctypes.cast(buffer, ctypes.c_void_p),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def _install_expected_snapshot_namespace_fence(
    path: pathlib.Path,
    sid: str,
) -> dict[str, Any]:
    """Deny the parent FILE_DELETE_CHILD alternative across resource commit return."""

    parent = path.parent
    _require_regular_directory_nonreparse(
        parent,
        label="trusted expected-snapshot parent namespace",
    )
    parent_dacl = _capture_windows_dacl(parent)
    had_delete_child = _directory_delete_child_available(parent)

    icacls = _CORE._windows_system_binary("icacls.exe")
    completed = subprocess.run(
        [str(icacls), str(parent), "/deny", f"*{sid}:(DC)", "/Q"],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if completed.returncode != 0:
        try:
            _restore_windows_dacl(parent, parent_dacl)
        except BaseException as exc:
            raise RuntimeError(
                "trusted expected-snapshot parent namespace fence could not be installed "
                "and the original parent DACL could not be restored: "
                f"icacls exited {completed.returncode}"
            ) from exc
        raise RuntimeError(
            "trusted expected-snapshot parent namespace fence could not be installed: "
            f"icacls exited {completed.returncode}"
        )
    try:
        if _directory_delete_child_available(parent):
            raise RuntimeError(
                "trusted expected-snapshot parent namespace fence still allows FILE_DELETE_CHILD"
            )
        if (
            os.environ.get("AUTOSPORT_TEST_REQUIRE_PARENT_DELETE_CHILD_AUTHORITY") == "1"
            and not had_delete_child
        ):
            raise RuntimeError(
                "expected-snapshot parent lacked FILE_DELETE_CHILD before adversarial namespace fence"
            )
    except BaseException:
        _restore_windows_dacl(parent, parent_dacl)
        raise

    return {
        "parent": parent,
        "parent_dacl": parent_dacl,
        "had_delete_child": had_delete_child,
        "parent_fenced": True,
    }


def run(argv: list[str] | None = None) -> int:
    if os.name != "nt":
        return _CORE.run(argv)

    import PyInstaller
    import PyInstaller.archive.writers as archive_writers
    import PyInstaller.building.api as building_api

    _require_expected_pyinstaller_version(PyInstaller.__version__)

    original_carchive_writer = building_api.CArchiveWriter
    original_append_data = building_api.EXE._append_data_to_exe
    missing = object()
    original_archive_open = getattr(archive_writers, "open", missing)

    original_set_fence = _CORE._set_expected_snapshot_write_fence
    original_remove_fence = _CORE._remove_expected_snapshot_write_fence
    original_require_access_denied = _CORE._require_windows_access_denied

    package_authorities: dict[str, dict[str, Any]] = {}
    expected_resource_fences: dict[str, dict[str, Any]] = {}

    def guarded_set_expected_snapshot_write_fence(path: pathlib.Path, sid: str) -> None:
        path = pathlib.Path(path)
        key = _CORE._normalized_path(path)
        existing = expected_resource_fences.get(key)
        if existing is not None:
            if existing["sid"] != sid:
                raise RuntimeError("expected-snapshot ACL fence SID changed unexpectedly")
            return

        namespace = _install_expected_snapshot_namespace_fence(path, sid)
        record = {
            "path": path,
            "sid": sid,
            **namespace,
            "file_fenced": False,
        }
        expected_resource_fences[key] = record
        try:
            original_set_fence(path, sid)
            record["file_fenced"] = True
        except BaseException:
            try:
                if record["parent_fenced"]:
                    _restore_windows_dacl(record["parent"], record["parent_dacl"])
                    record["parent_fenced"] = False
            finally:
                expected_resource_fences.pop(key, None)
            raise

    def guarded_remove_expected_snapshot_write_fence(path: pathlib.Path, sid: str) -> None:
        key = _CORE._normalized_path(path)
        existing = expected_resource_fences.get(key)
        if existing is None:
            raise RuntimeError("expected-snapshot ACL fence removal lacked installed authority")
        if existing["sid"] != sid:
            raise RuntimeError("expected-snapshot ACL fence removal SID mismatch")

        if existing["file_fenced"]:
            original_remove_fence(path, sid)
            existing["file_fenced"] = False
        if existing["parent_fenced"]:
            _restore_windows_dacl(existing["parent"], existing["parent_dacl"])
            existing["parent_fenced"] = False
        expected_resource_fences.pop(key, None)

    def guarded_require_windows_access_denied(
        path: pathlib.Path,
        desired_access: int,
        *,
        label: str,
    ) -> None:
        # BeginUpdateResource does not itself promise CreateFile sharing exclusion.
        # Install both object and parent-namespace fences before the first assertion
        # that depends on them, then keep both through EndUpdateResource and until
        # the retained post-commit oracle has been opened.
        if "native resource" in label and "post-EndUpdateResource" not in label:
            key = _CORE._normalized_path(path)
            if key not in expected_resource_fences:
                guarded_set_expected_snapshot_write_fence(
                    pathlib.Path(path),
                    _CORE._current_windows_user_sid(),
                )
        original_require_access_denied(path, desired_access, label=label)

    def guarded_carchive_writer(filename, *writer_args, **writer_kwargs):
        pkg_path = pathlib.Path(filename)
        pkg_key = _CORE._normalized_path(pkg_path)
        if pkg_key in package_authorities:
            raise RuntimeError("PyInstaller attempted to rebuild an already producer-bound PKG")

        retained_stream = None
        retained_identity = None

        def guarded_archive_open(file, mode="r", *open_args, **open_kwargs):
            nonlocal retained_stream, retained_identity
            if _CORE._normalized_path(file) == pkg_key and mode == "wb":
                if open_args or open_kwargs:
                    raise RuntimeError("unexpected arguments for authoritative PKG producer open")
                if retained_stream is not None:
                    raise RuntimeError("PyInstaller PKG producer reopened its authoritative output")
                retained_stream, retained_identity = _open_retained_package_writer(pkg_path)
                return _RetainedPackageWriter(retained_stream)
            opener = builtins.open if original_archive_open is missing else original_archive_open
            return opener(file, mode, *open_args, **open_kwargs)

        archive_writers.open = guarded_archive_open
        try:
            result = original_carchive_writer(filename, *writer_args, **writer_kwargs)
        except BaseException:
            if retained_stream is not None:
                retained_stream.close()
            raise
        finally:
            if original_archive_open is missing:
                try:
                    delattr(archive_writers, "open")
                except AttributeError:
                    pass
            else:
                archive_writers.open = original_archive_open

        if retained_stream is None or retained_identity is None:
            raise RuntimeError("PyInstaller CArchiveWriter bypassed authoritative PKG output open")
        current = _CORE._require_regular_nonreparse(
            pkg_path,
            label="completed PyInstaller PKG producer output",
        )
        if (
            _CORE._object_identity(os.fstat(retained_stream.fileno())) != retained_identity
            or _CORE._object_identity(current) != retained_identity
        ):
            retained_stream.close()
            raise RuntimeError("PyInstaller PKG identity changed at producer completion")

        package_authorities[pkg_key] = {
            "path": pkg_path,
            "stream": retained_stream,
            "identity": retained_identity,
            "digest": _CORE._sha256_stream(retained_stream),
            "uses": 0,
            "probe_exercised": False,
        }
        return result

    def guarded_append_data(exe_self, build_name, append_file):
        pkg_key = _CORE._normalized_path(append_file)
        authority = package_authorities.get(pkg_key)
        if authority is None:
            raise RuntimeError(
                "PyInstaller append reached release EXE without producer-bound PKG authority"
            )
        if authority["uses"] >= 2:
            raise RuntimeError("PyInstaller PKG authority was consumed more than expected")

        _validate_package_authority(authority, phase="before append consumption")
        _run_package_hostile_probes(authority)

        stream = authority["stream"]
        position = stream.tell()
        try:
            stream.seek(0)
            # Exact pinned 6.22.3 semantics, except PKG bytes come from the retained
            # producer handle rather than reopening a mutable pathname.
            opener = getattr(building_api, "open", builtins.open)
            with opener(build_name, "ab") as outf:
                shutil.copyfileobj(stream, outf, length=64 * 1024)
        finally:
            stream.seek(position)

        _validate_package_authority(authority, phase="after append consumption")
        authority["uses"] += 1
        if authority["uses"] == 2:
            stream.close()
            package_authorities.pop(pkg_key, None)
        return None

    _CORE._set_expected_snapshot_write_fence = guarded_set_expected_snapshot_write_fence
    _CORE._remove_expected_snapshot_write_fence = guarded_remove_expected_snapshot_write_fence
    _CORE._require_windows_access_denied = guarded_require_windows_access_denied
    building_api.CArchiveWriter = guarded_carchive_writer
    building_api.EXE._append_data_to_exe = guarded_append_data

    active_exception = False
    try:
        result = _CORE.run(argv)
        if package_authorities:
            pending = ", ".join(sorted(package_authorities))
            raise RuntimeError(f"PyInstaller completed with unconsumed producer-bound PKG authority: {pending}")
        if expected_resource_fences:
            pending = ", ".join(sorted(expected_resource_fences))
            raise RuntimeError(f"PyInstaller completed with unreleased expected-snapshot ACL fence: {pending}")
        return result
    except BaseException:
        active_exception = True
        raise
    finally:
        building_api.EXE._append_data_to_exe = original_append_data
        building_api.CArchiveWriter = original_carchive_writer
        _CORE._require_windows_access_denied = original_require_access_denied
        _CORE._remove_expected_snapshot_write_fence = original_remove_fence
        _CORE._set_expected_snapshot_write_fence = original_set_fence

        cleanup_errors: list[str] = []
        for authority in list(package_authorities.values()):
            try:
                authority["stream"].close()
            except BaseException as exc:
                cleanup_errors.append(f"PKG authority close failed: {exc}")
        package_authorities.clear()

        for record in list(expected_resource_fences.values()):
            try:
                if record["file_fenced"] and record["path"].exists():
                    original_remove_fence(record["path"], record["sid"])
                    record["file_fenced"] = False
            except BaseException as exc:
                cleanup_errors.append(
                    f"expected-snapshot file ACL cleanup failed for {record['path']}: {exc}"
                )
            try:
                if record["parent_fenced"] and record["parent"].exists():
                    _restore_windows_dacl(record["parent"], record["parent_dacl"])
                    record["parent_fenced"] = False
            except BaseException as exc:
                cleanup_errors.append(
                    f"expected-snapshot parent DACL cleanup failed for {record['parent']}: {exc}"
                )
        expected_resource_fences.clear()

        if cleanup_errors and not active_exception:
            raise RuntimeError("; ".join(cleanup_errors))


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except Exception as exc:
        print(f"guarded PyInstaller handoff failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())