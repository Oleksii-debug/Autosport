from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
from ctypes import wintypes
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_SECURITY_AUTHORITY = _ROOT / "scripts" / "guarded_pyinstaller_security_authority.py"


def _load_security_authority():
    spec = importlib.util.spec_from_file_location(
        "_autosport_uninspectable_handle_noise_security_authority_test",
        _SECURITY_AUTHORITY,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _snapshot(security, rows: list[tuple[int, int, int, int]], object_type: int):
    snapshot = security._SystemHandleSnapshot()
    for row in rows:
        snapshot.append(row)
        snapshot.object_types[(row[0], row[1], row[2])] = object_type
    return snapshot


def _uninspectable_case(security, *, candidate_pid: int):
    current_pid = os.getpid()
    target_object = 0x40404040
    candidate_object = 0x50505050
    trusted_handle = 0x444
    candidate_handle = 0x555
    object_type = 37
    target_identity = (0xABCDEF, b"T" * 16)
    snapshot = _snapshot(
        security,
        [
            (target_object, current_pid, trusted_handle, security._WRITE_DAC),
            (candidate_object, candidate_pid, candidate_handle, security._WRITE_DAC),
        ],
        object_type,
    )
    return snapshot, trusted_handle, candidate_handle, target_identity


def test_uninspectable_same_type_noise_is_not_target_evidence_but_fileid_match_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _load_security_authority()
    current_pid = os.getpid()
    target_object = 0x10101010
    noise_object = 0x20202020
    competitor_object = 0x30303030
    trusted_handle = 0x111
    noise_handle = 0x222
    competitor_handle = 0x333
    object_type = 37
    target_identity = (0xABCDEF, b"T" * 16)
    with_competitor = True

    def query_handles():
        rows = [
            (target_object, current_pid, trusted_handle, security._WRITE_DAC),
            (noise_object, current_pid + 100, noise_handle, security._WRITE_DAC),
        ]
        if with_competitor:
            rows.append(
                (
                    competitor_object,
                    current_pid + 101,
                    competitor_handle,
                    security._WRITE_DAC,
                )
            )
        return _snapshot(security, rows, object_type)

    monkeypatch.setattr(security, "_query_system_handles", query_handles)
    monkeypatch.setattr(security, "_file_identity", lambda _handle: target_identity)

    def candidate_identity(pid: int, handle_value: int) -> tuple[int, bytes]:
        if handle_value == noise_handle:
            assert pid == current_pid + 100
            raise security._CandidateFileIdentityUnavailable(
                "synthetic unrelated privileged handle"
            )
        assert pid == current_pid + 101
        assert handle_value == competitor_handle
        return target_identity

    monkeypatch.setattr(security, "_candidate_file_identity", candidate_identity)
    monkeypatch.setattr(
        security,
        "_snapshot_row_still_present",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        security,
        "_process_same_user_scope",
        lambda: {
            current_pid: True,
            current_pid + 100: False,
            current_pid + 101: True,
        },
    )

    with pytest.raises(RuntimeError, match="pre-existing competing mutation-capable handle"):
        security._require_no_competing_mutation_handles(
            trusted_handle,
            label="uninspectable noise plus proven competitor",
            directory=False,
        )

    with_competitor = False
    security._require_no_competing_mutation_handles(
        trusted_handle,
        label="uninspectable different-user noise without target evidence",
        directory=False,
    )


def test_uninspectable_same_user_candidate_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _load_security_authority()
    current_pid = os.getpid()
    candidate_pid = current_pid + 200
    snapshot, trusted_handle, candidate_handle, target_identity = _uninspectable_case(
        security,
        candidate_pid=candidate_pid,
    )

    monkeypatch.setattr(security, "_query_system_handles", lambda: snapshot)
    monkeypatch.setattr(security, "_file_identity", lambda _handle: target_identity)

    def candidate_identity(pid: int, handle_value: int) -> tuple[int, bytes]:
        assert pid == candidate_pid
        assert handle_value == candidate_handle
        raise security._CandidateFileIdentityUnavailable("synthetic same-user candidate")

    monkeypatch.setattr(security, "_candidate_file_identity", candidate_identity)
    monkeypatch.setattr(
        security,
        "_snapshot_row_still_present",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        security,
        "_process_same_user_scope",
        lambda: {current_pid: True, candidate_pid: True},
    )

    with pytest.raises(RuntimeError, match="same-user uninspectable mutation-capable handle"):
        security._require_no_competing_mutation_handles(
            trusted_handle,
            label="same-user uninspectable candidate",
            directory=False,
        )


def test_uninspectable_unknown_owner_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _load_security_authority()
    current_pid = os.getpid()
    candidate_pid = current_pid + 201
    snapshot, trusted_handle, candidate_handle, target_identity = _uninspectable_case(
        security,
        candidate_pid=candidate_pid,
    )

    monkeypatch.setattr(security, "_query_system_handles", lambda: snapshot)
    monkeypatch.setattr(security, "_file_identity", lambda _handle: target_identity)

    def candidate_identity(pid: int, handle_value: int) -> tuple[int, bytes]:
        assert pid == candidate_pid
        assert handle_value == candidate_handle
        raise security._CandidateFileIdentityUnavailable("synthetic unknown-owner candidate")

    monkeypatch.setattr(security, "_candidate_file_identity", candidate_identity)
    monkeypatch.setattr(
        security,
        "_snapshot_row_still_present",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        security,
        "_process_same_user_scope",
        lambda: {current_pid: True},
    )

    with pytest.raises(RuntimeError, match="unknown process owner"):
        security._require_no_competing_mutation_handles(
            trusted_handle,
            label="unknown-owner uninspectable candidate",
            directory=False,
        )


@pytest.mark.skipif(os.name != "nt", reason="real Windows unfiltered handle-table regression")
def test_unfiltered_windows_handle_table_passes_after_proven_competitor_closes(
    tmp_path: Path,
) -> None:
    security = _load_security_authority()
    target = tmp_path / "expected-snapshot.exe"
    target.write_bytes(b"MZ-autosport-unfiltered-handle-table-regression")

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
        # Deliberately use the production, unfiltered NtQuerySystemInformation snapshot.
        # On hosted Windows this includes unrelated privileged handles such as PID 4.
        with pytest.raises(RuntimeError, match="pre-existing competing mutation-capable handle"):
            security._require_no_competing_mutation_handles(
                trusted,
                label="unfiltered retained WRITE_DAC regression",
                directory=False,
            )

        assert close_handle(competing)
        competing = None

        # Ambient inaccessible different-user/system handles are not evidence that they
        # reference this target. Same-user or unknown-owner candidates remain fail-closed.
        security._require_no_competing_mutation_handles(
            trusted,
            label="unfiltered retained WRITE_DAC regression",
            directory=False,
        )
    finally:
        if competing is not None:
            close_handle(competing)
        close_handle(trusted)