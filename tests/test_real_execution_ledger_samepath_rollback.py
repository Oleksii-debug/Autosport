from __future__ import annotations

import threading

import pytest

from autosport.monotonic_workspace_authority import MonotonicAuthorityIntegrityError
from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionLedgerBusyError,
    ExecutionLedgerIntegrityError,
    ExecutionPlan,
    RealExecutionLedger,
)


TS = "2026-09-17T19:28:00+00:00"
RESERVED_AT = "2026-09-17T19:28:10+00:00"
EXPIRES_AT = "2026-09-17T19:29:00+00:00"


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="action-rollback",
        bookmaker_id="betfair",
        account_id="acct-rollback",
        event_id="event-rollback",
        market_id="market-rollback",
        selection_id="selection-rollback",
        side="BACK",
        requested_odds="2.50",
        requested_stake="10.00",
        quote_id="quote-rollback",
        quote_observed_at=TS,
        expires_at=EXPIRES_AT,
    )


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan-rollback",
        bookmaker_profile_version="profile-v1",
        decision_id="decision-rollback",
        approval_id="approval-rollback",
        created_at=TS,
        actions=(_action(),),
    )


def _isolated_ledger_path(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    authority_root = tmp_path / "machine-authority"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root),
    )
    return workspace / "execution.jsonl"


def test_same_path_valid_prefix_rollback_cannot_become_current_after_restart(
    tmp_path,
    monkeypatch,
) -> None:
    path = _isolated_ledger_path(tmp_path, monkeypatch)
    ledger = RealExecutionLedger(path)
    current_plan = _plan()

    ledger.reserve_plan(current_plan)
    snapshot_s1 = ledger.verified_snapshot()
    bytes_s1 = path.read_bytes()

    ledger.begin_attempt(
        plan_id=current_plan.plan_id,
        action_id=current_plan.actions[0].action_id,
        attempt_id="attempt-rollback",
        reserved_at=RESERVED_AT,
    )
    snapshot_s2 = ledger.verified_snapshot()

    assert snapshot_s2.sha256 != snapshot_s1.sha256
    assert snapshot_s2.event_count > snapshot_s1.event_count

    # Simulate a complete-valid workspace rollback: the protected ledger path is
    # restored to a byte-exact older prefix while the independent machine-state
    # high-water authority must still remember that S2 was durably published.
    path.write_bytes(bytes_s1)
    reopened = RealExecutionLedger(path)

    with pytest.raises(
        ExecutionLedgerIntegrityError,
        match="rollback|monotonic|older|high-water|committed",
    ):
        reopened.verified_snapshot()



def test_same_path_complete_deletion_cannot_become_pristine_after_restart(
    tmp_path,
    monkeypatch,
) -> None:
    path = _isolated_ledger_path(tmp_path, monkeypatch)
    ledger = RealExecutionLedger(path)
    ledger.reserve_plan(_plan())
    committed = ledger.verified_snapshot()
    assert committed.event_count == 1

    path.unlink()
    reopened = RealExecutionLedger(path)

    with pytest.raises(
        ExecutionLedgerIntegrityError,
        match="rollback|monotonic|older|high-water|committed",
    ):
        reopened.verified_snapshot()


def test_durable_append_with_interrupted_authority_commit_recovers_exact_tip(
    tmp_path,
    monkeypatch,
) -> None:
    path = _isolated_ledger_path(tmp_path, monkeypatch)
    ledger = RealExecutionLedger(path)
    current_plan = _plan()

    original_append_record = ledger._monotonic_authority._append_record
    append_calls = 0

    def interrupt_second_authority_record(record, index) -> None:
        nonlocal append_calls
        append_calls += 1
        if append_calls == 2:
            raise MonotonicAuthorityIntegrityError("injected commit interruption")
        original_append_record(record, index)

    monkeypatch.setattr(
        ledger._monotonic_authority,
        "_append_record",
        interrupt_second_authority_record,
    )
    with pytest.raises(
        ExecutionLedgerIntegrityError,
        match="monotonic COMMIT failed",
    ):
        ledger.reserve_plan(current_plan)

    durable_bytes = path.read_bytes()
    assert durable_bytes

    reopened = RealExecutionLedger(path)
    recovered = reopened.verified_snapshot()

    assert recovered.payload == durable_bytes
    assert recovered.event_count == 1
    assert reopened.reserve_plan(current_plan) == current_plan.fingerprint
    assert reopened.verified_snapshot().event_count == 1


def test_valid_ledger_bytes_cannot_bootstrap_missing_independent_authority(
    tmp_path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    authority_root = tmp_path / "machine-authority"
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root),
    )
    source_path = source_root / "execution.jsonl"
    source = RealExecutionLedger(source_path)
    source.reserve_plan(_plan())
    valid_bytes = source_path.read_bytes()

    copied_root = tmp_path / "copied"
    copied_root.mkdir()
    copied_path = copied_root / "execution.jsonl"
    copied_path.write_bytes(valid_bytes)
    copied = RealExecutionLedger(copied_path)

    with pytest.raises(
        ExecutionLedgerIntegrityError,
        match="rollback|monotonic|authority",
    ):
        copied.verified_snapshot()



def test_pristine_authoritative_read_does_not_create_ledger_file(
    tmp_path,
    monkeypatch,
) -> None:
    path = _isolated_ledger_path(tmp_path, monkeypatch)
    ledger = RealExecutionLedger(path)

    snapshot = ledger.verified_snapshot()

    assert snapshot.payload == b""
    assert snapshot.event_count == 0
    assert not path.exists()



def test_reader_cannot_abort_active_writer_prepare_window(
    tmp_path,
    monkeypatch,
) -> None:
    path = _isolated_ledger_path(tmp_path, monkeypatch)
    ledger = RealExecutionLedger(path)
    current_plan = _plan()
    original_append_record = ledger._monotonic_authority._append_record
    competing_read_was_fenced = False
    append_calls = 0

    def append_record_with_competing_read(record, index) -> None:
        nonlocal competing_read_was_fenced, append_calls
        append_calls += 1
        original_append_record(record, index)
        if append_calls == 1:
            competing = RealExecutionLedger(path)
            with pytest.raises(ExecutionLedgerBusyError):
                competing.verified_snapshot()
            competing_read_was_fenced = True

    monkeypatch.setattr(
        ledger._monotonic_authority,
        "_append_record",
        append_record_with_competing_read,
    )

    assert ledger.reserve_plan(current_plan) == current_plan.fingerprint
    assert competing_read_was_fenced
    assert RealExecutionLedger(path).verified_snapshot().event_count == 1



def test_instance_recover_shadow_cannot_accept_rolled_back_bytes(
    tmp_path,
    monkeypatch,
) -> None:
    path = _isolated_ledger_path(tmp_path, monkeypatch)
    ledger = RealExecutionLedger(path)
    current_plan = _plan()
    ledger.reserve_plan(current_plan)
    bytes_s1 = path.read_bytes()
    ledger.begin_attempt(
        plan_id=current_plan.plan_id,
        action_id=current_plan.actions[0].action_id,
        attempt_id="attempt-shadow",
        reserved_at=RESERVED_AT,
    )

    path.write_bytes(bytes_s1)
    reopened = RealExecutionLedger(path)
    monkeypatch.setattr(
        reopened._monotonic_authority,
        "recover",
        lambda **_kwargs: None,
    )

    with pytest.raises(
        ExecutionLedgerIntegrityError,
        match="rollback|monotonic|authority",
    ):
        reopened.verified_snapshot()



def test_instance_prepare_and_commit_shadows_do_not_gain_transition_authority(
    tmp_path,
    monkeypatch,
) -> None:
    path = _isolated_ledger_path(tmp_path, monkeypatch)
    ledger = RealExecutionLedger(path)
    current_plan = _plan()

    def forbidden_shadow(**_kwargs):
        raise AssertionError("instance transition shadow must not be invoked")

    monkeypatch.setattr(ledger._monotonic_authority, "prepare", forbidden_shadow)
    monkeypatch.setattr(ledger._monotonic_authority, "commit", forbidden_shadow)

    assert ledger.reserve_plan(current_plan) == current_plan.fingerprint
    assert ledger.verified_snapshot().event_count == 1



def test_same_instance_other_thread_cannot_recover_active_prepare(
    tmp_path,
    monkeypatch,
) -> None:
    path = _isolated_ledger_path(tmp_path, monkeypatch)
    ledger = RealExecutionLedger(path)
    current_plan = _plan()
    original_append_record = ledger._monotonic_authority._append_record

    prepare_durable = threading.Event()
    release_writer = threading.Event()
    reader_started = threading.Event()
    reader_finished = threading.Event()
    writer_errors: list[BaseException] = []
    reader_errors: list[BaseException] = []
    reader_counts: list[int] = []
    append_calls = 0

    def block_after_prepare(record, index) -> None:
        nonlocal append_calls
        append_calls += 1
        original_append_record(record, index)
        if append_calls == 1:
            prepare_durable.set()
            if not release_writer.wait(10):
                raise AssertionError("timed out waiting to release writer")

    monkeypatch.setattr(
        ledger._monotonic_authority,
        "_append_record",
        block_after_prepare,
    )

    def run_writer() -> None:
        try:
            ledger.reserve_plan(current_plan)
        except BaseException as exc:  # pragma: no cover - thread handoff
            writer_errors.append(exc)

    def run_reader() -> None:
        reader_started.set()
        try:
            reader_counts.append(ledger.verified_snapshot().event_count)
        except BaseException as exc:  # pragma: no cover - thread handoff
            reader_errors.append(exc)
        finally:
            reader_finished.set()

    writer = threading.Thread(target=run_writer, daemon=True)
    writer.start()
    assert prepare_durable.wait(10), "writer never reached durable PREPARE"

    reader = threading.Thread(target=run_reader, daemon=True)
    reader.start()
    assert reader_started.wait(10), "reader thread never started"

    # The same ledger instance owns serialization in the writer thread. The reader
    # must remain fenced behind the instance RLock instead of treating that writer's
    # PREPARE as its own and aborting it.
    assert not reader_finished.wait(0.2)

    release_writer.set()
    writer.join(10)
    reader.join(10)

    assert not writer.is_alive()
    assert not reader.is_alive()
    assert not writer_errors
    assert not reader_errors
    assert reader_counts == [1]
    assert RealExecutionLedger(path).verified_snapshot().event_count == 1
