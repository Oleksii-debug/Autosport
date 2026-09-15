from __future__ import annotations

import importlib.util
import os
import sys
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


def test_primary_thread_birth_mask_covers_required_mutation_rights() -> None:
    spec = importlib.util.spec_from_file_location(
        "_autosport_guarded_pyinstaller_launch_boundary_thread_mask_test",
        _LAUNCH_BOUNDARY,
    )
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = launcher
    spec.loader.exec_module(launcher)

    for access in (
        launcher._THREAD_SET_CONTEXT,
        launcher._THREAD_SUSPEND_RESUME,
        launcher._WRITE_DAC,
        launcher._WRITE_OWNER,
    ):
        assert launcher._DANGEROUS_THREAD_ACCESS & access == access


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
