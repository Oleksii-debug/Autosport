from __future__ import annotations

import ctypes
import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_LAUNCH_BOUNDARY = _ROOT / "scripts" / "guarded_pyinstaller_launch_boundary.py"
_PRODUCER_TEST_PATH = _ROOT / "tests" / "test_windows_pyinstaller_producer_handoff.py"
_PRODUCER_SPEC = importlib.util.spec_from_file_location(
    "_autosport_pyinstaller_producer_handoff_tests_thread_birth_authority",
    _PRODUCER_TEST_PATH,
)
assert _PRODUCER_SPEC is not None and _PRODUCER_SPEC.loader is not None
_PRODUCER_TESTS = importlib.util.module_from_spec(_PRODUCER_SPEC)
sys.modules[_PRODUCER_SPEC.name] = _PRODUCER_TESTS
_PRODUCER_SPEC.loader.exec_module(_PRODUCER_TESTS)
_REAL_WINDOWS_PYINSTALLER = (
    os.name == "nt" and importlib.util.find_spec("PyInstaller") is not None
)


def _load_launcher():
    spec = importlib.util.spec_from_file_location(
        "_autosport_guarded_pyinstaller_launch_boundary_thread_test",
        _LAUNCH_BOUNDARY,
    )
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = launcher
    spec.loader.exec_module(launcher)
    return launcher


def test_primary_thread_birth_security_is_wired_before_barrier_release() -> None:
    launcher = _LAUNCH_BOUNDARY.read_text(encoding="utf-8")
    relaunch = launcher.split("def relaunch_birth_protected_worker()", 1)[1]

    assert "_DANGEROUS_THREAD_ACCESS = 0x000C17B3" in launcher
    assert "_birth_thread_security_descriptor(current_user_sid)" in relaunch
    assert "ctypes.byref(thread_attributes)" in relaunch
    assert "_require_fresh_thread_access_denied(int(process_info.dwThreadId))" in relaunch
    assert "_run_primary_thread_sibling_probe(python, int(process_info.dwThreadId))" in relaunch
    assert relaunch.index("ctypes.byref(thread_attributes)") < relaunch.index(
        "_require_fresh_thread_access_denied(int(process_info.dwThreadId))"
    )
    assert relaunch.index(
        "_require_fresh_thread_access_denied(int(process_info.dwThreadId))"
    ) < relaunch.index("_close_handle(process_info.hThread)")
    assert relaunch.index(
        "_run_primary_thread_sibling_probe(python, int(process_info.dwThreadId))"
    ) < relaunch.index("set_event(barrier)")


def test_protected_worker_uses_restricted_token_and_immutable_file_bootstrap() -> None:
    launcher = _LAUNCH_BOUNDARY.read_text(encoding="utf-8")
    relaunch = launcher.split("def relaunch_birth_protected_worker()", 1)[1]

    assert "CreateProcessAsUserW" in relaunch
    assert "_create_restricted_primary_token()" in relaunch
    assert "restricted_token," in relaunch
    assert "str(launcher)," in relaunch
    assert "_PROTECTED_WORKER_ARG," in relaunch
    assert "_PROTECTED_BOOTSTRAP" not in launcher
    assert "runpy.run_path(str(binder), run_name=\"__main__\")" in launcher
    assert "_current_process_token_is_restricted()" in launcher


def test_forged_process_local_marker_is_not_enough_for_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _load_launcher()
    marker = types.ModuleType(launcher._PROTECTED_MARKER_MODULE)
    marker.nonce = "f" * 64
    marker.restricted_primary_token = True
    marker.launcher_path = str(_LAUNCH_BOUNDARY.resolve())
    monkeypatch.setitem(sys.modules, launcher._PROTECTED_MARKER_MODULE, marker)
    monkeypatch.setattr(launcher, "_current_process_token_is_restricted", lambda: False)

    assert launcher.protected_launch_attested() is False


def test_primary_thread_birth_mask_covers_required_mutation_rights() -> None:
    launcher = _load_launcher()

    for access in (
        launcher._THREAD_SET_CONTEXT,
        launcher._THREAD_SUSPEND_RESUME,
        launcher._WRITE_DAC,
        launcher._WRITE_OWNER,
    ):
        assert launcher._DANGEROUS_THREAD_ACCESS & access == access


@pytest.mark.skipif(os.name != "nt", reason="real Windows pre-census mutation regression")
def test_closed_vm_write_authority_cannot_turn_forged_marker_into_attestation() -> None:
    launcher = _load_launcher()
    if launcher._current_process_token_is_restricted():
        pytest.skip("test process already has a restricted primary token")

    target = ctypes.create_string_buffer(b"SAFE" + b"\0" * 60)
    payload = b"MUTATED-BEFORE-CENSUS"
    helper = r'''
import ctypes
import sys
from ctypes import wintypes

PROCESS_VM_OPERATION = 0x00000008
PROCESS_VM_WRITE = 0x00000020
pid = int(sys.argv[1])
address = int(sys.argv[2])
payload = bytes.fromhex(sys.argv[3])
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
open_process = kernel32.OpenProcess
open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
open_process.restype = wintypes.HANDLE
write_process_memory = kernel32.WriteProcessMemory
write_process_memory.argtypes = (
    wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
)
write_process_memory.restype = wintypes.BOOL
close_handle = kernel32.CloseHandle
close_handle.argtypes = (wintypes.HANDLE,)
close_handle.restype = wintypes.BOOL
handle = open_process(PROCESS_VM_OPERATION | PROCESS_VM_WRITE, False, pid)
value = handle if isinstance(handle, int) else ctypes.cast(handle, ctypes.c_void_p).value
if not value:
    raise SystemExit(f"OpenProcess failed: {ctypes.get_last_error()}")
try:
    source = ctypes.create_string_buffer(payload)
    written = ctypes.c_size_t(0)
    if not write_process_memory(
        handle,
        ctypes.c_void_p(address),
        source,
        len(payload),
        ctypes.byref(written),
    ):
        raise SystemExit(f"WriteProcessMemory failed: {ctypes.get_last_error()}")
    if int(written.value) != len(payload):
        raise SystemExit("WriteProcessMemory short write")
finally:
    close_handle(handle)
'''
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            helper,
            str(os.getpid()),
            str(ctypes.addressof(target)),
            payload.hex(),
        ],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
    assert target.raw.startswith(payload)

    marker = types.ModuleType(launcher._PROTECTED_MARKER_MODULE)
    marker.nonce = "f" * 64
    marker.restricted_primary_token = True
    marker.launcher_path = str(_LAUNCH_BOUNDARY.resolve())
    old_marker = sys.modules.get(launcher._PROTECTED_MARKER_MODULE)
    sys.modules[launcher._PROTECTED_MARKER_MODULE] = marker
    try:
        # The hostile VM_WRITE handle is already closed here; its persistent state
        # cannot turn an ordinary creator token into birth attestation.
        assert launcher.protected_launch_attested() is False
    finally:
        if old_marker is None:
            sys.modules.pop(launcher._PROTECTED_MARKER_MODULE, None)
        else:
            sys.modules[launcher._PROTECTED_MARKER_MODULE] = old_marker


@pytest.mark.skipif(
    not _REAL_WINDOWS_PYINSTALLER,
    reason="real Windows primary-thread birth-security regression",
)
def test_hostile_same_token_sibling_cannot_open_primary_thread_before_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_TEST_PRIMARY_THREAD_SIBLING_PROBE", "1")

    completed, artifact, bound, digest = _PRODUCER_TESTS._run_real_pyinstaller_probe(
        tmp_path
    )

    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
    assert artifact.is_file()
    assert bound.is_file()
    assert digest.is_file()
