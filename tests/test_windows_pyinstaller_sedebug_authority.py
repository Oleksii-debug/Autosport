from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROCESS_AUTHORITY = ROOT / "scripts" / "guarded_pyinstaller_process_authority.py"


def _load_process_authority():
    module_name = "_autosport_test_guarded_pyinstaller_process_authority_sedebug"
    spec = importlib.util.spec_from_file_location(module_name, PROCESS_AUTHORITY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _completed(returncode: int, stdout: str, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["python", "-I", "-c", "probe"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_assigned_sedebug_is_not_accepted_as_dacl_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    authority = _load_process_authority()
    monkeypatch.setattr(
        authority.subprocess,
        "run",
        lambda *args, **kwargs: _completed(11, "SEDEBUG_ASSIGNED\n"),
    )

    with pytest.raises(RuntimeError, match="SeDebugPrivilege assigned"):
        authority._fresh_process_access_available(authority._PROCESS_VM_WRITE)


def test_absent_sedebug_keeps_denied_probe_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    authority = _load_process_authority()
    monkeypatch.setattr(
        authority.subprocess,
        "run",
        lambda *args, **kwargs: _completed(0, "DENIED\n"),
    )

    assert authority._fresh_process_access_available(authority._PROCESS_VM_WRITE) is False


def test_available_dangerous_access_still_fails_the_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    authority = _load_process_authority()
    monkeypatch.setattr(
        authority.subprocess,
        "run",
        lambda *args, **kwargs: _completed(10, "AVAILABLE\n"),
    )

    assert authority._fresh_process_access_available(authority._PROCESS_VM_WRITE) is True


def test_embedded_probe_requires_sedebug_absence_before_open_process() -> None:
    authority = _load_process_authority()
    probe = authority._FRESH_PROCESS_ACCESS_PROBE

    assert "if privilege_error == 0:" in probe
    assert 'print("SEDEBUG_ASSIGNED", flush=True)' in probe
    assert "raise SystemExit(11)" in probe
    assert "if privilege_error != ERROR_NOT_ALL_ASSIGNED:" in probe
    assert "privilege_error not in (0, ERROR_NOT_ALL_ASSIGNED)" not in probe
    assert probe.index("if privilege_error == 0:") < probe.index("handle = open_process")
