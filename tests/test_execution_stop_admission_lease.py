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
        match="admission helper graph changed",
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
        match="admission helper graph changed",
    ):
        with authority.admission_lease():
            pytest.fail("rebound operation lock yielded an execution lease")

    assert forged_lock_entries == []


def test_admission_lease_rejects_second_order_lock_path_rebind(
    tmp_path,
    monkeypatch,
) -> None:
    authority, _armed = _armed_authority(tmp_path)
    forged_calls: list[str] = []

    def forged_stable_lock_path(_self):
        forged_calls.append("called")
        return tmp_path / "attacker-selected.lock"

    monkeypatch.setattr(
        ExecutionStopAuthority,
        "_stable_serialization_lock_path",
        forged_stable_lock_path,
    )

    with pytest.raises(
        ExecutionStopIntegrityError,
        match="admission helper graph changed",
    ):
        with authority.admission_lease():
            pytest.fail("rebound lock-path helper yielded an execution lease")

    assert forged_calls == []


def test_admission_lease_rejects_second_order_journal_reader_rebind(
    tmp_path,
    monkeypatch,
) -> None:
    authority = _authority(tmp_path)
    stopped = authority.initialize_stopped(
        operator_id="owner",
        reason="durable operator STOP",
        command_id="lease-graph-journal-init",
    )
    forged_calls: list[str] = []

    def forged_journal(_self, **_kwargs):
        forged_calls.append("called")
        return [
            {
                "revision": stopped.revision,
                "mode": ExecutionAuthorityMode.ARMED.value,
                "command_id": stopped.command_id,
                "operator_id": stopped.operator_id,
                "reason": "forged ARMED record",
                "created_at": stopped.created_at,
                "confirmation_id": "forged-confirmation",
                "record_sha256": stopped.record_sha256,
            }
        ]

    monkeypatch.setattr(
        ExecutionStopAuthority,
        "_read_journal_unlocked",
        forged_journal,
    )

    with pytest.raises(
        ExecutionStopIntegrityError,
        match="admission helper graph changed",
    ):
        with authority.admission_lease():
            pytest.fail("rebound journal reader yielded an execution lease")

    assert forged_calls == []


def test_admission_lease_rejects_module_current_alias_rebind(
    tmp_path,
    monkeypatch,
) -> None:
    authority = _authority(tmp_path)
    stopped = authority.initialize_stopped(
        operator_id="owner",
        reason="durable operator STOP",
        command_id="lease-module-current-init",
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
        stop_module,
        "_CANONICAL_ADMISSION_CURRENT_UNLOCKED",
        forged_current,
    )

    with pytest.raises(
        ExecutionStopIntegrityError,
        match="module dependency graph changed",
    ):
        with authority.admission_lease():
            pytest.fail("module alias rebind yielded an execution lease")

    assert forged_calls == []


def test_admission_lease_rejects_module_operation_lock_alias_rebind(
    tmp_path,
    monkeypatch,
) -> None:
    authority, _armed = _armed_authority(tmp_path)
    forged_entries: list[str] = []

    @contextmanager
    def no_op_lock(_self):
        forged_entries.append("entered")
        yield

    monkeypatch.setattr(
        stop_module,
        "_CANONICAL_ADMISSION_OPERATION_LOCK",
        no_op_lock,
    )

    with pytest.raises(
        ExecutionStopIntegrityError,
        match="module dependency graph changed",
    ):
        with authority.admission_lease():
            pytest.fail("module lock alias rebind yielded an execution lease")

    assert forged_entries == []


def test_admission_lease_rejects_module_graph_verifier_rebind(
    tmp_path,
    monkeypatch,
) -> None:
    authority, _armed = _armed_authority(tmp_path)
    forged_calls: list[str] = []

    def no_op_verifier() -> None:
        forged_calls.append("called")

    monkeypatch.setattr(
        stop_module,
        "_require_canonical_admission_graph",
        no_op_verifier,
    )

    with pytest.raises(
        ExecutionStopIntegrityError,
        match="module dependency graph changed",
    ):
        with authority.admission_lease():
            pytest.fail("module verifier rebind yielded an execution lease")

    assert forged_calls == []


def test_admission_lease_holds_same_file_lock_until_effect_boundary(
    tmp_path,
) -> None:
    primary, armed = _armed_authority(tmp_path)
    secondary = _authority(tmp_path)

    worker_started = threading.Event()
    worker_done = threading.Event()
    worker_errors: list[BaseException] = []

    def issue_stop() -> None:
        worker_started.set()
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
        assert worker_started.wait(timeout=5)
        # Give the already-running STOP worker a bounded chance to complete.
        # It must remain blocked while this lease owns the shared OS locks.
        assert worker_done.wait(timeout=0.5) is False

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
) -> None:
    primary, armed = _armed_authority(tmp_path)
    secondary = _authority(tmp_path)

    worker_started = threading.Event()
    worker_done = threading.Event()
    worker_errors: list[BaseException] = []
    stable_lock_path = primary._stable_serialization_lock_path()

    assert secondary._stable_serialization_lock_path() == stable_lock_path
    assert stable_lock_path != primary._lock_path
    assert stable_lock_path.parent != primary.path.parent

    journal_bytes = primary.path.read_bytes()
    sidecar_bytes = primary._lock_path.read_bytes()

    def issue_stop() -> None:
        worker_started.set()
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
        assert worker_started.wait(timeout=5)
        # Even after both mutable workspace paths are rebound, the worker must
        # remain serialized by the independent machine-root lock.
        assert worker_done.wait(timeout=0.5) is False

    worker.join(timeout=5)
    assert worker.is_alive() is False
    assert worker_done.is_set() is True
    assert worker_errors == []

    stopped = primary.current()
    assert stopped.mode is ExecutionAuthorityMode.STOPPED
    assert stopped.revision == armed.revision + 1
    assert stopped.command_id == "lease-dual-replacement-stop"

