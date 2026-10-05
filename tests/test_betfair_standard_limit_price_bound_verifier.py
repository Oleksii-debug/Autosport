from __future__ import annotations

from dataclasses import fields
from decimal import Decimal
from pathlib import Path
import tempfile

import pytest

import autosport.betfair_standard_limit_price_bound_verifier as verifier_module
from autosport.betfair_standard_limit_price_bound import (
    BetfairStandardLimitPriceBoundError,
    BetfairStandardLimitPriceBoundEvidence,
    resolve_betfair_standard_limit_price_bound,
)
from autosport.betfair_standard_limit_price_bound_verifier import (
    verify_betfair_standard_limit_price_bound,
)
from autosport.economic_goal_store import EconomicGoalStore
from autosport.real_execution_ledger import RealExecutionLedger
from autosport.supervised_plan_issuance import SupervisedPlanIssuanceStore

from test_betfair_supervised_execution import _bound, _profile


def _forge_clone(
    evidence: BetfairStandardLimitPriceBoundEvidence,
) -> BetfairStandardLimitPriceBoundEvidence:
    forged = object.__new__(BetfairStandardLimitPriceBoundEvidence)
    for field in fields(BetfairStandardLimitPriceBoundEvidence):
        object.__setattr__(forged, field.name, getattr(evidence, field.name))
    return forged


def _issuance_store(bound, goal) -> SupervisedPlanIssuanceStore:
    root = Path(tempfile.mkdtemp())
    workspace = root / "workspace"
    authority_root = root / "machine-authority"
    workspace.mkdir()
    EconomicGoalStore(workspace).initialize_owner(goal)
    return SupervisedPlanIssuanceStore(
        workspace,
        authority_root=authority_root,
    )


def _product_issued_evidence():
    bound, approval, goal = _bound(_profile())
    action = bound.execution_plan.actions[0]
    store = _issuance_store(bound, goal)
    store.issue(bound=bound, approval=approval)
    ledger = RealExecutionLedger(store.workspace / "execution-ledger.jsonl")
    ledger.reserve_plan(bound.execution_plan)
    ledger.bind_supervised_approval(
        plan_id=bound.execution_plan.plan_id,
        approval_id=approval.ledger_identity,
        approval_fingerprint=approval.fingerprint,
        approved_at=approval.approved_at,
        evidence_sha256=approval.evidence_sha256,
    )
    evidence = resolve_betfair_standard_limit_price_bound(
        bound=bound,
        action_id=action.action_id,
    )
    return bound, action, evidence, ledger, store


def _verify(*, evidence, ledger, store, bound, action_id):
    return verify_betfair_standard_limit_price_bound(
        evidence=evidence,
        ledger=ledger,
        issuance_store=store,
        execution_plan_id=bound.execution_plan.plan_id,
        action_id=action_id,
    )


def test_object_new_forge_with_changed_price_is_not_accepted() -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    forged = _forge_clone(evidence)
    object.__setattr__(forged, "price_floor_odds", Decimal("1.99"))

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="does not match fresh canonical re-resolution",
    ):
        _verify(
            evidence=forged,
            ledger=ledger,
            store=store,
            bound=bound,
            action_id=action.action_id,
        )


def test_object_new_forge_with_changed_plan_identity_is_not_accepted() -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    forged = _forge_clone(evidence)
    object.__setattr__(forged, "execution_plan_id", "attacker-plan")

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="does not match fresh canonical re-resolution",
    ):
        _verify(
            evidence=forged,
            ledger=ledger,
            store=store,
            bound=bound,
            action_id=action.action_id,
        )


def test_post_issuance_object_setattr_tamper_is_not_accepted() -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    object.__setattr__(evidence, "requested_stake", Decimal("999.00"))

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="does not match fresh canonical re-resolution",
    ):
        _verify(
            evidence=evidence,
            ledger=ledger,
            store=store,
            bound=bound,
            action_id=action.action_id,
        )


def test_exact_forged_clone_is_replaced_by_fresh_canonical_record() -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    forged = _forge_clone(evidence)

    verified = _verify(
        evidence=forged,
        ledger=ledger,
        store=store,
        bound=bound,
        action_id=action.action_id,
    )

    assert verified is not forged
    assert verified.action_id == action.action_id
    assert verified.price_floor_odds == action.requested_odds
    assert verified.requested_stake == action.requested_stake
    assert verified.execution_feasibility_proven is False
    assert verified.realized_price_exact is False


def test_self_consistent_synthetic_bound_without_product_issuance_is_rejected() -> None:
    bound, _approval, goal = _bound(_profile(), selection_id="43")
    action = bound.execution_plan.actions[0]
    raw_evidence = resolve_betfair_standard_limit_price_bound(
        bound=bound,
        action_id=action.action_id,
    )
    store = _issuance_store(bound, goal)
    empty_ledger = RealExecutionLedger(store.workspace / "execution-ledger.jsonl")

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="durable product supervised-plan issuance is missing or invalid",
    ):
        _verify(
            evidence=raw_evidence,
            ledger=empty_ledger,
            store=store,
            bound=bound,
            action_id=action.action_id,
        )


def test_reserved_plan_without_durable_supervised_approval_is_rejected() -> None:
    bound, approval, goal = _bound(_profile())
    action = bound.execution_plan.actions[0]
    store = _issuance_store(bound, goal)
    store.issue(bound=bound, approval=approval)
    ledger = RealExecutionLedger(store.workspace / "execution-ledger.jsonl")
    ledger.reserve_plan(bound.execution_plan)
    raw_evidence = resolve_betfair_standard_limit_price_bound(
        bound=bound,
        action_id=action.action_id,
    )

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="durable supervised approval is missing or revoked",
    ):
        _verify(
            evidence=raw_evidence,
            ledger=ledger,
            store=store,
            bound=bound,
            action_id=action.action_id,
        )



def test_global_snapshot_helper_rebinding_cannot_accept_forged_evidence(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    forged = _forge_clone(evidence)
    object.__setattr__(forged, "price_floor_odds", Decimal("1.99"))

    monkeypatch.setattr(
        verifier_module,
        "_exact_snapshot",
        lambda _evidence: (("attacker", object, "same"),),
    )

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="does not match fresh canonical re-resolution",
    ):
        _verify(
            evidence=forged,
            ledger=ledger,
            store=store,
            bound=bound,
            action_id=action.action_id,
        )


def test_global_continuity_helper_rebinding_cannot_bypass_approval_gate(monkeypatch) -> None:
    bound, approval, goal = _bound(_profile())
    action = bound.execution_plan.actions[0]
    store = _issuance_store(bound, goal)
    store.issue(bound=bound, approval=approval)
    ledger = RealExecutionLedger(store.workspace / "execution-ledger.jsonl")
    ledger.reserve_plan(bound.execution_plan)
    raw_evidence = resolve_betfair_standard_limit_price_bound(
        bound=bound,
        action_id=action.action_id,
    )

    monkeypatch.setattr(
        verifier_module,
        "_require_execution_state_continuity",
        lambda **_kwargs: None,
    )

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="durable supervised approval is missing or revoked",
    ):
        _verify(
            evidence=raw_evidence,
            ledger=ledger,
            store=store,
            bound=bound,
            action_id=action.action_id,
        )


def test_global_issuance_request_helper_rebinding_is_non_authoritative(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()

    def attacker_helper(**_kwargs):
        raise AssertionError("module-global issuance helper must never execute")

    monkeypatch.setattr(
        verifier_module,
        "_require_issuance_time_provider_request",
        attacker_helper,
    )

    verified = _verify(
        evidence=evidence,
        ledger=ledger,
        store=store,
        bound=bound,
        action_id=action.action_id,
    )

    assert verified is not evidence
    assert verified.action_id == action.action_id


def test_in_place_approval_method_mutation_is_rejected_before_execution() -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    method = RealExecutionLedger.supervised_approval_is_active
    original_code = method.__code__

    def attacker_method(*_args, **_kwargs):
        return True

    try:
        method.__code__ = attacker_method.__code__
        with pytest.raises(
            BetfairStandardLimitPriceBoundError,
            match="verifier dependency authority changed",
        ):
            _verify(
                evidence=evidence,
                ledger=ledger,
                store=store,
                bound=bound,
                action_id=action.action_id,
            )
    finally:
        method.__code__ = original_code

def test_rebound_issuance_load_locked_is_rejected_before_execution(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    attacker_called = False

    def attacker_load_locked(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound durable issuance loader must never execute")

    monkeypatch.setattr(
        SupervisedPlanIssuanceStore,
        "_load_locked",
        attacker_load_locked,
    )

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="verifier dependency authority changed",
    ):
        _verify(
            evidence=evidence,
            ledger=ledger,
            store=store,
            bound=bound,
            action_id=action.action_id,
        )

    assert attacker_called is False


def test_rebound_issuance_authority_factory_is_rejected_before_execution(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    attacker_called = False

    def attacker_authority(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound monotonic authority factory must never execute")

    monkeypatch.setattr(
        SupervisedPlanIssuanceStore,
        "_authority",
        attacker_authority,
    )

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="verifier dependency authority changed",
    ):
        _verify(
            evidence=evidence,
            ledger=ledger,
            store=store,
            bound=bound,
            action_id=action.action_id,
        )

    assert attacker_called is False

def test_instance_shadowed_issuance_loader_is_rejected_before_execution() -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    attacker_called = False

    def attacker_load_locked(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("instance-shadowed durable issuance loader must never execute")

    store._load_locked = attacker_load_locked

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="issuance_store method shadow is not allowed",
    ):
        _verify(
            evidence=evidence,
            ledger=ledger,
            store=store,
            bound=bound,
            action_id=action.action_id,
        )

    assert attacker_called is False


def test_instance_shadowed_issuance_authority_is_rejected_before_execution() -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    attacker_called = False

    def attacker_authority(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("instance-shadowed monotonic authority must never execute")

    store._authority = attacker_authority

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="issuance_store method shadow is not allowed",
    ):
        _verify(
            evidence=evidence,
            ledger=ledger,
            store=store,
            bound=bound,
            action_id=action.action_id,
        )

    assert attacker_called is False

