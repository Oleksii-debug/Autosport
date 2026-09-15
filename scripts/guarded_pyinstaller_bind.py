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
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_CREATE_ALWAYS = 2
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


def _create_windows_stream(
    path: pathlib.Path,
    *,
    desired_access: int,
    share_mode: int,
    creation_disposition: int,
    os_flags: int,
    mode: str,
):
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
        desired_access,
        share_mode,
        None,
        creation_disposition,
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
            os_flags | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        close_handle(raw_handle)
        raise

    try:
        return os.fdopen(descriptor, mode, closefd=True)
    except BaseException:
        os.close(descriptor)
        raise


def _copy_artifact_and_capture_identity(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
) -> tuple[int, int]:
    """Copy the initial artifact and capture identity from the still-open producing handle."""

    destination = pathlib.Path(dst)
    stream = _create_windows_stream(
        destination,
        desired_access=_GENERIC_READ | _GENERIC_WRITE,
        share_mode=_FILE_SHARE_READ,
        creation_disposition=_CREATE_ALWAYS,
        os_flags=os.O_RDWR,
        mode="w+b",
    )
    try:
        with open(src, "rb") as source:
            shutil.copyfileobj(source, stream)
        stream.flush()
        os.fsync(stream.fileno())

        if os.environ.get("AUTOSPORT_TEST_WRITE_PYINSTALLER_OUTPUT_BEFORE_IDENTITY") == "1":
            with open(destination, "r+b") as competing_stream:
                competing_stream.seek(0)
                competing_stream.write(b"hostile same-object bytes")
                competing_stream.flush()
                os.fsync(competing_stream.fileno())
            raise RuntimeError(
                "producing PyInstaller handle allowed a concurrent write before identity capture"
            )

        if os.environ.get("AUTOSPORT_TEST_REPLACE_PYINSTALLER_OUTPUT_BEFORE_IDENTITY") == "1":
            replacement = destination.with_name(
                f".{destination.name}.pre-identity-replacement-{os.getpid()}"
            )
            try:
                with open(replacement, "wb") as replacement_stream:
                    stream.seek(0)
                    shutil.copyfileobj(stream, replacement_stream)
                    replacement_stream.flush()
                    os.fsync(replacement_stream.fileno())
                stream.seek(0, os.SEEK_END)
                os.replace(replacement, destination)
            finally:
                try:
                    replacement.unlink()
                except FileNotFoundError:
                    pass

        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeError(
                f"initial PyInstaller output handle must be a regular file: {destination}"
            )
        if _windows_attributes(destination) & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise RuntimeError(
                f"initial PyInstaller output must not be a Windows reparse point: {destination}"
            )
        current_path = _require_regular_nonreparse(
            destination,
            label="initial PyInstaller output",
        )
        if _object_identity(current_path) != _object_identity(opened):
            raise RuntimeError(
                "initial PyInstaller output pathname changed before identity capture"
            )
        return _object_identity(opened)
    finally:
        stream.close()


def _open_retained_read_fence(
    path: pathlib.Path,
    *,
    expected_object_identity: tuple[int, int],
):
    if os.name != "nt":
        raise RuntimeError("guarded PyInstaller artifact binding is Windows-only")

    before = _require_regular_nonreparse(path, label="PyInstaller output")
    if _object_identity(before) != expected_object_identity:
        raise RuntimeError("PyInstaller produced object identity changed before binding")

    stream = _create_windows_stream(
        path,
        desired_access=_GENERIC_READ,
        share_mode=_FILE_SHARE_READ,
        creation_disposition=_OPEN_EXISTING,
        os_flags=os.O_RDONLY,
        mode="rb",
    )
    try:
        opened = os.fstat(stream.fileno())
        if _object_identity(opened) != expected_object_identity:
            raise RuntimeError("PyInstaller output changed while acquiring retained binding fence")
        after_path = _require_regular_nonreparse(path, label="PyInstaller output")
        if _object_identity(after_path) != expected_object_identity:
            raise RuntimeError("PyInstaller output path changed while acquiring retained binding fence")
        return stream
    except BaseException:
        stream.close()
        raise


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run PyInstaller and bind its exact produced EXE before producer completion."
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

    import PyInstaller.__main__
    import PyInstaller.building.api as building_api
    import PyInstaller.utils.misc as miscutils

    original_copyfile = shutil.copyfile
    original_mtime = miscutils.mtime
    original_assemble = building_api.EXE.assemble
    state: dict[str, Any] = {
        "initial_identity": None,
        "guard_stream": None,
        "bound": False,
        "test_replacement_injected": False,
    }

    def guarded_copyfile(src, dst, *copy_args, **copy_kwargs):
        if _normalized_path(dst) == artifact_key and state["initial_identity"] is None:
            if copy_args:
                raise RuntimeError("unexpected positional shutil.copyfile options for PyInstaller artifact")
            unexpected = set(copy_kwargs) - {"follow_symlinks"}
            if unexpected:
                raise RuntimeError(
                    "unexpected shutil.copyfile options for PyInstaller artifact: "
                    + ", ".join(sorted(unexpected))
                )
            if copy_kwargs.get("follow_symlinks", True) is not True:
                raise RuntimeError("PyInstaller artifact copy must follow the regular bootloader source")
            state["initial_identity"] = _copy_artifact_and_capture_identity(src, dst)
            return dst
        return original_copyfile(src, dst, *copy_args, **copy_kwargs)

    def guarded_mtime(path):
        if _normalized_path(path) == artifact_key and state["guard_stream"] is None:
            initial_identity = state["initial_identity"]
            if initial_identity is None:
                raise RuntimeError("PyInstaller output identity was never captured at producer creation")

            if (
                os.environ.get("AUTOSPORT_TEST_REPLACE_PYINSTALLER_OUTPUT") == "1"
                and not state["test_replacement_injected"]
            ):
                replacement = artifact.with_name(
                    f".{artifact.name}.replacement-{os.getpid()}"
                )
                try:
                    original_copyfile(artifact, replacement)
                    os.replace(replacement, artifact)
                finally:
                    try:
                        replacement.unlink()
                    except FileNotFoundError:
                        pass
                state["test_replacement_injected"] = True

            state["guard_stream"] = _open_retained_read_fence(
                artifact,
                expected_object_identity=initial_identity,
            )
        return original_mtime(path)

    def guarded_assemble(self):
        result = original_assemble(self)
        if _normalized_path(self.name) != artifact_key:
            return result
        initial_identity = state["initial_identity"]
        guard_stream = state["guard_stream"]
        if initial_identity is None or guard_stream is None:
            raise RuntimeError(
                "PyInstaller producer completed without retained artifact identity fence"
            )

        opened_before = os.fstat(guard_stream.fileno())
        if _object_identity(opened_before) != initial_identity:
            raise RuntimeError("retained PyInstaller artifact identity changed before trusted bind")
        current_path = _require_regular_nonreparse(
            artifact,
            label="PyInstaller output before trusted bind",
        )
        if _object_identity(current_path) != initial_identity:
            raise RuntimeError("PyInstaller output pathname no longer names the produced object")

        _invoke_trusted_verifier(
            verifier=verifier,
            verifier_sha256=args.verifier_sha256,
            artifact=artifact,
            bound_output=bound_output,
            digest_output=digest_output,
        )

        opened_after = os.fstat(guard_stream.fileno())
        current_after = _require_regular_nonreparse(
            artifact,
            label="PyInstaller output after trusted bind",
        )
        if (
            _stable_identity(opened_after) != _stable_identity(opened_before)
            or _object_identity(current_after) != initial_identity
        ):
            raise RuntimeError("PyInstaller output changed across trusted artifact binding")
        state["bound"] = True
        return result

    shutil.copyfile = guarded_copyfile
    miscutils.mtime = guarded_mtime
    building_api.EXE.assemble = guarded_assemble
    try:
        PyInstaller.__main__.run(pyi_args=list(args.pyinstaller_args))
        if not state["bound"]:
            raise RuntimeError("PyInstaller exited without binding the exact produced artifact")
        digest = digest_output.read_text(encoding="utf-8").strip()
        if _SHA256_RE.fullmatch(digest) is None:
            raise RuntimeError("trusted artifact verifier emitted invalid digest")
        return 0
    finally:
        building_api.EXE.assemble = original_assemble
        miscutils.mtime = original_mtime
        shutil.copyfile = original_copyfile
        guard_stream = state.get("guard_stream")
        if guard_stream is not None:
            guard_stream.close()


def main(argv: list[str] | None = None) -> int:
    try:
        return run(argv)
    except Exception as exc:
        print(f"guarded PyInstaller handoff failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
