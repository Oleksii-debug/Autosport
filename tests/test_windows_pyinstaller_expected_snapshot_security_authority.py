from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_GUARDED_PYINSTALLER = _ROOT / "scripts" / "guarded_pyinstaller_bind.py"
_SECURITY_AUTHORITY = _ROOT / "scripts" / "guarded_pyinstaller_security_authority.py"
_PRODUCER_TEST_PATH = _ROOT / "tests" / "test_windows_pyinstaller_producer_handoff.py"
_SPEC = importlib.util.spec_from_file_location(
    "_autosport_pyinstaller_producer_handoff_tests_security_authority",
    _PRODUCER_TEST_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
_PRODUCER_TESTS = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _PRODUCER_TESTS
_SPEC.loader.exec_module(_PRODUCER_TESTS)

_REAL_WINDOWS_PYINSTALLER = (
    os.name == "nt" and importlib.util.find_spec("PyInstaller") is not None
)


def test_expected_snapshot_security_authority_is_handle_restored_and_nonrevocable() -> None:
    wrapper = _GUARDED_PYINSTALLER.read_text(encoding="utf-8")
    hardening = _SECURITY_AUTHORITY.read_text(encoding="utf-8")

    installer = wrapper.index("def _install_expected_snapshot_security_authority_hardening(")
    run = wrapper.index("def run(", installer)
    install_call = wrapper.index(
        "_install_expected_snapshot_security_authority_hardening()",
        run,
    )
    capture_core_hooks = wrapper.index(
        "original_set_fence = _CORE._set_expected_snapshot_write_fence",
        install_call,
    )

    open_authority = hardening.index("def open_security_authority(")
    handle_restore = hardening.index("def restore_dacl_through_handle(", open_authority)
    owner_rights_lock = hardening.index("def install_owner_write_dac_lock(", handle_restore)
    namespace = hardening.index("def hardened_namespace_fence(", owner_rights_lock)
    file_fence = hardening.index("def hardened_set_file_fence(", namespace)
    hostile_probe = hardening.index("def run_hostile_child_probe(", file_fence)
    require = hardening.index("def hardened_require_access_denied(", hostile_probe)

    assert install_call < capture_core_hooks
    assert open_authority < handle_restore < owner_rights_lock < namespace < file_fence
    assert file_fence < hostile_probe < require
    assert '_OWNER_RIGHTS_SID = "S-1-3-4"' in hardening
    assert 'f"*{_OWNER_RIGHTS_SID}:(WDAC)"' in hardening
    assert "SetSecurityInfo" in hardening
    assert "restore_dacl_through_handle(authority[\"handle\"], descriptor)" in hardening
    assert "trusted expected-snapshot parent security fence still allows fresh WRITE_DAC" in hardening
    assert "trusted expected-snapshot file security fence still allows fresh WRITE_DAC" in hardening
    assert "post-EndUpdateResource ACL write exclusion" in hardening
    assert "same-token DACL rewrite unexpectedly succeeded" in hardening


@pytest.mark.skipif(
    not _REAL_WINDOWS_PYINSTALLER,
    reason="real Windows PyInstaller regression",
)
def test_post_end_resource_security_authority_blocks_same_token_dacl_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_TEST_REWRITE_EXPECTED_DACL_AFTER_RESOURCE_END",
        "1",
    )

    completed, _artifact, bound, digest = _PRODUCER_TESTS._run_real_pyinstaller_probe(
        tmp_path
    )

    assert completed.returncode != 0
    combined = completed.stdout + "\n" + completed.stderr
    assert (
        "post-EndUpdateResource security authority blocked hostile same-token DACL rewrite before oracle"
        in combined
    )
    assert "same-token expected-snapshot security adversary did not fail closed" not in combined
    assert not bound.exists()
    assert not digest.exists()
