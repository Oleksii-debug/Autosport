from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_PROCESS_AUTHORITY = _ROOT / "scripts" / "guarded_pyinstaller_process_authority.py"
_LAUNCH_BOUNDARY = _ROOT / "scripts" / "guarded_pyinstaller_launch_boundary.py"
_PRODUCER_TEST_PATH = _ROOT / "tests" / "test_windows_pyinstaller_producer_handoff.py"
_PRODUCER_SPEC = importlib.util.spec_from_file_location(
    "_autosport_pyinstaller_producer_handoff_tests_process_authority",
    _PRODUCER_TEST_PATH,
)
assert _PRODUCER_SPEC is not None and _PRODUCER_SPEC.loader is not None
_PRODUCER_TESTS = importlib.util.module_from_spec(_PRODUCER_SPEC)
sys.modules[_PRODUCER_SPEC.name] = _PRODUCER_TESTS
_PRODUCER_SPEC.loader.exec_module(_PRODUCER_TESTS)
_REAL_WINDOWS_PYINSTALLER = (
    os.name == "nt" and importlib.util.find_spec("PyInstaller") is not None
)


def _load_process_authority():
    spec = importlib.util.spec_from_file_location(
        "_autosport_pyinstaller_process_authority_test",
        _PROCESS_AUTHORITY,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_dangerous_process_mask_blocks_handle_duplication_and_code_injection() -> None:
    authority = _load_process_authority()

    for access in (
        authority._PROCESS_CREATE_THREAD,
        authority._PROCESS_VM_OPERATION,
        authority._PROCESS_VM_WRITE,
        authority._PROCESS_DUP_HANDLE,
        authority._WRITE_DAC,
        authority._WRITE_OWNER,
    ):
        assert authority._DANGEROUS_PROCESS_ACCESS & access == access


def test_fresh_process_access_probe_uses_sibling_with_sedebug_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _load_process_authority()
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="DENIED\n", stderr="")

    monkeypatch.setattr(authority.subprocess, "run", fake_run)
    monkeypatch.setattr(authority.os, "getpid", lambda: 4242)

    assert authority._fresh_process_access_available(authority._PROCESS_VM_WRITE) is False
    command = captured["command"]
    assert isinstance(command, list)
    assert command[:4] == [authority.sys.executable, "-I", "-c", authority._FRESH_PROCESS_ACCESS_PROBE]
    assert command[-2:] == ["4242", str(authority._PROCESS_VM_WRITE)]
    assert "SeDebugPrivilege" in authority._FRESH_PROCESS_ACCESS_PROBE
    assert "AdjustTokenPrivileges" in authority._FRESH_PROCESS_ACCESS_PROBE
    assert "ERROR_NOT_ALL_ASSIGNED" in authority._FRESH_PROCESS_ACCESS_PROBE
    assert captured["kwargs"] == {
        "check": False,
        "capture_output": True,
        "text": True,
        "errors": "replace",
    }


def test_fresh_process_access_probe_reports_unexpected_available_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _load_process_authority()

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 10, stdout="AVAILABLE\n", stderr="")

    monkeypatch.setattr(authority.subprocess, "run", fake_run)

    assert authority._fresh_process_access_available(authority._PROCESS_CREATE_THREAD) is True


def test_fresh_process_access_probe_fails_closed_on_ambiguous_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _load_process_authority()

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="SEDEBUG_DISABLE_FAILED:5\n",
        )

    monkeypatch.setattr(authority.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="fresh same-token process-access sibling probe failed"):
        authority._fresh_process_access_available(authority._PROCESS_DUP_HANDLE)


def test_production_process_authority_requires_birth_protected_worker_boundary() -> None:
    authority = _PROCESS_AUTHORITY.read_text(encoding="utf-8")
    launcher = _LAUNCH_BOUNDARY.read_text(encoding="utf-8")

    assert "def _require_birth_protected_worker(" in authority
    assert "_require_birth_protected_worker(_query_system_handles)" in authority
    assert "creator_fence.acquire(launcher._current_user_sid())" in authority
    assert "launcher.protected_launch_attested()" in authority
    assert "launcher.relaunch_birth_protected_worker()" in authority
    assert authority.index(
        "creator_fence.acquire(launcher._current_user_sid())"
    ) < authority.index("launcher.relaunch_birth_protected_worker()")
    assert authority.rindex(
        "_require_birth_protected_worker(_query_system_handles)"
    ) > authority.index("class ProcessDuplicationFence")
    assert "CreateProcessW" in launcher
    assert "_birth_security_descriptor" in launcher
    assert "_DANGEROUS_PROCESS_ACCESS = 0x000C006A" in launcher
    assert "_close_handle(process_info.hThread)" in launcher
    assert "_close_handle(process_info.hProcess)" in launcher
    assert "set_event(barrier)" in launcher
    assert launcher.index("_close_handle(process_info.hThread)") < launcher.index("set_event(barrier)")
    assert launcher.index("_close_handle(process_info.hProcess)") < launcher.index("set_event(barrier)")


def test_preexisting_vm_write_authority_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _load_process_authority()
    current_pid = 101
    hostile_pid = 303
    identity_handle = 0x123
    process_object = 0xABCDEF

    monkeypatch.setattr(authority.os, "getpid", lambda: current_pid)
    monkeypatch.setattr(authority, "_open_self_identity_handle", lambda: identity_handle)
    monkeypatch.setattr(authority, "_close_handle", lambda _handle: None)

    fence = authority.ProcessDuplicationFence(
        lambda: [
            (
                process_object,
                current_pid,
                identity_handle,
                authority._PROCESS_QUERY_LIMITED_INFORMATION,
            ),
            (
                process_object,
                hostile_pid,
                0x456,
                authority._PROCESS_VM_WRITE,
            ),
        ]
    )

    with pytest.raises(RuntimeError, match="pre-existing external dangerous process handle"):
        fence._require_no_untrusted_preexisting_authority()


def test_parent_process_dangerous_broker_authority_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _load_process_authority()
    current_pid = 101
    parent_pid = 202
    identity_handle = 0x123
    process_object = 0xABCDEF

    monkeypatch.setattr(authority.os, "getpid", lambda: current_pid)
    monkeypatch.setattr(authority, "_open_self_identity_handle", lambda: identity_handle)
    monkeypatch.setattr(authority, "_close_handle", lambda _handle: None)

    fence = authority.ProcessDuplicationFence(
        lambda: [
            (
                process_object,
                current_pid,
                identity_handle,
                authority._PROCESS_QUERY_LIMITED_INFORMATION,
            ),
            (
                process_object,
                parent_pid,
                0x789,
                authority._PROCESS_DUP_HANDLE | authority._PROCESS_VM_WRITE,
            ),
        ]
    )

    with pytest.raises(RuntimeError, match="pre-existing external dangerous process handle"):
        fence._require_no_untrusted_preexisting_authority()


def test_current_process_dangerous_handles_are_not_external_brokers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _load_process_authority()
    current_pid = 101
    identity_handle = 0x123
    process_object = 0xABCDEF

    monkeypatch.setattr(authority.os, "getpid", lambda: current_pid)
    monkeypatch.setattr(authority, "_open_self_identity_handle", lambda: identity_handle)
    monkeypatch.setattr(authority, "_close_handle", lambda _handle: None)

    fence = authority.ProcessDuplicationFence(
        lambda: [
            (
                process_object,
                current_pid,
                identity_handle,
                authority._PROCESS_QUERY_LIMITED_INFORMATION,
            ),
            (
                process_object,
                current_pid,
                0x789,
                authority._PROCESS_DUP_HANDLE | authority._PROCESS_VM_WRITE,
            ),
        ]
    )

    fence._require_no_untrusted_preexisting_authority()


@pytest.mark.skipif(
    not _REAL_WINDOWS_PYINSTALLER,
    reason="real Windows prelaunch process-authority regression",
)
def test_protected_launcher_rejects_external_preexisting_dup_handle_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_TEST_PRELAUNCH_PROCESS_DUP_HANDLE_HELPER", "1")

    completed, _artifact, bound, digest = _PRODUCER_TESTS._run_real_pyinstaller_probe(
        tmp_path
    )

    assert completed.returncode != 0
    combined = completed.stdout + "\n" + completed.stderr
    assert "pre-existing external dangerous process handle" in combined
    assert not bound.exists()
    assert not digest.exists()


@pytest.mark.skipif(
    not _REAL_WINDOWS_PYINSTALLER,
    reason="real Windows protected-worker PyInstaller regression",
)
def test_protected_worker_rejects_retained_creator_process_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSPORT_TEST_RETAIN_CREATOR_PROCESS_HANDLE", "1")

    completed, _artifact, bound, digest = _PRODUCER_TESTS._run_real_pyinstaller_probe(
        tmp_path
    )

    assert completed.returncode != 0
    combined = completed.stdout + "\n" + completed.stderr
    assert "pre-existing external dangerous process handle" in combined
    assert not bound.exists()
    assert not digest.exists()
