from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
import pickle

import pytest

import autosport.smarkets_execution_approval as sut
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan
from autosport.supervised_confirmation import SupervisedConfirmationAuthority
from autosport.supervised_execution import (
    BoundSupervisedExecutionPlan,
    ExecutionLegConstraint,
    ProfileBinding,
    SupervisedApproval,
    _bound_binding_sha256,
)


class FakeClock:
    def __init__(self, value: str = "2026-09-22T12:10:00+00:00") -> None:
        self.value = datetime.fromisoformat(value)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _approval(*, expires_at: str = "2026-09-22T13:00:00+00:00") -> SupervisedApproval:
    return SupervisedApproval(
        approval_id="approval-1",
        portfolio_plan_sha256="1" * 64,
        intent_id="intent-1",
        routing_request_id="routing-1",
        execution_terms_sha256="2" * 64,
        approved_at="2026-09-22T12:00:00+00:00",
        expires_at=expires_at,
        evidence_sha256="3" * 64,
    )


def _action(
    *,
    action_id: str = "a" * 64,
    bookmaker_id: str = "smarkets",
    account_id: str = "account-1",
    selection_id: str = "contract-1",
) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id=bookmaker_id,
        account_id=account_id,
        event_id="event-1",
        market_id="market-1",
        selection_id=selection_id,
        side="BACK",
        requested_odds=Decimal("2.00"),
        requested_stake=Decimal("10.00"),
        quote_id=f"quote-{selection_id}",
        quote_observed_at="2026-09-22T12:05:00+00:00",
        expires_at="2026-09-22T12:30:00+00:00",
    )


def _bound(
    approval: SupervisedApproval,
    *,
    bookmaker_id: str = "smarkets",
    duplicate_same_account: bool = False,
) -> BoundSupervisedExecutionPlan:
    first = _action(bookmaker_id=bookmaker_id)
    actions = (first,)
    constraints = (
        ExecutionLegConstraint(
            leg_id=first.action_id,
            side="BACK",
            quote_expires_at=first.expires_at,
            max_slippage_fraction=Decimal("0.01"),
        ),
    )
    if duplicate_same_account:
        second = _action(
            action_id="b" * 64,
            bookmaker_id=bookmaker_id,
            selection_id="contract-2",
        )
        actions += (second,)
        constraints += (
            ExecutionLegConstraint(
                leg_id=second.action_id,
                side="BACK",
                quote_expires_at=second.expires_at,
                max_slippage_fraction=Decimal("0.01"),
            ),
        )

    profile_bindings = (
        ProfileBinding(
            venue_id=bookmaker_id,
            account_id="account-1",
            adapter_id="provider-adapter",
            adapter_version="1",
            profile_version=1,
            profile_sha256="4" * 64,
        ),
    )
    decision_id = "portfolio:decision-1:intent:intent-1"
    provisional = ExecutionPlan(
        plan_id="pending-supervised-v2-binding",
        bookmaker_profile_version="profile-set-v1-test",
        decision_id=decision_id,
        approval_id=approval.ledger_identity,
        created_at="2026-09-22T12:06:00+00:00",
        actions=actions,
    )
    bridge_id = _bound_binding_sha256(
        provisional,
        approval.portfolio_plan_sha256,
        "5" * 64,
        approval.intent_id,
        "6" * 64,
        approval.fingerprint,
        profile_bindings,
        constraints,
    )
    execution = ExecutionPlan(
        plan_id=f"supervised-v2-{bridge_id}",
        bookmaker_profile_version=provisional.bookmaker_profile_version,
        decision_id=provisional.decision_id,
        approval_id=provisional.approval_id,
        created_at=provisional.created_at,
        actions=actions,
    )
    return BoundSupervisedExecutionPlan(
        execution_plan=execution,
        portfolio_plan_sha256=approval.portfolio_plan_sha256,
        economic_goal_contract_sha256="5" * 64,
        intent_id=approval.intent_id,
        intent_sha256="6" * 64,
        approval_fingerprint=approval.fingerprint,
        profile_bindings=profile_bindings,
        constraints=constraints,
    )


def _confirmed(
    authority: SupervisedConfirmationAuthority,
    bound: BoundSupervisedExecutionPlan,
    approval: SupervisedApproval,
    *,
    decision_sha256: str | None = None,
    approval_evidence_sha256: str | None = None,
    ttl_seconds: int = 120,
):
    action = bound.execution_plan.actions[0]
    review = authority.prepare_review(
        review_id="review-1",
        decision_id=bound.execution_plan.decision_id,
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        decision_sha256=decision_sha256 or bound.execution_plan.fingerprint,
        approval_evidence_sha256=approval_evidence_sha256 or approval.evidence_sha256,
        risk_evidence_sha256="7" * 64,
        review_payload={
            "execution_plan_id": bound.execution_plan.plan_id,
            "action_id": action.action_id,
            "requested_odds": str(action.requested_odds),
            "requested_stake": str(action.requested_stake),
        },
        ttl_seconds=ttl_seconds,
    )
    receipt = authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )
    return review, receipt


def _patch_authority(monkeypatch, authority: SupervisedConfirmationAuthority) -> None:
    monkeypatch.setattr(sut, "_open_confirmation_authority", lambda: authority)


def test_consumed_product_confirmation_issues_restart_resolvable_smarkets_approval(
    tmp_path, monkeypatch
):
    clock = FakeClock()
    authority = SupervisedConfirmationAuthority(
        tmp_path / "supervised-confirmation.jsonl",
        clock=clock,
    )
    approval = _approval()
    bound = _bound(approval)
    review, receipt = _confirmed(authority, bound, approval)
    _patch_authority(monkeypatch, authority)

    witness = sut.consume_smarkets_execution_approval(
        bound,
        approval,
        action_id="a" * 64,
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
    )

    assert witness.action_id == "a" * 64
    assert witness.execution_plan_id == bound.execution_plan.plan_id
    assert witness.decision_sha256 == bound.execution_plan.fingerprint
    assert witness.approval_fingerprint == approval.fingerprint
    assert witness.approval_evidence_sha256 == approval.evidence_sha256
    assert witness.risk_evidence_sha256 == "7" * 64
    assert witness.receipt_id == receipt.receipt_id
    assert witness.consumer_key.startswith("smarkets-execution-approval:v1:")
    assert len(witness.evidence_id) == 64

    with pytest.raises(sut.SmarketsExecutionApprovalError, match="could not be consumed"):
        sut.consume_smarkets_execution_approval(
            bound,
            approval,
            action_id="a" * 64,
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
        )

    reopened = SupervisedConfirmationAuthority(authority.path, clock=clock)
    _patch_authority(monkeypatch, reopened)
    restored = sut.resolve_consumed_smarkets_execution_approval(
        bound,
        approval,
        action_id="a" * 64,
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
    )
    assert restored.evidence_id == witness.evidence_id
    assert restored.consumer_key == witness.consumer_key


def test_wrong_durable_decision_digest_fails_before_receipt_consumption(
    tmp_path, monkeypatch
):
    authority = SupervisedConfirmationAuthority(
        tmp_path / "supervised-confirmation.jsonl",
        clock=FakeClock(),
    )
    approval = _approval()
    bound = _bound(approval)
    review, receipt = _confirmed(
        authority,
        bound,
        approval,
        decision_sha256="8" * 64,
    )
    _patch_authority(monkeypatch, authority)

    with pytest.raises(
        sut.SmarketsExecutionApprovalError,
        match="does not bind exact execution plan bytes",
    ):
        sut.consume_smarkets_execution_approval(
            bound,
            approval,
            action_id="a" * 64,
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
        )

    audit = authority.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        require_unconsumed=False,
    )
    assert audit.receipt.consumed_at is None
    assert audit.receipt.consumed_by is None


def test_wrong_durable_approval_evidence_fails_before_receipt_consumption(
    tmp_path, monkeypatch
):
    authority = SupervisedConfirmationAuthority(
        tmp_path / "supervised-confirmation.jsonl",
        clock=FakeClock(),
    )
    approval = _approval()
    bound = _bound(approval)
    review, receipt = _confirmed(
        authority,
        bound,
        approval,
        approval_evidence_sha256="9" * 64,
    )
    _patch_authority(monkeypatch, authority)

    with pytest.raises(
        sut.SmarketsExecutionApprovalError,
        match="approval evidence does not match",
    ):
        sut.consume_smarkets_execution_approval(
            bound,
            approval,
            action_id="a" * 64,
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
        )

    assert authority.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        require_unconsumed=False,
    ).receipt.consumed_at is None


def test_confirmation_lifetime_cannot_outlive_underlying_supervised_approval(
    tmp_path, monkeypatch
):
    authority = SupervisedConfirmationAuthority(
        tmp_path / "supervised-confirmation.jsonl",
        clock=FakeClock(),
    )
    approval = _approval(expires_at="2026-09-22T12:11:00+00:00")
    bound = _bound(approval)
    review, receipt = _confirmed(authority, bound, approval, ttl_seconds=120)
    _patch_authority(monkeypatch, authority)

    with pytest.raises(
        sut.SmarketsExecutionApprovalError,
        match="lifetime exceeds underlying",
    ):
        sut.consume_smarkets_execution_approval(
            bound,
            approval,
            action_id="a" * 64,
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
        )


def test_same_account_multi_action_plan_is_ambiguous_and_fails_closed(
    tmp_path, monkeypatch
):
    authority = SupervisedConfirmationAuthority(
        tmp_path / "supervised-confirmation.jsonl",
        clock=FakeClock(),
    )
    approval = _approval()
    bound = _bound(approval, duplicate_same_account=True)
    review, receipt = _confirmed(authority, bound, approval)
    _patch_authority(monkeypatch, authority)

    with pytest.raises(
        sut.SmarketsExecutionApprovalError,
        match="does not identify one unique action",
    ):
        sut.consume_smarkets_execution_approval(
            bound,
            approval,
            action_id="a" * 64,
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
        )


def test_non_smarkets_action_is_rejected_before_confirmation_lookup(tmp_path, monkeypatch):
    authority = SupervisedConfirmationAuthority(
        tmp_path / "supervised-confirmation.jsonl",
        clock=FakeClock(),
    )
    approval = _approval()
    bound = _bound(approval, bookmaker_id="betfair")
    calls = 0

    def open_authority():
        nonlocal calls
        calls += 1
        return authority

    monkeypatch.setattr(sut, "_open_confirmation_authority", open_authority)
    with pytest.raises(
        sut.SmarketsExecutionApprovalError,
        match="bookmaker is not Smarkets",
    ):
        sut.consume_smarkets_execution_approval(
            bound,
            approval,
            action_id="a" * 64,
            receipt_id="f" * 64,
            expected_review_sha256="e" * 64,
        )
    assert calls == 0


def test_receipt_consumed_by_another_product_identity_cannot_be_rebound(
    tmp_path, monkeypatch
):
    authority = SupervisedConfirmationAuthority(
        tmp_path / "supervised-confirmation.jsonl",
        clock=FakeClock(),
    )
    approval = _approval()
    bound = _bound(approval)
    review, receipt = _confirmed(authority, bound, approval)
    authority.consume_receipt(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        consumer_key="different-product-consumer",
    )
    _patch_authority(monkeypatch, authority)

    with pytest.raises(
        sut.SmarketsExecutionApprovalError,
        match="consumed by another execution identity",
    ):
        sut.resolve_consumed_smarkets_execution_approval(
            bound,
            approval,
            action_id="a" * 64,
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
        )


def test_caller_cannot_construct_copy_or_pickle_approval_witness(tmp_path, monkeypatch):
    authority = SupervisedConfirmationAuthority(
        tmp_path / "supervised-confirmation.jsonl",
        clock=FakeClock(),
    )
    approval = _approval()
    bound = _bound(approval)
    review, receipt = _confirmed(authority, bound, approval)
    _patch_authority(monkeypatch, authority)
    witness = sut.consume_smarkets_execution_approval(
        bound,
        approval,
        action_id="a" * 64,
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
    )

    with pytest.raises(TypeError, match="issued only by product approval authority"):
        replace(witness, action_id="b" * 64)
    with pytest.raises(TypeError, match="non-serializable"):
        pickle.dumps(witness)


def test_canonical_confirmation_path_is_product_workspace_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSPORT_WORKSPACE", str(tmp_path.resolve()))
    assert sut.canonical_supervised_confirmation_path() == (
        tmp_path.resolve() / "supervised-confirmation.jsonl"
    )
