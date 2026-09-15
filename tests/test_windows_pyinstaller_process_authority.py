from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_PROCESS_AUTHORITY = _ROOT / "scripts" / "guarded_pyinstaller_process_authority.py"


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


def test_preexisting_vm_write_authority_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _load_process_authority()
    current_pid = 101
    parent_pid = 202
    hostile_pid = 303
    identity_handle = 0x123
    process_object = 0xABCDEF

    monkeypatch.setattr(authority.os, "getpid", lambda: current_pid)
    monkeypatch.setattr(authority.os, "getppid", lambda: parent_pid)
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

    with pytest.raises(RuntimeError, match="pre-existing untrusted dangerous process handle"):
        fence._require_no_untrusted_preexisting_authority()


def test_parent_process_remains_explicitly_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _load_process_authority()
    current_pid = 101
    parent_pid = 202
    identity_handle = 0x123
    process_object = 0xABCDEF

    monkeypatch.setattr(authority.os, "getpid", lambda: current_pid)
    monkeypatch.setattr(authority.os, "getppid", lambda: parent_pid)
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

    fence._require_no_untrusted_preexisting_authority()
