from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionLedgerBusyError,
    ExecutionPlan,
    RealExecutionLedger,
)
from autosport.workspace_lock import WorkspaceEconomicLock


APPROVED_AT = "2026-10-04T08:00:02+00:00"
REVOKED_AT = "2026-10-04T08:00:03+00:00"
APPROVAL_FINGERPRINT = "a" * 64
APPROVAL_EVIDENCE = "b" * 64
REVOCATION_EVIDENCE = "c" * 64


def _ledger_with_active_approval(tmp_path):
    action = ExecutionAction(
        action_id="leg-1",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.23456789",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.00"),
        requested_stake=Decimal("1.00"),
        quote_id="quote-1",
        quote_observed_at="2026-10-04T08:00:00+00:00",
        expires_at="2026-10-04T08:05:00+00:00",
    )
    plan = ExecutionPlan(
        plan_id="plan-revocation-fence",
        bookmaker_profile_version="profile-v1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at="2026-10-04T08:00:01+00:00",
        actions=(action,),
    )
    ledger = RealExecutionLedger(tmp_path / "real-execution.jsonl")
    ledger.reserve_plan(plan)
    ledger.bind_supervised_approval(
        plan_id=plan.plan_id,
        approval_id=plan.approval_id,
        approval_fingerprint=APPROVAL_FINGERPRINT,
        approved_at=APPROVED_AT,
        evidence_sha256=APPROVAL_EVIDENCE,
    )
    return ledger, plan


def _revoke(ledger: RealExecutionLedger, plan: ExecutionPlan) -> None:
    ledger.revoke_supervised_approval(
        plan_id=plan.plan_id,
        approval_id=plan.approval_id,
        approval_fingerprint=APPROVAL_FINGERPRINT,
        revoked_at=REVOKED_AT,
        revocation_evidence_sha256=REVOCATION_EVIDENCE,
    )


def _is_active(ledger: RealExecutionLedger, plan: ExecutionPlan) -> bool:
    return ledger.supervised_approval_is_active(
        plan_id=plan.plan_id,
        approval_id=plan.approval_id,
        approval_fingerprint=APPROVAL_FINGERPRINT,
    )


def test_revocation_cannot_interleave_with_active_economic_write_fence(tmp_path) -> None:
    ledger, plan = _ledger_with_active_approval(tmp_path)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with WorkspaceEconomicLock(tmp_path):
            future = pool.submit(_revoke, ledger, plan)
            with pytest.raises(
                ExecutionLedgerBusyError,
                match="fenced by active economic execution",
            ):
                future.result()

    assert _is_active(ledger, plan) is True

    _revoke(ledger, plan)
    assert _is_active(ledger, plan) is False


def test_revocation_that_wins_fence_blocks_later_submission_authority(tmp_path) -> None:
    ledger, plan = _ledger_with_active_approval(tmp_path)

    _revoke(ledger, plan)

    assert _is_active(ledger, plan) is False
    assert ledger.verify_integrity() == 3


def test_context_manager_rebind_cannot_bypass_revocation_fence(
    tmp_path, monkeypatch
) -> None:
    ledger, plan = _ledger_with_active_approval(tmp_path)
    owner = WorkspaceEconomicLock(tmp_path)
    owner.acquire()
    try:
        monkeypatch.setattr(
            WorkspaceEconomicLock,
            "__enter__",
            lambda self: self,
        )
        with pytest.raises(
            ExecutionLedgerBusyError,
            match="fenced by active economic execution",
        ):
            _revoke(ledger, plan)
        assert _is_active(ledger, plan) is True
    finally:
        owner.release()
