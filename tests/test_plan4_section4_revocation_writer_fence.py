"""Fixture-only regression: durable approval revoke and irreversible POST share a fence.

No real bookmaker account, credentials, provider transport or money effects.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import test_betfair_supervised_execution as fixtures
from autosport.real_execution_ledger import RealExecutionLedger
from autosport.supervised_execution import revoke_supervised_approval
from autosport.workspace_lock import WorkspaceEconomicLock, WorkspaceEconomicLockBusyError


def test_supervised_revoke_cannot_commit_during_provider_send_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reuse the same canonical plan/approval and mock owner authority as
    # the existing Betfair fixture tests. No sender is invoked in this test.
    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: fixtures.RESERVED_AT,
    )
    _profile, bound, approval, ledger, _action, _store = fixtures._prepared(
        str(tmp_path)
    )
    identity = dict(
        plan_id=bound.execution_plan.plan_id,
        approval_id=approval.ledger_identity,
        approval_fingerprint=approval.fingerprint,
    )
    competitor = RealExecutionLedger(ledger.path)
    assert competitor.supervised_approval_is_active(**identity)

    # execute_betfair_supervised_action owns this lock across its single
    # transport POST. Even a *different* ledger instance may not durably
    # revoke approval between its final check and the irreversible POST.
    with WorkspaceEconomicLock(tmp_path):
        with pytest.raises(WorkspaceEconomicLockBusyError):
            revoke_supervised_approval(
                competitor,
                bound,
                approval,
                revocation_evidence_sha256="f" * 64,
            )
        assert RealExecutionLedger(ledger.path).supervised_approval_is_active(
            **identity
        )

    # Once the send fence ends, the same genuine revocation commits,
    # and the durable decision remains revoked after re-opening the ledger.
    revoke_supervised_approval(
        competitor,
        bound,
        approval,
        revocation_evidence_sha256="f" * 64,
    )
    assert not ledger.supervised_approval_is_active(**identity)
    assert not RealExecutionLedger(ledger.path).supervised_approval_is_active(
        **identity
    )
