from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_SECURITY_AUTHORITY = _ROOT / "scripts" / "guarded_pyinstaller_security_authority.py"


def _load_security_authority():
    spec = importlib.util.spec_from_file_location(
        "_autosport_retained_handle_file_identity_test",
        _SECURITY_AUTHORITY,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _snapshot(security, *, trusted_object: int, candidate_object: int):
    current_pid = os.getpid()
    trusted_handle = 0x111
    candidate_pid = current_pid + 101
    candidate_handle = 0x222
    snapshot = security._SystemHandleSnapshot()
    snapshot.extend(
        [
            (trusted_object, current_pid, trusted_handle, security._WRITE_DAC),
            (candidate_object, candidate_pid, candidate_handle, security._WRITE_DAC),
        ]
    )
    object_type = 37
    snapshot.object_types[(trusted_object, current_pid, trusted_handle)] = object_type
    snapshot.object_types[(candidate_object, candidate_pid, candidate_handle)] = object_type
    return snapshot, trusted_handle, candidate_pid, candidate_handle


def test_distinct_file_objects_with_same_file_id_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _load_security_authority()
    snapshot, trusted_handle, candidate_pid, candidate_handle = _snapshot(
        security,
        trusted_object=0xAAA0,
        candidate_object=0xBBB0,
    )
    identity = (0x1234, b"A" * 16)

    monkeypatch.setattr(security, "_query_system_handles", lambda: snapshot)
    monkeypatch.setattr(security, "_file_identity", lambda _handle: identity)

    inspected: list[tuple[int, int]] = []

    def candidate_identity(pid: int, handle: int):
        inspected.append((pid, handle))
        return identity

    monkeypatch.setattr(security, "_candidate_file_identity", candidate_identity)

    with pytest.raises(RuntimeError, match="pre-existing competing mutation-capable handle"):
        security._require_no_competing_mutation_handles(
            trusted_handle,
            label="stable file identity regression",
            directory=False,
        )

    assert inspected == [(candidate_pid, candidate_handle)]


def test_distinct_file_objects_with_different_file_ids_are_not_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _load_security_authority()
    snapshot, trusted_handle, _candidate_pid, _candidate_handle = _snapshot(
        security,
        trusted_object=0xAAA1,
        candidate_object=0xBBB1,
    )

    monkeypatch.setattr(security, "_query_system_handles", lambda: snapshot)
    monkeypatch.setattr(
        security,
        "_file_identity",
        lambda _handle: (0x1234, b"A" * 16),
    )
    monkeypatch.setattr(
        security,
        "_candidate_file_identity",
        lambda _pid, _handle: (0x1234, b"B" * 16),
    )

    security._require_no_competing_mutation_handles(
        trusted_handle,
        label="unrelated retained handle",
        directory=False,
    )


def test_uninspectable_live_candidate_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _load_security_authority()
    snapshot, trusted_handle, _candidate_pid, _candidate_handle = _snapshot(
        security,
        trusted_object=0xAAA2,
        candidate_object=0xBBB2,
    )
    monkeypatch.setattr(security, "_query_system_handles", lambda: snapshot)
    monkeypatch.setattr(
        security,
        "_file_identity",
        lambda _handle: (0x1234, b"A" * 16),
    )

    def unavailable(_pid: int, _handle: int):
        raise security._CandidateFileIdentityUnavailable("duplication denied")

    monkeypatch.setattr(security, "_candidate_file_identity", unavailable)

    with pytest.raises(RuntimeError, match="live uninspectable mutation-capable handle"):
        security._require_no_competing_mutation_handles(
            trusted_handle,
            label="uninspectable retained handle",
            directory=False,
        )


def test_uninspectable_candidate_may_be_ignored_only_after_positive_disappearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security = _load_security_authority()
    snapshot, trusted_handle, _candidate_pid, _candidate_handle = _snapshot(
        security,
        trusted_object=0xAAA3,
        candidate_object=0xBBB3,
    )
    refreshed = security._SystemHandleSnapshot()
    trusted_row = snapshot[0]
    refreshed.append(trusted_row)
    refreshed.object_types[
        (trusted_row[0], trusted_row[1], trusted_row[2])
    ] = snapshot.object_types[(trusted_row[0], trusted_row[1], trusted_row[2])]
    snapshots = iter([snapshot, refreshed])

    monkeypatch.setattr(security, "_query_system_handles", lambda: next(snapshots))
    monkeypatch.setattr(
        security,
        "_file_identity",
        lambda _handle: (0x1234, b"A" * 16),
    )

    def unavailable(_pid: int, _handle: int):
        raise security._CandidateFileIdentityUnavailable("handle closed during inspection")

    monkeypatch.setattr(security, "_candidate_file_identity", unavailable)

    security._require_no_competing_mutation_handles(
        trusted_handle,
        label="disappeared retained handle",
        directory=False,
    )
