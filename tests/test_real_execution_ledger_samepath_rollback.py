from __future__ import annotations

import pytest

from autosport.monotonic_workspace_authority import MonotonicAuthorityConflictError
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

    def fail_commit(**_kwargs) -> None:
        raise MonotonicAuthorityConflictError("injected commit interruption")

    monkeypatch.setattr(ledger._monotonic_authority, "commit", fail_commit)
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
    original_prepare = ledger._monotonic_authority.prepare
    competing_read_was_fenced = False

    def prepare_with_competing_read(**kwargs):
        nonlocal competing_read_was_fenced
        competing = RealExecutionLedger(path)
        with pytest.raises(ExecutionLedgerBusyError):
            competing.verified_snapshot()
        competing_read_was_fenced = True
        return original_prepare(**kwargs)

    monkeypatch.setattr(
        ledger._monotonic_authority,
        "prepare",
        prepare_with_competing_read,
    )

    assert ledger.reserve_plan(current_plan) == current_plan.fingerprint
    assert competing_read_was_fenced
    assert RealExecutionLedger(path).verified_snapshot().event_count == 1
