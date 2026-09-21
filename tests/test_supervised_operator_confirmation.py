from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import json

import pytest

from autosport.economic_goal_provenance import provenance_for
from autosport.economic_goal_store import EconomicGoalStore
from autosport.owner_economic_authority import (
    INITIAL_OWNER_FORM_DEFAULTS,
    build_initial_owner_contract,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan
from autosport.supervised_execution import (
    ApprovalState,
    BoundSupervisedExecutionPlan,
    ExecutionLegConstraint,
    ProfileBinding,
    SupervisedApproval,
    _bound_binding_sha256,
)
from autosport.supervised_operator_confirmation import (
    SupervisedOperatorConfirmationError,
    SupervisedOperatorConfirmationService,
)


def _authority_root(tmp_path, monkeypatch) -> None:
    root = tmp_path.parent / f"{tmp_path.name}-monotonic-authority"
    monkeypatch.setenv("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", str(root))


def _make_inputs(tmp_path, monkeypatch, *, emergency_stop: bool = False):
    _authority_root(tmp_path, monkeypatch)
    values = dict(INITIAL_OWNER_FORM_DEFAULTS)
    owner = build_initial_owner_contract(values, emergency_stop=emergency_stop)
    EconomicGoalStore(tmp_path).initialize_owner(owner)
    owner_sha = provenance_for(owner).contract_sha256

    portfolio_sha = "1" * 64
    intent_sha = "2" * 64
    action_id = "5" * 64
    approval = SupervisedApproval(
        approval_id="approval-operator-1",
        portfolio_plan_sha256=portfolio_sha,
        intent_id="intent-operator-1",
        routing_request_id="routing-operator-1",
        execution_terms_sha256="3" * 64,
        approved_at="2026-01-01T00:00:00+00:00",
        expires_at="2099-01-01T00:00:00+00:00",
        evidence_sha256="4" * 64,
        state=ApprovalState.APPROVED,
    )
    action = ExecutionAction(
        action_id=action_id,
        bookmaker_id="betfair",
        account_id="account-redacted",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side="BACK",
        requested_odds=Decimal("2.10"),
        requested_stake=Decimal("10"),
        quote_id="quote-1",
        quote_observed_at="2026-09-20T12:00:00+00:00",
        expires_at="2098-12-31T23:59:00+00:00",
    )
    profile = ProfileBinding(
        venue_id="betfair",
        account_id="account-redacted",
        adapter_id="betfair-exchange",
        adapter_version="1",
        profile_version=1,
        profile_sha256="6" * 64,
    )
    constraint = ExecutionLegConstraint(
        leg_id=action_id,
        side="BACK",
        quote_expires_at=action.expires_at,
        max_slippage_fraction=Decimal("0"),
    )
    provisional = ExecutionPlan(
        plan_id="pending-supervised-v2-binding",
        bookmaker_profile_version="profile-set-test",
        decision_id=f"portfolio:{portfolio_sha}:intent:{intent_sha}",
        approval_id=approval.ledger_identity,
        created_at="2026-09-20T12:00:01+00:00",
        actions=(action,),
    )
    bridge = _bound_binding_sha256(
        provisional,
        portfolio_sha,
        owner_sha,
        approval.intent_id,
        intent_sha,
        approval.fingerprint,
        (profile,),
        (constraint,),
    )
    execution = ExecutionPlan(
        plan_id=f"supervised-v2-{bridge}",
        bookmaker_profile_version=provisional.bookmaker_profile_version,
        decision_id=provisional.decision_id,
        approval_id=provisional.approval_id,
        created_at=provisional.created_at,
        actions=provisional.actions,
    )
    bound = BoundSupervisedExecutionPlan(
        execution_plan=execution,
        portfolio_plan_sha256=portfolio_sha,
        economic_goal_contract_sha256=owner_sha,
        intent_id=approval.intent_id,
        intent_sha256=intent_sha,
        approval_fingerprint=approval.fingerprint,
        profile_bindings=(profile,),
        constraints=(constraint,),
    )
    return bound, approval


def test_review_is_non_durable_and_confirm_survives_restart(tmp_path, monkeypatch):
    bound, approval = _make_inputs(tmp_path, monkeypatch)
    service = SupervisedOperatorConfirmationService(tmp_path)

    review = service.review(bound=bound, approval=approval)

    assert not (tmp_path / "supervised-operator-confirmations").exists()

    receipt = service.confirm(review)
    reopened = SupervisedOperatorConfirmationService(tmp_path)
    resolved = reopened.resolve_for_execution(bound=bound, approval=approval)

    assert resolved == receipt
    assert receipt.plan_id == bound.execution_plan.plan_id
    assert receipt.plan_fingerprint == bound.execution_plan.fingerprint
    assert receipt.owner_contract_sha256 == bound.economic_goal_contract_sha256
    assert receipt.approval_fingerprint == approval.fingerprint
    assert receipt.review_sha256 == review.review_sha256


def test_reconstructed_review_cannot_mint_confirmation(tmp_path, monkeypatch):
    bound, approval = _make_inputs(tmp_path, monkeypatch)
    service = SupervisedOperatorConfirmationService(tmp_path)
    review = service.review(bound=bound, approval=approval)
    forged_copy = replace(review)

    with pytest.raises(
        SupervisedOperatorConfirmationError,
        match="exact live REVIEW object",
    ):
        service.confirm(forged_copy)

    assert not (tmp_path / "supervised-operator-confirmations").exists()


def test_restart_before_confirm_requires_fresh_review(tmp_path, monkeypatch):
    bound, approval = _make_inputs(tmp_path, monkeypatch)
    first = SupervisedOperatorConfirmationService(tmp_path)
    review = first.review(bound=bound, approval=approval)

    restarted = SupervisedOperatorConfirmationService(tmp_path)
    with pytest.raises(
        SupervisedOperatorConfirmationError,
        match="exact live REVIEW object",
    ):
        restarted.confirm(review)

    fresh = restarted.review(bound=bound, approval=approval)
    assert fresh.review_sha256 != review.review_sha256
    restarted.confirm(fresh)


def test_new_review_invalidates_older_review(tmp_path, monkeypatch):
    bound, approval = _make_inputs(tmp_path, monkeypatch)
    service = SupervisedOperatorConfirmationService(tmp_path)
    first = service.review(bound=bound, approval=approval)
    second = service.review(bound=bound, approval=approval)

    with pytest.raises(
        SupervisedOperatorConfirmationError,
        match="exact live REVIEW object",
    ):
        service.confirm(first)

    service.confirm(second)


def test_failed_revalidation_consumes_review(tmp_path, monkeypatch):
    bound, approval = _make_inputs(tmp_path, monkeypatch)
    service = SupervisedOperatorConfirmationService(tmp_path)
    review = service.review(bound=bound, approval=approval)
    owner_path = EconomicGoalStore(tmp_path).path
    original = owner_path.read_bytes()
    owner_path.write_text("{}", encoding="utf-8")

    with pytest.raises(
        SupervisedOperatorConfirmationError,
        match="current durable owner authority",
    ):
        service.confirm(review)

    owner_path.write_bytes(original)
    with pytest.raises(
        SupervisedOperatorConfirmationError,
        match="exact live REVIEW object",
    ):
        service.confirm(review)


def test_duplicate_confirm_is_idempotent_not_second_issuance(tmp_path, monkeypatch):
    bound, approval = _make_inputs(tmp_path, monkeypatch)
    service = SupervisedOperatorConfirmationService(tmp_path)
    review = service.review(bound=bound, approval=approval)

    first = service.confirm(review)
    second = service.confirm(review)

    assert second == first
    directory = tmp_path / "supervised-operator-confirmations"
    assert [path.name for path in directory.iterdir()] == [
        f"{bound.execution_plan.plan_id}.json"
    ]


def test_durable_receipt_tamper_fails_closed(tmp_path, monkeypatch):
    bound, approval = _make_inputs(tmp_path, monkeypatch)
    service = SupervisedOperatorConfirmationService(tmp_path)
    receipt = service.confirm(service.review(bound=bound, approval=approval))
    path = (
        tmp_path
        / "supervised-operator-confirmations"
        / f"{bound.execution_plan.plan_id}.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["receipt"]["approval_evidence_sha256"] = "9" * 64
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SupervisedOperatorConfirmationError):
        SupervisedOperatorConfirmationService(tmp_path).resolve_for_execution(
            bound=bound,
            approval=approval,
        )

    assert receipt.approval_evidence_sha256 == approval.evidence_sha256


def test_durable_receipt_deletion_is_detected_after_restart(tmp_path, monkeypatch):
    bound, approval = _make_inputs(tmp_path, monkeypatch)
    service = SupervisedOperatorConfirmationService(tmp_path)
    service.confirm(service.review(bound=bound, approval=approval))
    path = (
        tmp_path
        / "supervised-operator-confirmations"
        / f"{bound.execution_plan.plan_id}.json"
    )
    path.unlink()

    with pytest.raises(
        SupervisedOperatorConfirmationError,
        match="deleted or rolled back",
    ):
        SupervisedOperatorConfirmationService(tmp_path).resolve_for_execution(
            bound=bound,
            approval=approval,
        )


def test_owner_stop_blocks_review_before_any_receipt(tmp_path, monkeypatch):
    bound, approval = _make_inputs(
        tmp_path,
        monkeypatch,
        emergency_stop=True,
    )
    service = SupervisedOperatorConfirmationService(tmp_path)

    with pytest.raises(
        SupervisedOperatorConfirmationError,
        match="emergency STOP",
    ):
        service.review(bound=bound, approval=approval)

    assert not (tmp_path / "supervised-operator-confirmations").exists()


def test_changed_approval_cannot_rebind_existing_receipt(tmp_path, monkeypatch):
    bound, approval = _make_inputs(tmp_path, monkeypatch)
    service = SupervisedOperatorConfirmationService(tmp_path)
    service.confirm(service.review(bound=bound, approval=approval))
    changed = replace(approval, evidence_sha256="8" * 64)

    with pytest.raises(
        SupervisedOperatorConfirmationError,
        match="approval does not exactly bind",
    ):
        SupervisedOperatorConfirmationService(tmp_path).resolve_for_execution(
            bound=bound,
            approval=changed,
        )
