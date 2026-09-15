from __future__ import annotations

import ctypes
import importlib.util
import os
import shutil
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_SECURITY_AUTHORITY = _ROOT / "scripts" / "guarded_pyinstaller_security_authority.py"
_PRODUCER_TEST_PATH = _ROOT / "tests" / "test_windows_pyinstaller_producer_handoff.py"
_PRODUCER_SPEC = importlib.util.spec_from_file_location(
    "_autosport_pyinstaller_producer_handoff_tests_retained_write_dac",
    _PRODUCER_TEST_PATH,
)
assert _PRODUCER_SPEC is not None and _PRODUCER_SPEC.loader is not None
_PRODUCER_TESTS = importlib.util.module_from_spec(_PRODUCER_SPEC)
sys.modules[_PRODUCER_SPEC.name] = _PRODUCER_TESTS
_PRODUCER_SPEC.loader.exec_module(_PRODUCER_TESTS)

_REAL_WINDOWS_PYINSTALLER = (
    os.name == "nt" and importlib.util.find_spec("PyInstaller") is not None
)

_CHILD_RETAINED_DIRECTORY_WRITE_DAC = r'''
import ctypes
import sys
from ctypes import wintypes

WRITE_DAC = 0x00040000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
FILE_SHARE_DELETE = 0x4
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

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

raw_handle = create_file(
    sys.argv[1],
    WRITE_DAC,
    FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
    None,
    OPEN_EXISTING,
    FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
    None,
)
value = raw_handle if isinstance(raw_handle, int) else ctypes.cast(raw_handle, ctypes.c_void_p).value
if value in {None, INVALID_HANDLE_VALUE}:
    raise SystemExit(f"PREOPEN_FAILED:{ctypes.get_last_error()}")
print("READY", flush=True)
try:
    sys.stdin.buffer.read(1)
finally:
    close_handle(raw_handle)
'''


def _load_security_authority():
    spec = importlib.util.spec_from_file_location(
        "_autosport_retained_write_dac_security_authority_test",
        _SECURITY_AUTHORITY,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_security_authority_source_audits_preexisting_mutation_handles() -> None:
    source = _SECURITY_AUTHORITY.read_text(encoding="utf-8")

    assert "NtQuerySystemInformation" in source
    assert "_SYSTEM_EXTENDED_HANDLE_INFORMATION = 64" in source
    assert "_MUTATION_CAPABLE_ACCESS" in source
    assert "pre-existing competing mutation-capable handle(s)" in source
    assert source.count("_require_no_competing_mutation_handles(") >= 3
    assert "allow_current_process_data_mutators=True" in source
    assert "directory=True" in source
    assert "directory=False" in source
    assert "trusted expected-snapshot parent security fence" in source
    assert "trusted expected-snapshot file security fence" in source
    assert "guarded_pyinstaller_process_authority.py" in source
    assert "ProcessDuplicationFence(_query_system_handles)" in source
    assert source.count("process_fence.acquire(sid)") >= 2
    assert source.count("release_process_fence_if_idle()") >= 5


def test_mutation_handle_audit_rejects_cross_process_direct_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _load_security_authority()
    current_pid = os.getpid()
    target_object = 0x12345678
    trusted_handle = 0x111

    monkeypatch.setattr(
        security,
        "_query_system_handles",
        lambda: [
            (target_object, current_pid, trusted_handle, security._WRITE_DAC),
            (
                target_object,
                current_pid + 1,
                0x222,
                security._FILE_WRITE_DATA | security._FILE_WRITE_ATTRIBUTES,
            ),
        ],
    )

    with pytest.raises(RuntimeError, match="pre-existing competing mutation-capable handle"):
        security._require_no_competing_mutation_handles(
            trusted_handle,
            label="cross-process retained direct writer",
            directory=False,
            allow_current_process_data_mutators=True,
        )


def test_mutation_handle_audit_allows_only_current_process_data_mutator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _load_security_authority()
    current_pid = os.getpid()
    target_object = 0x22334455
    trusted_handle = 0x333
    native_writer = 0x444

    monkeypatch.setattr(
        security,
        "_query_system_handles",
        lambda: [
            (target_object, current_pid, trusted_handle, security._WRITE_DAC),
            (
                target_object,
                current_pid,
                native_writer,
                security._FILE_WRITE_DATA | security._DELETE_ACCESS,
            ),
        ],
    )

    security._require_no_competing_mutation_handles(
        trusted_handle,
        label="trusted native resource writer",
        directory=False,
        allow_current_process_data_mutators=True,
    )

    with pytest.raises(RuntimeError, match="pre-existing competing mutation-capable handle"):
        security._require_no_competing_mutation_handles(
            trusted_handle,
            label="untrusted current-process writer",
            directory=False,
            allow_current_process_data_mutators=False,
        )


def test_mutation_handle_audit_never_exempts_security_mutator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _load_security_authority()
    current_pid = os.getpid()
    target_object = 0x33445566
    trusted_handle = 0x555

    monkeypatch.setattr(
        security,
        "_query_system_handles",
        lambda: [
            (target_object, current_pid, trusted_handle, security._WRITE_DAC),
            (target_object, current_pid, 0x666, security._WRITE_DAC),
        ],
    )

    with pytest.raises(RuntimeError, match="pre-existing competing mutation-capable handle"):
        security._require_no_competing_mutation_handles(
            trusted_handle,
            label="current-process retained security mutator",
            directory=False,
            allow_current_process_data_mutators=True,
        )


def test_mutation_handle_audit_scopes_delete_child_bit_to_directories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _load_security_authority()
    current_pid = os.getpid()
    target_object = 0x44556677
    trusted_handle = 0x777

    monkeypatch.setattr(
        security,
        "_query_system_handles",
        lambda: [
            (target_object, current_pid, trusted_handle, security._WRITE_DAC),
            (target_object, current_pid + 1, 0x888, security._FILE_DELETE_CHILD),
        ],
    )

    # The 0x40 bit is FILE_EXECUTE for a regular file, so it is not a mutation right.
    security._require_no_competing_mutation_handles(
        trusted_handle,
        label="regular-file execute handle",
        directory=False,
        allow_current_process_data_mutators=True,
    )

    # The same bit is FILE_DELETE_CHILD for a directory and must fail closed.
    with pytest.raises(RuntimeError, match="pre-existing competing mutation-capable handle"):
        security._require_no_competing_mutation_handles(
            trusted_handle,
            label="directory delete-child handle",
            directory=True,
        )


@pytest.mark.skipif(os.name != "nt", reason="real Windows retained-WRITE_DAC regression")
def test_preopened_write_dac_handle_survives_deny_but_is_detected(tmp_path: Path) -> None:
    security = _load_security_authority()
    icacls = shutil.which("icacls.exe")
    assert icacls is not None

    target = tmp_path / "expected-snapshot.exe"
    target.write_bytes(b"MZ-autosport-retained-write-dac-regression")

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

    file_share_read = 0x1
    file_share_write = 0x2
    file_share_delete = 0x4
    open_existing = 3
    file_attribute_normal = 0x80
    file_flag_open_reparse_point = 0x00200000
    invalid_handle_value = ctypes.c_void_p(-1).value

    def open_write_dac():
        ctypes.set_last_error(0)
        handle = create_file(
            str(target),
            security._WRITE_DAC,
            file_share_read | file_share_write | file_share_delete,
            None,
            open_existing,
            file_attribute_normal | file_flag_open_reparse_point,
            None,
        )
        value = handle if isinstance(handle, int) else ctypes.cast(handle, ctypes.c_void_p).value
        return handle, value, ctypes.get_last_error()

    trusted, trusted_value, trusted_error = open_write_dac()
    assert trusted_value not in {None, invalid_handle_value}, trusted_error
    competing, competing_value, competing_error = open_write_dac()
    assert competing_value not in {None, invalid_handle_value}, competing_error

    try:
        completed = subprocess.run(
            [
                icacls,
                str(target),
                "/deny",
                f"*{security._OWNER_RIGHTS_SID}:(WDAC)",
                "/Q",
            ],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr

        fresh, fresh_value, _fresh_error = open_write_dac()
        if fresh_value not in {None, invalid_handle_value}:
            close_handle(fresh)
            pytest.fail("OWNER RIGHTS deny did not block a fresh WRITE_DAC open")

        with pytest.raises(
            RuntimeError,
            match=(
                "pre-existing competing mutation-capable handle"
                "|live uninspectable mutation-capable handle"
            ),
        ):
            security._require_no_competing_mutation_handles(
                trusted,
                label="retained WRITE_DAC regression",
                directory=False,
            )

        assert close_handle(competing)
        competing = None

        security._require_no_competing_mutation_handles(
            trusted,
            label="retained WRITE_DAC regression",
            directory=False,
        )
    finally:
        if competing is not None:
            close_handle(competing)
        close_handle(trusted)


@pytest.mark.skipif(
    not _REAL_WINDOWS_PYINSTALLER,
    reason="real Windows PyInstaller retained-WRITE_DAC regression",
)
def test_production_namespace_fence_rejects_preopened_cross_process_write_dac(
    tmp_path: Path,
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()

    child = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            _CHILD_RETAINED_DIRECTORY_WRITE_DAC,
            str(dist),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdin is not None
    assert child.stderr is not None

    try:
        ready = child.stdout.readline().strip()
        if ready != "READY":
            stderr = child.stderr.read()
            pytest.fail(f"retained-WRITE_DAC child did not become ready: {ready} {stderr}")
        assert child.poll() is None

        completed, _artifact, bound, digest = _PRODUCER_TESTS._run_real_pyinstaller_probe(
            tmp_path
        )

        assert completed.returncode != 0
        combined = completed.stdout + "\n" + completed.stderr
        assert "trusted expected-snapshot parent security fence has" in combined
        assert "pre-existing competing mutation-capable handle" in combined
        assert not bound.exists()
        assert not digest.exists()
    finally:
        try:
            child.stdin.close()
        except OSError:
            pass
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.terminate()
            child.wait(timeout=10)
