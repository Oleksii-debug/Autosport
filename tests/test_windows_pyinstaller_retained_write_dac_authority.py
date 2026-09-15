from __future__ import annotations

import ctypes
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_SECURITY_AUTHORITY = _ROOT / "scripts" / "guarded_pyinstaller_security_authority.py"


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


def test_security_authority_source_audits_preexisting_write_dac_handles() -> None:
    source = _SECURITY_AUTHORITY.read_text(encoding="utf-8")

    assert "NtQuerySystemInformation" in source
    assert "_SYSTEM_EXTENDED_HANDLE_INFORMATION = 64" in source
    assert "pre-existing competing WRITE_DAC handle(s)" in source
    assert source.count("_require_no_competing_write_dac_handles(") >= 3
    assert "trusted expected-snapshot parent security fence" in source
    assert "trusted expected-snapshot file security fence" in source


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

        with pytest.raises(RuntimeError, match="pre-existing competing WRITE_DAC handle"):
            security._require_no_competing_write_dac_handles(
                trusted,
                label="retained WRITE_DAC regression",
            )

        assert close_handle(competing)
        competing = None

        security._require_no_competing_write_dac_handles(
            trusted,
            label="retained WRITE_DAC regression",
        )
    finally:
        if competing is not None:
            close_handle(competing)
        close_handle(trusted)
