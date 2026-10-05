from __future__ import annotations

from dataclasses import fields
from decimal import Decimal
from pathlib import Path
import tempfile

import pytest

import autosport.betfair_standard_limit_price_bound_verifier as verifier_module
import autosport.real_execution_ledger as ledger_module
import autosport.supervised_plan_issuance as issuance_module
import autosport.monotonic_workspace_authority as monotonic_module
import autosport.json_integrity as json_integrity_module
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

def test_rebound_ledger_event_reader_is_rejected_before_execution(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    attacker_called = False

    def attacker_events(_self):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound ledger event reader must never execute")

    monkeypatch.setattr(RealExecutionLedger, "_events", attacker_events)

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


def test_in_place_ledger_event_reader_mutation_is_rejected_before_execution() -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    reader = RealExecutionLedger._events
    original_code = reader.__code__
    attacker_called = False

    def attacker_events(_self):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("mutated ledger event reader must never execute")

    try:
        reader.__code__ = attacker_events.__code__
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
        reader.__code__ = original_code

    assert attacker_called is False


def test_instance_shadowed_ledger_event_reader_is_rejected_before_execution() -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    attacker_called = False

    def attacker_events():
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("instance-shadowed ledger event reader must never execute")

    ledger._events = attacker_events

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="ledger authority method shadow is not allowed",
    ):
        _verify(
            evidence=evidence,
            ledger=ledger,
            store=store,
            bound=bound,
            action_id=action.action_id,
        )

    assert attacker_called is False

def test_rebound_ledger_parser_is_rejected_before_execution(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    attacker_called = False

    def attacker_parse(_cls, _raw):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound ledger parser must never execute")

    monkeypatch.setattr(RealExecutionLedger, "_parse", classmethod(attacker_parse))

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


def test_in_place_ledger_parser_code_mutation_is_rejected_before_execution() -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    parser = RealExecutionLedger._parse.__func__
    original_code = parser.__code__
    attacker_called = False

    def attacker_parse(_cls, _raw):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("mutated ledger parser must never execute")

    try:
        parser.__code__ = attacker_parse.__code__
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
        parser.__code__ = original_code

    assert attacker_called is False


def test_instance_shadowed_ledger_parser_is_rejected_before_execution() -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    attacker_called = False

    def attacker_parse(_raw):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("instance-shadowed ledger parser must never execute")

    ledger._parse = attacker_parse

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="ledger authority method shadow is not allowed",
    ):
        _verify(
            evidence=evidence,
            ledger=ledger,
            store=store,
            bound=bound,
            action_id=action.action_id,
        )

    assert attacker_called is False

def test_rebound_ledger_json_loader_is_rejected_before_execution(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    attacker_called = False

    def attacker_loads(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound ledger JSON loader must never execute")

    monkeypatch.setattr(ledger_module.json, "loads", attacker_loads)

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


def test_in_place_ledger_json_loader_mutation_is_rejected_before_execution() -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    loader = ledger_module.json.loads
    original_code = loader.__code__
    attacker_called = False

    def attacker_loads(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("mutated ledger JSON loader must never execute")

    try:
        loader.__code__ = attacker_loads.__code__
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
        loader.__code__ = original_code

    assert attacker_called is False


def test_rebound_ledger_digest_is_rejected_before_execution(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    attacker_called = False

    def attacker_digest(_value):
        nonlocal attacker_called
        attacker_called = True
        return "0" * 64

    monkeypatch.setattr(ledger_module, "_digest", attacker_digest)

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


def test_in_place_ledger_digest_mutation_is_rejected_before_execution() -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    digest = ledger_module._digest
    original_code = digest.__code__
    attacker_called = False

    def attacker_digest(_value):
        nonlocal attacker_called
        attacker_called = True
        return "0" * 64

    try:
        digest.__code__ = attacker_digest.__code__
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
        digest.__code__ = original_code

    assert attacker_called is False

def test_rebound_issuance_strict_json_decoder_is_rejected_before_execution(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    attacker_called = False

    def attacker_loads(_text):
        nonlocal attacker_called
        attacker_called = True
        return {}

    monkeypatch.setattr(issuance_module, "strict_json_loads", attacker_loads)

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


def test_in_place_issuance_digest_mutation_is_rejected_before_execution() -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    digest = issuance_module._digest
    original_code = digest.__code__
    attacker_called = False

    def attacker_digest(_payload):
        nonlocal attacker_called
        attacker_called = True
        return "0" * 64

    try:
        digest.__code__ = attacker_digest.__code__
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
        digest.__code__ = original_code

    assert attacker_called is False


def test_rebound_issuance_bound_decoder_is_rejected_before_execution(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    attacker_called = False

    def attacker_decode(_raw):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound issuance bound decoder must never execute")

    monkeypatch.setattr(issuance_module, "_decode_bound", attacker_decode)

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


def test_rebound_monotonic_recover_is_rejected_before_execution(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    attacker_called = False

    def attacker_recover(self, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound monotonic recover must never execute")

    monkeypatch.setattr(
        issuance_module.MonotonicWorkspaceAuthority,
        "recover",
        attacker_recover,
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


def test_in_place_monotonic_history_loader_mutation_is_rejected_before_execution() -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    loader = monotonic_module.MonotonicWorkspaceAuthority._load_history
    original_code = loader.__code__
    attacker_called = False

    def attacker_load_history(_self):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("mutated monotonic history loader must never execute")

    try:
        loader.__code__ = attacker_load_history.__code__
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
        loader.__code__ = original_code

    assert attacker_called is False


def test_rebound_monotonic_record_hash_is_rejected_before_execution(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    attacker_called = False

    def attacker_record_hash(_payload):
        nonlocal attacker_called
        attacker_called = True
        return "0" * 64

    monkeypatch.setattr(monotonic_module, "_record_hash", attacker_record_hash)

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

def test_rebound_issuance_provider_resolver_is_rejected_before_execution(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    attacker_called = False

    def attacker_resolver(**_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound issuance provider resolver must never execute")

    monkeypatch.setattr(
        issuance_module,
        "resolve_betfair_standard_limit_price_bound",
        attacker_resolver,
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


def test_rebound_issuance_authority_domain_is_rejected_before_execution(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()

    monkeypatch.setattr(
        issuance_module,
        "_AUTHORITY_DOMAIN",
        "attacker.supervised-plan-issuance",
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


def test_rebound_issuance_execution_plan_type_is_rejected_before_decode(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()

    monkeypatch.setattr(issuance_module, "ExecutionPlan", object)

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


def test_rebound_issuance_schema_keys_are_rejected_before_decode(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()

    monkeypatch.setattr(
        issuance_module,
        "_PROVIDER_REQUEST_KEYS",
        frozenset({"action_id"}),
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

def test_rebound_strict_json_loader_is_rejected_before_durable_decode(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    attacker_called = False

    def attacker_loads(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        return {}

    monkeypatch.setattr(json_integrity_module.json, "loads", attacker_loads)

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


def test_in_place_strict_json_validator_mutation_is_rejected_before_decode() -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    validator = json_integrity_module._validate_strict_json_value
    original_code = validator.__code__
    attacker_called = False

    def attacker_validator(_value):
        nonlocal attacker_called
        attacker_called = True
        return None

    try:
        validator.__code__ = attacker_validator.__code__
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
        validator.__code__ = original_code

    assert attacker_called is False


def test_rebound_strict_json_integer_limit_is_rejected_before_decode(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()

    monkeypatch.setattr(json_integrity_module, "_JSON_INTEGER_MAX_DIGITS", 1)

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

def test_rebound_monotonic_root_resolver_is_rejected_before_execution(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    attacker_called = False

    def attacker_root(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        return store.workspace / "attacker-authority"

    monkeypatch.setattr(monotonic_module, "resolve_monotonic_authority_root", attacker_root)

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


def test_rebound_workspace_binding_resolver_is_rejected_before_execution(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    attacker_called = False

    def attacker_resolve(cls, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("rebound workspace binding resolver must never execute")

    monkeypatch.setattr(
        monotonic_module.WorkspaceIdentityBinding,
        "resolve",
        classmethod(attacker_resolve),
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


def test_in_place_root_selection_resolver_mutation_is_rejected_before_execution() -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    resolver = monotonic_module.AuthorityRootSelectionBinding.__dict__["resolve"].__func__
    original_code = resolver.__code__
    attacker_called = False

    def attacker_resolve(cls, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("mutated root-selection resolver must never execute")

    try:
        resolver.__code__ = attacker_resolve.__code__
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
        resolver.__code__ = original_code

    assert attacker_called is False


def test_rebound_monotonic_authority_id_is_rejected_before_execution(monkeypatch) -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()

    monkeypatch.setattr(monotonic_module, "AUTHORITY_ID", "attacker.machine.authority")

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

def test_in_place_issued_plan_constructor_mutation_is_rejected_before_execution() -> None:
    bound, action, evidence, ledger, store = _product_issued_evidence()
    constructor = issuance_module.IssuedSupervisedPlan.__init__
    original_code = constructor.__code__
    attacker_called = False

    def attacker_init(self, *args, **kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("mutated issued-plan constructor must never execute")

    try:
        constructor.__code__ = attacker_init.__code__
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
        constructor.__code__ = original_code

    assert attacker_called is False

