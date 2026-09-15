from __future__ import annotations

import argparse
import builtins
import ctypes
import hashlib
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
from ctypes import wintypes
from typing import Any

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_EXPECTED_PYINSTALLER_VERSION = "6.22.3"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_CREATE_NEW = 1
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

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


def _open_delete_denial_continuity_anchor(
    path: pathlib.Path,
) -> tuple[Any, tuple[int, int]]:
    """Pin the producer pathname without interfering with its legitimate reads/writes.

    The zero-access handle shares READ/WRITE but deliberately not DELETE. It can
    therefore remain open across PyInstaller's resource/appending mutations while
    preventing rename/delete replacement of the object established at creation.
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
) -> tuple[Any, tuple[int, int]]:
    """Create the first producer object and pin it before its creating handle closes."""

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
        _GENERIC_WRITE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
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
            os.O_WRONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        _close_windows_handle(raw_writer)
        raise

    continuity_handle = None
    try:
        with os.fdopen(descriptor, "wb", buffering=0, closefd=True) as writer:
            with builtins.open(source, "rb") as reader:
                while True:
                    chunk = reader.read(1024 * 1024)
                    if not chunk:
                        break
                    writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
            writer_identity = _object_identity(os.fstat(writer.fileno()))

            # Open the no-delete continuity anchor while the creating writer is
            # still alive, so there is no pathname-replacement interval at all.
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
        return continuity_handle, writer_identity
    except BaseException:
        if continuity_handle is not None:
            _close_windows_handle(continuity_handle)
        raise


def _open_producer_continuity_anchor(
    path: pathlib.Path,
) -> tuple[Any, tuple[int, int]]:
    """Upgrade continuity to one read/write handle that denies every second writer/deleter.

    The earlier zero-access handle has already pinned the object from its creation.
    Opening this handle while that no-delete anchor remains alive atomically upgrades
    the boundary for PyInstaller 6.22.3's final checksum rewrite: any already-open or
    later foreign WRITE/DELETE handle makes this acquisition fail closed.
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
    state: dict[str, Any] = {
        "producer_active": False,
        "creation_anchor_handle": None,
        "producer_anchor_stream": None,
        "producer_identity": None,
        "producer_digest": None,
        "final_identity": None,
        "guard_stream": None,
        "guard_error": None,
        "bound": False,
        "test_pre_checksum_replacement_result": None,
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

    def guarded_copyfile(source, destination, *copy_args, **copy_kwargs):
        if not state["producer_active"] or _normalized_path(destination) != artifact_key:
            return original_copyfile(source, destination, *copy_args, **copy_kwargs)
        if state["guard_error"] is not None:
            raise RuntimeError(str(state["guard_error"]))
        if copy_args or copy_kwargs:
            raise poison_guard(
                "unexpected arguments for authoritative PyInstaller bootloader copy"
            )
        if state["creation_anchor_handle"] is not None or state["producer_identity"] is not None:
            raise poison_guard("PyInstaller attempted to recreate an already-pinned producer output")

        try:
            continuity_handle, producer_identity = _create_initial_producer_copy(
                source,
                pathlib.Path(destination),
            )
        except BaseException as exc:
            raise poison_guard(f"PyInstaller authoritative initial copy failed: {exc}", exc)
        state["creation_anchor_handle"] = continuity_handle
        state["producer_identity"] = producer_identity
        return destination

    def guarded_update_exe_pe_checksum(path, *checksum_args, **checksum_kwargs):
        if not state["producer_active"] or _normalized_path(path) != artifact_key:
            return original_update_checksum(path, *checksum_args, **checksum_kwargs)
        if state["guard_error"] is not None:
            raise RuntimeError(str(state["guard_error"]))

        creation_anchor = state["creation_anchor_handle"]
        producer_identity = state["producer_identity"]
        if creation_anchor is None or producer_identity is None:
            raise poison_guard(
                "PyInstaller reached final checksum without authoritative creation continuity"
            )

        current_before_upgrade = _require_regular_nonreparse(
            artifact,
            label="PyInstaller producer output before final checksum fence",
        )
        if _object_identity(current_before_upgrade) != producer_identity:
            raise poison_guard(
                "PyInstaller producer pathname lost authoritative creation identity before checksum"
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

        anchor_stream = state["producer_anchor_stream"]
        if anchor_stream is None:
            try:
                anchor_stream, upgraded_identity = _open_producer_continuity_anchor(artifact)
            except BaseException as exc:
                raise poison_guard(
                    f"PyInstaller final producer write-fence acquisition failed: {exc}",
                    exc,
                )
            if upgraded_identity != producer_identity:
                anchor_stream.close()
                raise poison_guard(
                    "PyInstaller final producer fence does not match authoritative creation object"
                )
            state["producer_anchor_stream"] = anchor_stream
        else:
            anchor_now = os.fstat(anchor_stream.fileno())
            current_path = _require_regular_nonreparse(
                artifact,
                label="PyInstaller producer output before checksum retry",
            )
            if (
                _object_identity(anchor_now) != producer_identity
                or _object_identity(current_path) != producer_identity
            ):
                raise poison_guard(
                    "PyInstaller producer output lost continuity before checksum retry"
                )

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
        building_api.EXE.assemble = original_assemble
        building_api.winutils.update_exe_pe_checksum = original_update_checksum
        miscutils.mtime = original_mtime
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
