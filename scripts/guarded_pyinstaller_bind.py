from __future__ import annotations

import argparse
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
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
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


def _require_expected_pyinstaller_version(actual: str) -> None:
    if actual != _EXPECTED_PYINSTALLER_VERSION:
        raise RuntimeError(
            "guarded PyInstaller producer version mismatch: "
            f"expected {_EXPECTED_PYINSTALLER_VERSION}, got {actual}"
        )


def _windows_attributes(path: pathlib.Path) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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


def _open_producer_continuity_anchor(
    path: pathlib.Path,
) -> tuple[Any, tuple[int, int]]:
    """Retain the producer object across its final legitimate Windows mutation.

    The anchor permits additional READ/WRITE opens so PyInstaller can perform its
    final checksum rewrite, but intentionally omits DELETE sharing. A same-path
    rename/replacement therefore cannot swap the object between that producer
    mutation and acquisition of the stricter final binding fence.
    """

    if os.name != "nt":
        raise RuntimeError("guarded PyInstaller artifact binding is Windows-only")

    import msvcrt

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

    ctypes.set_last_error(0)
    raw_handle = create_file(
        str(path),
        _GENERIC_READ,
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
        descriptor = msvcrt.open_osfhandle(
            int(handle_value),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        close_handle(raw_handle)
        raise

    try:
        stream = os.fdopen(descriptor, "rb", closefd=True)
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


def _open_final_retained_read_fence(
    path: pathlib.Path,
) -> tuple[Any, tuple[int, int]]:
    """Establish the final no-write/no-delete binding fence from an open handle."""

    if os.name != "nt":
        raise RuntimeError("guarded PyInstaller artifact binding is Windows-only")

    import msvcrt

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
        close_handle(raw_handle)
        raise

    try:
        stream = os.fdopen(descriptor, "rb", closefd=True)
    except BaseException:
        os.close(descriptor)
        raise

    try:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeError(f"final PyInstaller output handle is not a regular file: {path}")
        final_identity = _object_identity(opened)
        after_path = _require_regular_nonreparse(path, label="final PyInstaller output")
        if _object_identity(after_path) != final_identity:
            raise RuntimeError(
                "PyInstaller output path changed while acquiring final retained binding fence"
            )
        return stream, final_identity
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
        "producer_anchor_stream": None,
        "producer_identity": None,
        "final_identity": None,
        "guard_stream": None,
        "guard_error": None,
        "bound": False,
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

    def guarded_update_exe_pe_checksum(path, *checksum_args, **checksum_kwargs):
        if not state["producer_active"] or _normalized_path(path) != artifact_key:
            return original_update_checksum(path, *checksum_args, **checksum_kwargs)
        if state["guard_error"] is not None:
            raise RuntimeError(str(state["guard_error"]))

        anchor_stream = state["producer_anchor_stream"]
        producer_identity = state["producer_identity"]
        if anchor_stream is None:
            try:
                anchor_stream, producer_identity = _open_producer_continuity_anchor(artifact)
            except BaseException as exc:
                raise poison_guard(
                    f"PyInstaller producer continuity anchor acquisition failed: {exc}",
                    exc,
                )
            state["producer_anchor_stream"] = anchor_stream
            state["producer_identity"] = producer_identity
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

        try:
            result = original_update_checksum(path, *checksum_args, **checksum_kwargs)
            anchor_after = os.fstat(anchor_stream.fileno())
            current_after = _require_regular_nonreparse(
                artifact,
                label="PyInstaller producer output after final checksum",
            )
            if (
                _object_identity(anchor_after) != producer_identity
                or _object_identity(current_after) != producer_identity
            ):
                raise poison_guard(
                    "PyInstaller producer output lost continuity during final checksum"
                )
            return result
        except RuntimeError as exc:
            if state["guard_error"] is not None:
                raise
            raise exc

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
            if anchor_stream is None or producer_identity is None:
                raise poison_guard(
                    "PyInstaller reached final fence without a producer continuity anchor"
                )

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
                guard_stream, final_identity = _open_final_retained_read_fence(artifact)
                anchor_now = os.fstat(anchor_stream.fileno())
                if (
                    _object_identity(anchor_now) != producer_identity
                    or final_identity != producer_identity
                ):
                    guard_stream.close()
                    raise RuntimeError(
                        "final PyInstaller fence object does not match producer continuity anchor"
                    )
            except BaseException as exc:
                raise poison_guard(
                    f"PyInstaller final fence integrity failure: {exc}",
                    exc,
                )

            state["guard_stream"] = guard_stream
            state["final_identity"] = final_identity

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

            return os.fstat(guard_stream.fileno()).st_mtime
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
        anchor_stream = state["producer_anchor_stream"]
        final_identity = state["final_identity"]
        guard_stream = state["guard_stream"]
        if producer_identity is None or anchor_stream is None:
            raise RuntimeError(
                "PyInstaller producer completed without retained producer continuity anchor"
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
        ):
            raise RuntimeError(
                "retained PyInstaller producer/final artifact identity changed before trusted bind"
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
        ):
            raise RuntimeError("PyInstaller output changed across trusted artifact binding")
        state["bound"] = True
        return result

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
        guard_stream = state.get("guard_stream")
        if guard_stream is not None:
            guard_stream.close()
        anchor_stream = state.get("producer_anchor_stream")
        if anchor_stream is not None:
            anchor_stream.close()


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except Exception as exc:
        print(f"guarded PyInstaller handoff failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
