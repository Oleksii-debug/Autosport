from __future__ import annotations

from contextlib import contextmanager
import os
import threading

import pytest

import autosport.execution_stop_authority as stop_module
from autosport.execution_stop_authority import (
    ExecutionAuthorityMode,
    ExecutionStopAuthority,
    ExecutionStopIntegrityError,
    ExecutionStopStateError,
    ExecutionStoppedError,
)


def _authority(tmp_path):
    return ExecutionStopAuthority(tmp_path / "execution-stop.jsonl")


def _armed_authority(tmp_path):
    authority = _authority(tmp_path)
    authority.initialize_stopped(
        operator_id="owner",
        reason="initial safe state",
        command_id="lease-init",
    )
    armed = authority.arm(
        operator_id="owner",
        reason="supervised execution explicitly armed",
        confirmation_id="lease-confirmation",
        expected_revision=1,
        command_id="lease-arm",
    )
    return authority, armed


def test_admission_lease_missing_authority_fails_closed(tmp_path) -> None:
    authority = _authority(tmp_path)

    with pytest.raises(
        ExecutionStopStateError,
        match="STOP authority is missing",
    ):
        with authority.admission_lease():
            pytest.fail("missing STOP authority yielded an execution lease")


def test_admission_lease_stopped_authority_fails_closed(tmp_path) -> None:
    authority = _authority(tmp_path)
    stopped = authority.initialize_stopped(
        operator_id="owner",
        reason="operator STOP",
        command_id="lease-stopped-init",
    )

    with pytest.raises(
        ExecutionStoppedError,
        match=f"revision {stopped.revision}",
    ):
        with authority.admission_lease():
            pytest.fail("STOPPED authority yielded an execution lease")


def test_admission_lease_corrupt_authority_fails_closed(tmp_path) -> None:
    authority, _armed = _armed_authority(tmp_path)
    authority.anchor_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ExecutionStopIntegrityError):
        with authority.admission_lease():
            pytest.fail("corrupt STOP authority yielded an execution lease")


def test_admission_lease_yields_exact_armed_state(tmp_path) -> None:
    authority, armed = _armed_authority(tmp_path)

    with authority.admission_lease() as admitted:
        assert admitted == armed
        assert admitted.mode is ExecutionAuthorityMode.ARMED
        assert admitted.revision == 2

    assert authority.current() == armed


def test_admission_lease_rejects_current_unlocked_class_rebind(
    tmp_path,
    monkeypatch,
) -> None:
    authority = _authority(tmp_path)
    stopped = authority.initialize_stopped(
        operator_id="owner",
        reason="durable operator STOP",
        command_id="lease-lower-current-init",
    )
    forged_calls: list[str] = []

    def forged_current(_self):
        forged_calls.append("called")
        return type(stopped)(
            revision=stopped.revision,
            mode=ExecutionAuthorityMode.ARMED,
            command_id=stopped.command_id,
            operator_id=stopped.operator_id,
            reason="forged ARMED state",
            created_at=stopped.created_at,
            confirmation_id="forged-confirmation",
            record_sha256=stopped.record_sha256,
        )

    monkeypatch.setattr(
        ExecutionStopAuthority,
        "_current_unlocked",
        forged_current,
    )

    with pytest.raises(
        ExecutionStopIntegrityError,
        match="admission lower dispatch changed",
    ):
        with authority.admission_lease():
            pytest.fail("rebound current-state helper yielded an execution lease")

    assert forged_calls == []


def test_admission_lease_rejects_operation_lock_class_rebind(
    tmp_path,
    monkeypatch,
) -> None:
    authority, _armed = _armed_authority(tmp_path)
    forged_lock_entries: list[str] = []

    @contextmanager
    def no_op_operation_lock(_self):
        forged_lock_entries.append("entered")
        yield

    monkeypatch.setattr(
        ExecutionStopAuthority,
        "_authority_operation_lock",
        no_op_operation_lock,
    )

    with pytest.raises(
        ExecutionStopIntegrityError,
        match="admission lower dispatch changed",
    ):
        with authority.admission_lease():
            pytest.fail("rebound operation lock yielded an execution lease")

    assert forged_lock_entries == []


def test_admission_lease_holds_same_file_lock_until_effect_boundary(
    tmp_path,
    monkeypatch,
) -> None:
    primary, armed = _armed_authority(tmp_path)
    secondary = _authority(tmp_path)

    main_thread_id = threading.get_ident()
    worker_lock_attempted = threading.Event()
    worker_done = threading.Event()
    worker_errors: list[BaseException] = []
    original_file_lock = stop_module._exclusive_file_lock

    @contextmanager
    def observed_file_lock(path, *, guard_path=None):
        if threading.get_ident() != main_thread_id:
            worker_lock_attempted.set()
        with original_file_lock(path, guard_path=guard_path):
            yield

    monkeypatch.setattr(stop_module, "_exclusive_file_lock", observed_file_lock)

    def issue_stop() -> None:
        try:
            secondary.stop(
                operator_id="owner",
                reason="concurrent operator STOP",
                expected_revision=armed.revision,
                command_id="lease-concurrent-stop",
            )
        except BaseException as exc:  # preserve exact worker failure for assertion
            worker_errors.append(exc)
        finally:
            worker_done.set()

    worker = threading.Thread(target=issue_stop, daemon=True)

    with primary.admission_lease() as admitted:
        assert admitted == armed
        worker.start()
        assert worker_lock_attempted.wait(timeout=5)
        # The worker has reached the exact shared file-lock acquisition.  Because
        # this lease still owns that lock, STOP cannot have committed yet.
        assert worker_done.is_set() is False

    worker.join(timeout=5)
    assert worker.is_alive() is False
    assert worker_done.is_set() is True
    assert worker_errors == []

    stopped = primary.current()
    assert stopped.mode is ExecutionAuthorityMode.STOPPED
    assert stopped.revision == armed.revision + 1
    assert stopped.command_id == "lease-concurrent-stop"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX regression exercises atomic replacement of open workspace paths",
)
def test_admission_lease_stable_machine_lock_survives_dual_workspace_replacement(
    tmp_path,
    monkeypatch,
) -> None:
    primary, armed = _armed_authority(tmp_path)
    secondary = _authority(tmp_path)

    main_thread_id = threading.get_ident()
    worker_stable_lock_attempted = threading.Event()
    worker_done = threading.Event()
    worker_errors: list[BaseException] = []
    original_file_lock = stop_module._exclusive_file_lock
    stable_lock_path = primary._stable_serialization_lock_path()

    assert secondary._stable_serialization_lock_path() == stable_lock_path
    assert stable_lock_path != primary._lock_path
    assert stable_lock_path.parent != primary.path.parent

    @contextmanager
    def observed_file_lock(path, *, guard_path=None):
        if (
            threading.get_ident() != main_thread_id
            and path == stable_lock_path
        ):
            worker_stable_lock_attempted.set()
        with original_file_lock(path, guard_path=guard_path):
            yield

    monkeypatch.setattr(stop_module, "_exclusive_file_lock", observed_file_lock)

    journal_bytes = primary.path.read_bytes()
    sidecar_bytes = primary._lock_path.read_bytes()

    def issue_stop() -> None:
        try:
            secondary.stop(
                operator_id="owner",
                reason="STOP after dual workspace replacement",
                expected_revision=armed.revision,
                command_id="lease-dual-replacement-stop",
            )
        except BaseException as exc:  # preserve exact worker failure for assertion
            worker_errors.append(exc)
        finally:
            worker_done.set()

    worker = threading.Thread(target=issue_stop, daemon=True)

    with primary.admission_lease() as admitted:
        assert admitted == armed

        replacement_journal = tmp_path / "replacement-execution-stop.jsonl"
        replacement_journal.write_bytes(journal_bytes)
        os.replace(replacement_journal, primary.path)

        replacement_sidecar = tmp_path / "replacement-execution-stop.lock"
        replacement_sidecar.write_bytes(sidecar_bytes)
        os.replace(replacement_sidecar, primary._lock_path)

        worker.start()
        assert worker_stable_lock_attempted.wait(timeout=5)
        # The mutable workspace lock and journal now name different inodes, but
        # both instances still contend on the same machine-root namespace lock.
        assert worker_done.is_set() is False

    worker.join(timeout=5)
    assert worker.is_alive() is False
    assert worker_done.is_set() is True
    assert worker_errors == []

    stopped = primary.current()
    assert stopped.mode is ExecutionAuthorityMode.STOPPED
    assert stopped.revision == armed.revision + 1
    assert stopped.command_id == "lease-dual-replacement-stop"
