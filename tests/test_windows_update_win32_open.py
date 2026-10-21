"""Portable ABI regressions for Windows update ZIP handle admission.

These mocks test the Win32 call contract, not actual NTFS behavior; Windows CI
must still qualify a real filesystem and reparse-point witness.
"""
from __future__ import annotations

import ctypes
import os
import sys
from types import SimpleNamespace

import pytest

from autosport import windows_update_snapshot as snapshot


class FakeCreateFile:
    argtypes = None
    restype = None

    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


class FakeCloseHandle:
    argtypes = None
    restype = None

    def __init__(self):
        self.calls = []

    def __call__(self, handle):
        self.calls.append(handle)
        return True


def _win32(monkeypatch, *, handle=1234, fd=17, raise_osf=False):
    create = FakeCreateFile(handle)
    close = FakeCloseHandle()
    transferred = []

    def open_osfhandle(raw_handle, flags):
        transferred.append((raw_handle, flags))
        if raise_osf:
            raise OSError("simulated descriptor allocation failure")
        return fd

    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: SimpleNamespace(
        CreateFileW=create, CloseHandle=close,
    ), raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=open_osfhandle))
    return create, close, transferred


def test_win32_zip_open_uses_no_follow_handle_and_transfers_ownership(monkeypatch):
    create, close, transferred = _win32(monkeypatch)
    result = snapshot._open_windows_archive_fd("C:\\Оновлення\\кандидат.zip")
    assert result == 17
    assert len(create.calls) == 1
    path, access, share, security, disposition, flags, template = create.calls[0]
    assert path == "C:\\Оновлення\\кандидат.zip"
    assert access == 0x80000000  # GENERIC_READ
    assert share & 0x7 == 0x7  # FILE_SHARE_READ|WRITE|DELETE
    assert security is None and disposition == 3 and template is None
    assert flags & 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
    assert transferred == [(1234, os.O_RDONLY | getattr(os, "O_BINARY", 0))]
    assert not close.calls  # CRT owns the native HANDLE after transfer


def test_win32_zip_open_rejects_invalid_handle_without_transfer(monkeypatch):
    invalid = ctypes.c_void_p(-1).value
    create, close, transferred = _win32(monkeypatch, handle=invalid)
    with pytest.raises(snapshot.WindowsUpdateSnapshotError, match="handle"):
        snapshot._open_windows_archive_fd("C:\\candidate.zip")
    assert len(create.calls) == 1
    assert not transferred and not close.calls


def test_win32_zip_open_closes_handle_if_crt_adoption_fails(monkeypatch):
    create, close, transferred = _win32(monkeypatch, raise_osf=True)
    with pytest.raises(snapshot.WindowsUpdateSnapshotError, match="handle"):
        snapshot._open_windows_archive_fd("C:\\candidate.zip")
    assert len(create.calls) == 1
    assert transferred and close.calls == [1234]


def test_win32_change_time_rejects_reparse_attribute(monkeypatch):
    class Query:
        argtypes = None
        restype = None

        def __call__(self, handle, info_class, output, size):
            assert handle == 1234 and info_class == 0 and size == 40
            raw = ctypes.cast(output, ctypes.POINTER(ctypes.c_longlong * 5))
            raw.contents[3] = 123456789
            # FileAttributes follows four 64-bit timestamps, at offset 32.
            ctypes.cast(output, ctypes.POINTER(ctypes.c_uint32 * 10)).contents[8] = 0x400
            return True

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: 1234))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: SimpleNamespace(
        GetFileInformationByHandleEx=Query(),
    ), raising=False)
    with pytest.raises(snapshot.WindowsUpdateSnapshotError, match="reparse|redirect"):
        snapshot._query_windows_change_time_100ns(17)


def test_win32_zip_open_does_not_fallback_if_native_api_unavailable(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("simulated Win32 API unavailable")

    monkeypatch.setattr(ctypes, "WinDLL", unavailable, raising=False)
    with pytest.raises(snapshot.WindowsUpdateSnapshotError, match="handle unavailable"):
        snapshot._open_windows_archive_fd("C:\\candidate.zip")


@pytest.mark.parametrize("result", [None, 0, ctypes.c_void_p(-1).value])
def test_win32_zip_open_rejects_null_or_invalid_native_handle(monkeypatch, result):
    create, close, transferred = _win32(monkeypatch, handle=result)
    with pytest.raises(snapshot.WindowsUpdateSnapshotError, match="handle unavailable"):
        snapshot._open_windows_archive_fd("C:\\candidate.zip")
    assert len(create.calls) == 1
    assert not transferred and not close.calls


def test_zip_open_dispatches_windows_to_native_nofollow_without_crt_path_open(monkeypatch):
    from pathlib import Path
    calls = []

    def native(path):
        calls.append(path)
        return 21

    def unsafe_path_open(*args, **kwargs):
        raise AssertionError("Windows must not use path-following os.open")

    monkeypatch.setattr(snapshot, "_open_windows_archive_fd", native)
    monkeypatch.setattr(snapshot.os, "open", unsafe_path_open)
    path = Path("candidate.zip")
    assert snapshot._open_archive_fd(path, _platform="nt") == 21
    assert calls == [path]


def test_win32_zip_open_closes_native_handle_on_memory_pressure(monkeypatch):
    """CRT allocation failures must not leak an unowned Win32 HANDLE."""
    create, close, transferred = _win32(monkeypatch)

    def out_of_memory(*args):
        raise MemoryError("simulated CRT allocation pressure")

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=out_of_memory))
    with pytest.raises(snapshot.WindowsUpdateSnapshotError, match="handle unavailable"):
        snapshot._open_windows_archive_fd("C:\\Оновлення\\candidate.zip")
    assert len(create.calls) == 1
    assert close.calls == [1234]


def test_win32_zip_open_closes_native_handle_on_unexpected_crt_failure(monkeypatch):
    """An unexpected CRT exception must still fail closed and release HANDLE."""
    create, close, transferred = _win32(monkeypatch)

    def runtime_failure(*args):
        raise RuntimeError("simulated CRT initialization failure")

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=runtime_failure))
    with pytest.raises(snapshot.WindowsUpdateSnapshotError, match="handle unavailable"):
        snapshot._open_windows_archive_fd("C:\\candidate.zip")
    assert len(create.calls) == 1
    assert close.calls == [1234]


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows NTFS handle semantics")
def test_win32_real_zip_unicode_path_and_native_handle(tmp_path):
    """Windows CI: a real, ordinary ZIP is accepted through CreateFileW."""
    import zipfile

    package = tmp_path / "оновлення з пробілами.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", "{}")
    fd = snapshot._open_windows_archive_fd(package)
    try:
        assert os.fstat(fd).st_size == package.stat().st_size
        assert snapshot._query_windows_change_time_100ns(fd) > 0
    finally:
        os.close(fd)
    snapshot._require_bounded_zip_members(package)


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows NTFS reparse semantics")
def test_win32_real_symlink_handle_is_rejected(tmp_path):
    """Windows CI: CreateFileW opens a symlink itself and rejects its attribute.

    Some locked-down Windows runners cannot create symlinks; that is explicitly
    SKIPPED, not evidence of native reparse-point acceptance.
    """
    import zipfile

    target = tmp_path / "реальний пакет.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", "{}")
    alias = tmp_path / "посилання.zip"
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Windows runner cannot create file symlink: {type(exc).__name__}")
    fd = snapshot._open_windows_archive_fd(alias)
    try:
        with pytest.raises(snapshot.WindowsUpdateSnapshotError, match="reparse"):
            snapshot._query_windows_change_time_100ns(fd)
    finally:
        os.close(fd)
    with pytest.raises(snapshot.WindowsUpdateSnapshotError):
        snapshot._require_bounded_zip_members(alias)


@pytest.mark.parametrize("cancel", [KeyboardInterrupt, SystemExit])
def test_win32_crt_adoption_cancellation_closes_untransferred_handle(monkeypatch, cancel):
    """Interrupting CRT adoption must not leak an unowned Win32 HANDLE."""
    create, close, transferred = _win32(monkeypatch)

    def interrupt(*args):
        raise cancel("cancelled before descriptor ownership transfer")

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=interrupt))
    with pytest.raises(cancel):
        snapshot._open_windows_archive_fd("C:\\Оновлення\\candidate.zip")
    assert len(create.calls) == 1
    assert close.calls == [1234]


@pytest.mark.parametrize("failure", [RuntimeError, OverflowError])
def test_win32_change_time_native_wrapper_failure_is_sanitized(monkeypatch, failure):
    """Unusual ctypes failures must fail closed without leaking private paths."""
    class BrokenQuery:
        argtypes = None
        restype = None

        def __call__(self, *args):
            raise failure("C:\\private\\secret-update.zip")

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: 1234))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: SimpleNamespace(
        GetFileInformationByHandleEx=BrokenQuery(),
    ), raising=False)
    with pytest.raises(snapshot.WindowsUpdateSnapshotError, match="change-time unavailable") as exc:
        snapshot._query_windows_change_time_100ns(17)
    assert "secret-update.zip" not in str(exc.value)
    assert exc.value.__suppress_context__ is True


def test_win32_zip_open_sanitizes_native_wrapper_overflow(monkeypatch):
    """An ABI overflow must not expose an untrusted update path in diagnostics."""
    def overflow(*args, **kwargs):
        raise OverflowError(r"C:\private\secret-update.zip")

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=lambda *a: 17))
    monkeypatch.setattr(ctypes, "WinDLL", overflow, raising=False)
    with pytest.raises(snapshot.WindowsUpdateSnapshotError, match="handle unavailable") as error:
        snapshot._open_windows_archive_fd(r"C:\private\secret-update.zip")
    assert "secret-update.zip" not in str(error.value)
    assert error.value.__suppress_context__ is True


def test_win32_zip_open_sanitizes_close_failure_after_crt_failure(monkeypatch):
    """A broken CloseHandle wrapper must not replace the original fail-closed error."""
    create, close, _ = _win32(monkeypatch, raise_osf=True)

    def broken_close(self, handle):
        self.calls.append(handle)
        raise RuntimeError(r"C:\private\secret-update.zip")

    monkeypatch.setattr(FakeCloseHandle, "__call__", broken_close)
    with pytest.raises(snapshot.WindowsUpdateSnapshotError, match="handle unavailable") as error:
        snapshot._open_windows_archive_fd(r"C:\private\secret-update.zip")
    assert create.calls
    assert close.calls == [1234]
    assert "secret-update.zip" not in str(error.value)
    assert error.value.__suppress_context__ is True


def test_win32_zip_open_sanitizes_createfile_wrapper_overflow(monkeypatch):
    """Native argument marshalling errors must not leak the operator's path."""
    class OverflowCreate:
        argtypes = None
        restype = None

        def __call__(self, *args):
            raise OverflowError(r"C:\private\secret-update.zip")

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=lambda *a: 17))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: SimpleNamespace(
        CreateFileW=OverflowCreate(), CloseHandle=FakeCloseHandle(),
    ), raising=False)
    with pytest.raises(snapshot.WindowsUpdateSnapshotError, match="handle unavailable") as error:
        snapshot._open_windows_archive_fd(r"C:\private\secret-update.zip")
    assert "secret-update.zip" not in str(error.value)
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize("cancel", [KeyboardInterrupt, SystemExit])
def test_win32_crt_cancellation_survives_close_wrapper_failure(monkeypatch, cancel):
    """A broken native cleanup wrapper cannot convert process cancellation to retry."""
    create, close, _ = _win32(monkeypatch)

    def interrupt(*args):
        raise cancel("operator cancellation")

    def broken_close(self, handle):
        self.calls.append(handle)
        raise RuntimeError(r"C:\private\secret-update.zip")

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=interrupt))
    monkeypatch.setattr(FakeCloseHandle, "__call__", broken_close)
    with pytest.raises(cancel, match="operator cancellation"):
        snapshot._open_windows_archive_fd(r"C:\private\secret-update.zip")
    assert create.calls and close.calls == [1234]
