from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.betfair_pre_provider_recovery import (
    BetfairPreProviderRecoveryError,
    recover_betfair_pre_provider_attempt,
)
from autosport.real_execution_ledger import (
    AttemptState,
    ExecutionAction,
    ExecutionPlan,
    ExecutionStateError,
    RealExecutionLedger,
)
from autosport.supervised_execution import (
    BoundSupervisedExecutionPlan,
    ExecutionLegConstraint,
    ProfileBinding,
    SupervisedApproval,
    _bound_binding_sha256,
    begin_supervised_attempt,
    reserve_supervised_plan,
)


APPROVED_AT = "2026-01-01T00:00:00+00:00"
APPROVAL_EXPIRES_AT = "2099-01-01T00:00:00+00:00"
QUOTE_OBSERVED_AT = "2026-01-01T00:00:01+00:00"
QUOTE_EXPIRES_AT = "2099-01-01T00:00:00+00:00"
CREATED_AT = "2026-01-01T00:00:02+00:00"
ACTION_ID = "a" * 64


def _future_observed_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(
        timespec="microseconds"
    )


def _bound() -> tuple[BoundSupervisedExecutionPlan, SupervisedApproval]:
    approval = SupervisedApproval(
        approval_id="approval-pre-provider-recovery",
        portfolio_plan_sha256="1" * 64,
        intent_id="intent-pre-provider-recovery",
        routing_request_id="route-pre-provider-recovery",
        execution_terms_sha256="2" * 64,
        approved_at=APPROVED_AT,
        expires_at=APPROVAL_EXPIRES_AT,
        evidence_sha256="3" * 64,
    )
    action = ExecutionAction(
        action_id=ACTION_ID,
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.23456789",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.0"),
        requested_stake=Decimal("10"),
        quote_id="quote-1",
        quote_observed_at=QUOTE_OBSERVED_AT,
        expires_at=QUOTE_EXPIRES_AT,
    )
    plan = ExecutionPlan(
        plan_id="placeholder",
        bookmaker_profile_version="profile-set-v1-test",
        decision_id="decision-pre-provider-recovery",
        approval_id=approval.ledger_identity,
        created_at=CREATED_AT,
        actions=(action,),
    )
    binding = ProfileBinding(
        venue_id="betfair",
        account_id="acct-1",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        profile_version=1,
        profile_sha256="4" * 64,
    )
    constraint = ExecutionLegConstraint(
        leg_id=ACTION_ID,
        side="BACK",
        quote_expires_at=QUOTE_EXPIRES_AT,
        max_slippage_fraction=Decimal("0.05"),
    )
    economic_goal_sha = "5" * 64
    intent_sha = "6" * 64
    plan_id = "supervised-v2-" + _bound_binding_sha256(
        plan,
        approval.portfolio_plan_sha256,
        economic_goal_sha,
        approval.intent_id,
        intent_sha,
        approval.fingerprint,
        (binding,),
        (constraint,),
    )
    plan = replace(plan, plan_id=plan_id)
    bound = BoundSupervisedExecutionPlan(
        execution_plan=plan,
        portfolio_plan_sha256=approval.portfolio_plan_sha256,
        economic_goal_contract_sha256=economic_goal_sha,
        intent_id=approval.intent_id,
        intent_sha256=intent_sha,
        approval_fingerprint=approval.fingerprint,
        profile_bindings=(binding,),
        constraints=(constraint,),
    )
    return bound, approval


def _reserved_ledger(path):
    bound, approval = _bound()
    ledger = RealExecutionLedger(path)
    reserve_supervised_plan(ledger, bound, approval)
    begin_supervised_attempt(
        ledger,
        bound,
        approval,
        action_id=ACTION_ID,
        attempt_id="attempt-1",
    )
    return ledger, bound, approval


def test_restart_before_provider_reference_releases_exactly_one_retry(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    ledger, bound, approval = _reserved_ledger(path)

    # Simulate a fresh process after the durable reservation but before the Betfair
    # writer bound its deterministic customerOrderRef.
    restarted = RealExecutionLedger(path)
    assert restarted.recover_uncertain() == ("attempt-1",)
    assert restarted.attempt_state("attempt-1") is AttemptState.UNKNOWN
    assert not restarted.can_retry_action(
        plan_id=bound.execution_plan.plan_id,
        action_id=ACTION_ID,
    )

    result = recover_betfair_pre_provider_attempt(
        restarted,
        bound,
        approval,
        attempt_id="attempt-1",
        observed_at=_future_observed_at(),
    )

    assert result.state is AttemptState.RECONCILED_NOT_FOUND
    assert restarted.attempt_state("attempt-1") is AttemptState.RECONCILED_NOT_FOUND
    assert restarted.provider_order_reference(
        attempt_id="attempt-1",
        provider_id="betfair",
    ) is None
    assert restarted.can_retry_action(
        plan_id=bound.execution_plan.plan_id,
        action_id=ACTION_ID,
    )

    retry = begin_supervised_attempt(
        restarted,
        bound,
        approval,
        action_id=ACTION_ID,
        attempt_id="attempt-2",
    )
    assert retry.attempt_id == "attempt-2"
    assert restarted.attempt_state("attempt-2") is AttemptState.RESERVED
    assert not restarted.can_retry_action(
        plan_id=bound.execution_plan.plan_id,
        action_id=ACTION_ID,
    )
    with pytest.raises(ExecutionStateError, match="unresolved/final attempt"):
        begin_supervised_attempt(
            restarted,
            bound,
            approval,
            action_id=ACTION_ID,
            attempt_id="attempt-3",
        )


def test_recovery_is_idempotent_for_same_product_issued_proof(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    ledger, bound, approval = _reserved_ledger(path)
    ledger.recover_uncertain()
    observed_at = _future_observed_at()

    first = recover_betfair_pre_provider_attempt(
        ledger,
        bound,
        approval,
        attempt_id="attempt-1",
        observed_at=observed_at,
    )
    event_count = ledger.verify_integrity()
    second = recover_betfair_pre_provider_attempt(
        ledger,
        bound,
        approval,
        attempt_id="attempt-1",
        observed_at=_future_observed_at(),
    )

    assert second == first
    assert ledger.verify_integrity() == event_count


def test_provider_reference_boundary_still_requires_verified_readback(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    ledger, bound, approval = _reserved_ledger(path)
    provider_ref = ledger.bind_provider_order_reference(
        attempt_id="attempt-1",
        provider_id="betfair",
    )
    assert provider_ref
    restarted = RealExecutionLedger(path)
    restarted.recover_uncertain()

    with pytest.raises(
        BetfairPreProviderRecoveryError,
        match="provider/submission/evidence boundary",
    ):
        recover_betfair_pre_provider_attempt(
            restarted,
            bound,
            approval,
            attempt_id="attempt-1",
            observed_at=_future_observed_at(),
        )

    assert restarted.attempt_state("attempt-1") is AttemptState.UNKNOWN
    assert not restarted.can_retry_action(
        plan_id=bound.execution_plan.plan_id,
        action_id=ACTION_ID,
    )


def test_submitted_boundary_still_requires_verified_readback(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    ledger, bound, approval = _reserved_ledger(path)
    ledger.bind_provider_order_reference(
        attempt_id="attempt-1",
        provider_id="betfair",
    )
    ledger.mark_submitted("attempt-1")
    restarted = RealExecutionLedger(path)
    restarted.recover_uncertain()

    with pytest.raises(
        BetfairPreProviderRecoveryError,
        match="provider/submission/evidence boundary",
    ):
        recover_betfair_pre_provider_attempt(
            restarted,
            bound,
            approval,
            attempt_id="attempt-1",
            observed_at=_future_observed_at(),
        )

    assert restarted.attempt_state("attempt-1") is AttemptState.UNKNOWN
    assert not restarted.can_retry_action(
        plan_id=bound.execution_plan.plan_id,
        action_id=ACTION_ID,
    )


def test_transport_ambiguity_unknown_cannot_bypass_provider_readback(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    ledger, bound, approval = _reserved_ledger(path)
    ledger.bind_provider_order_reference(
        attempt_id="attempt-1",
        provider_id="betfair",
    )
    ledger.mark_submitted("attempt-1")
    ledger.mark_unknown("attempt-1", reason="ambiguous_transport")

    with pytest.raises(
        BetfairPreProviderRecoveryError,
        match="provider/submission/evidence boundary",
    ):
        recover_betfair_pre_provider_attempt(
            ledger,
            bound,
            approval,
            attempt_id="attempt-1",
            observed_at=_future_observed_at(),
        )

    assert ledger.attempt_state("attempt-1") is AttemptState.UNKNOWN
    assert not ledger.can_retry_action(
        plan_id=bound.execution_plan.plan_id,
        action_id=ACTION_ID,
    )


def test_revoked_durable_approval_blocks_pre_provider_recovery(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    ledger, bound, approval = _reserved_ledger(path)
    ledger.recover_uncertain()
    ledger.revoke_supervised_approval(
        plan_id=bound.execution_plan.plan_id,
        approval_id=approval.ledger_identity,
        approval_fingerprint=approval.fingerprint,
        revoked_at=_future_observed_at(),
        revocation_evidence_sha256="7" * 64,
    )

    with pytest.raises(
        BetfairPreProviderRecoveryError,
        match="missing or revoked",
    ):
        recover_betfair_pre_provider_attempt(
            ledger,
            bound,
            approval,
            attempt_id="attempt-1",
            observed_at=_future_observed_at(),
        )
