from __future__ import annotations

import pytest

from autosport.monotonic_workspace_authority import (
    MonotonicAuthorityConflictError,
    MonotonicWorkspaceAuthority,
)
from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionLedgerIntegrityError,
    ExecutionPlan,
    RealExecutionLedger,
)


TS = "2026-09-17T19:28:00+00:00"
RESERVED_AT = "2026-09-17T19:28:10+00:00"
EXPIRES_AT = "2026-09-17T19:29:00+00:00"


def _isolate_authority(tmp_path, monkeypatch) -> None:
    authority_root = (tmp_path / "machine-state").resolve()
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root),
    )


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


def test_same_path_valid_prefix_rollback_cannot_become_current_after_restart(
    tmp_path,
    monkeypatch,
) -> None:
    _isolate_authority(tmp_path, monkeypatch)
    path = tmp_path / "workspace" / "execution.jsonl"
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

    # Complete-valid same-path rollback must not erase a later durable high-water tip.
    path.write_bytes(bytes_s1)
    reopened = RealExecutionLedger(path)

    with pytest.raises(ExecutionLedgerIntegrityError, match="monotonic"):
        reopened.verified_snapshot()

    # Snapshot-CAS admission must consume the same writer-owned currentness
    # authority rather than accepting the rolled-back bytes merely because their
    # hash matches the caller's old snapshot.
    with pytest.raises(ExecutionLedgerIntegrityError, match="monotonic"):
        reopened.begin_attempt(
            plan_id=current_plan.plan_id,
            action_id=current_plan.actions[0].action_id,
            attempt_id="attempt-after-rollback",
            reserved_at=RESERVED_AT,
            expected_snapshot_sha256=snapshot_s1.sha256,
        )
    assert path.read_bytes() == bytes_s1


def test_crash_after_local_publish_recovers_exact_prepared_tip(
    tmp_path,
    monkeypatch,
) -> None:
    _isolate_authority(tmp_path, monkeypatch)
    path = tmp_path / "workspace" / "execution.jsonl"
    ledger = RealExecutionLedger(path)
    current_plan = _plan()
    original_commit = MonotonicWorkspaceAuthority.commit
    fail_once = True

    def _crash_after_publish(self, **kwargs):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise MonotonicAuthorityConflictError(
                "simulated crash after local publish"
            )
        return original_commit(self, **kwargs)

    monkeypatch.setattr(
        MonotonicWorkspaceAuthority,
        "commit",
        _crash_after_publish,
    )
    with pytest.raises(ExecutionLedgerIntegrityError, match="monotonic"):
        ledger.reserve_plan(current_plan)
    assert path.exists()
    durable_after_failure = path.read_bytes()
    assert durable_after_failure

    monkeypatch.setattr(
        MonotonicWorkspaceAuthority,
        "commit",
        original_commit,
    )
    reopened = RealExecutionLedger(path)
    snapshot = reopened.verified_snapshot()

    assert snapshot.payload == durable_after_failure
    assert snapshot.event_count == 1
    assert reopened.saga(current_plan.plan_id).plan_id == current_plan.plan_id


def test_deleted_ledger_cannot_rebootstrap_after_committed_tip(
    tmp_path,
    monkeypatch,
) -> None:
    _isolate_authority(tmp_path, monkeypatch)
    path = tmp_path / "workspace" / "execution.jsonl"
    ledger = RealExecutionLedger(path)
    ledger.reserve_plan(_plan())
    assert ledger.verified_snapshot().event_count == 1

    path.unlink()
    reopened = RealExecutionLedger(path)
    with pytest.raises(ExecutionLedgerIntegrityError, match="monotonic"):
        reopened.verified_snapshot()
    assert not path.exists()


def test_valid_legacy_nonempty_ledger_establishes_exact_tofu_baseline(
    tmp_path,
    monkeypatch,
) -> None:
    _isolate_authority(tmp_path, monkeypatch)
    current_plan = _plan()

    source_path = tmp_path / "source-workspace" / "source.jsonl"
    source = RealExecutionLedger(source_path)
    source.reserve_plan(current_plan)
    legacy_bytes = source_path.read_bytes()

    legacy_path = tmp_path / "legacy-workspace" / "execution.jsonl"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_bytes(legacy_bytes)

    legacy = RealExecutionLedger(legacy_path)
    baseline = legacy.verified_snapshot()
    assert baseline.payload == legacy_bytes
    assert baseline.event_count == 1

    source.begin_attempt(
        plan_id=current_plan.plan_id,
        action_id=current_plan.actions[0].action_id,
        attempt_id="attempt-successor",
        reserved_at=RESERVED_AT,
    )
    different_valid_bytes = source_path.read_bytes()
    assert different_valid_bytes != legacy_bytes

    legacy_path.write_bytes(different_valid_bytes)
    with pytest.raises(ExecutionLedgerIntegrityError, match="monotonic"):
        RealExecutionLedger(legacy_path).verified_snapshot()


def test_invalid_legacy_bytes_are_not_blessed_as_tofu_baseline(
    tmp_path,
    monkeypatch,
) -> None:
    _isolate_authority(tmp_path, monkeypatch)
    legacy_path = tmp_path / "legacy-workspace" / "execution.jsonl"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_bytes(b'{"not":"a-ledger-envelope"}\n')

    with pytest.raises(ExecutionLedgerIntegrityError):
        RealExecutionLedger(legacy_path).verified_snapshot()

    source_path = tmp_path / "source-workspace" / "source.jsonl"
    source = RealExecutionLedger(source_path)
    source.reserve_plan(_plan())
    valid_bytes = source_path.read_bytes()
    legacy_path.write_bytes(valid_bytes)

    snapshot = RealExecutionLedger(legacy_path).verified_snapshot()
    assert snapshot.payload == valid_bytes
    assert snapshot.event_count == 1
