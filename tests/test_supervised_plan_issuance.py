from __future__ import annotations

from contextlib import contextmanager
from dataclasses import fields, replace
import json
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import pytest

import autosport.trusted_runtime_code_profile as trusted_runtime_profile
from autosport.betfair_standard_limit_price_bound import (
    BetfairStandardLimitPriceBoundError,
    BetfairStandardLimitPriceBoundEvidence,
    BetfairStandardLimitPriceBoundStatus,
    resolve_betfair_standard_limit_price_bound,
)
from autosport.betfair_standard_limit_price_bound_product_verifier import (
    verify_product_betfair_standard_limit_price_bound,
)
from autosport.continuous_session import SessionState
from autosport.product_runtime import AutonomousProductRuntime
from autosport.real_execution_ledger import RealExecutionLedger
from autosport.supervised_plan_issuance import (
    SupervisedPlanIssuanceError,
    SupervisedPlanIssuanceStore,
)

from test_betfair_supervised_execution import _bound, _profile


def _store(monkeypatch, tmp_path: Path, bound):
    import autosport.supervised_plan_issuance as module

    class GoalStore:
        def __init__(self, workspace):
            self.workspace = Path(workspace)

        def load(self):
            return object()

    monkeypatch.setattr(module, "EconomicGoalStore", GoalStore)
    monkeypatch.setattr(
        module,
        "provenance_for",
        lambda _goal: SimpleNamespace(
            contract_sha256=bound.economic_goal_contract_sha256
        ),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return SupervisedPlanIssuanceStore(workspace)


def _issue(monkeypatch, tmp_path: Path):
    bound, approval, _goal = _bound(_profile())
    store = _store(monkeypatch, tmp_path, bound)
    issued = store.issue(bound=bound, approval=approval)
    return bound, approval, store, issued


class _RuntimeLease:
    authority_active = True


class _StartTransitionStore:
    @staticmethod
    def pending() -> None:
        return None


class _RunningCoordinator:
    @staticmethod
    def status() -> SimpleNamespace:
        return SimpleNamespace(state=SessionState.RUNNING)


class _RunningCollector:
    @staticmethod
    def status() -> dict[str, object | None]:
        return {"stopped_at": None, "stop_reason": None}


@contextmanager
def _active_runtime_profile(workspace: Path):
    workspace = Path(workspace).resolve()
    runtime = object.__new__(AutonomousProductRuntime)
    runtime.workspace = workspace
    runtime.manifest = SimpleNamespace(source_id="parlayapi:table_tennis")
    runtime.coordinator = _RunningCoordinator()
    runtime.collector = _RunningCollector()
    runtime._runtime_lease = _RuntimeLease()
    runtime._start_transition_store = _StartTransitionStore()
    runtime._closed = False
    runtime._operation_fence = RLock()
    trusted_runtime_profile._register_started_product_runtime_origin(
        runtime,
        source_factory="autosport.product_source:create_parlay_product_source",
        expected_provider_source_id="parlayapi:table_tennis",
    )
    profile = trusted_runtime_profile.issue_trusted_runtime_code_profile(runtime)
    try:
        yield profile
    finally:
        trusted_runtime_profile.revoke_trusted_runtime_code_profile(profile)
        trusted_runtime_profile._clear_started_product_runtime_origin(runtime)


def _verify_product(
    *,
    evidence,
    ledger,
    issuance_store,
    execution_plan_id,
    action_id,
):
    with _active_runtime_profile(issuance_store.workspace) as runtime_profile:
        return verify_product_betfair_standard_limit_price_bound(
            evidence=evidence,
            ledger=ledger,
            issuance_store=issuance_store,
            runtime_profile=runtime_profile,
            execution_plan_id=execution_plan_id,
            action_id=action_id,
        )


def test_generic_issuance_survives_unavailable_betfair_price_bound_without_authority_upgrade(
    monkeypatch, tmp_path: Path
) -> None:
    import autosport.supervised_plan_issuance as module

    bound, approval, _goal = _bound(_profile())
    store = _store(monkeypatch, tmp_path, bound)

    def unproven_price_bound(*, bound, action_id):
        raise BetfairStandardLimitPriceBoundError(
            "provider-specific price-bound proof is unavailable"
        )

    monkeypatch.setattr(
        module,
        "resolve_betfair_standard_limit_price_bound",
        unproven_price_bound,
    )

    issued = store.issue(bound=bound, approval=approval)
    restarted = SupervisedPlanIssuanceStore(
        store.workspace,
        authority_root=store.authority_root,
    )
    loaded = restarted.load(bound.execution_plan.plan_id)

    assert issued.provider_requests == ()
    assert loaded.provider_requests == ()

    # Later software/provider knowledge may prove the Betfair request shape, but
    # an old generic issuance must not retroactively acquire positive #735
    # authority that was absent at issuance time.
    action = bound.execution_plan.actions[0]
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

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="not durably proven at plan issuance",
    ):
        _verify_product(
            evidence=evidence,
            ledger=ledger,
            issuance_store=restarted,
            execution_plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )


def test_non_betfair_action_does_not_invoke_betfair_price_bound_resolver(
    monkeypatch,
) -> None:
    import autosport.supervised_plan_issuance as module

    action = SimpleNamespace(
        action_id="future-provider-action",
        bookmaker_id="future-provider",
        account_id="acct-future",
    )
    bound = SimpleNamespace(
        execution_plan=SimpleNamespace(actions=(action,))
    )

    def forbidden_resolver(*, bound, action_id):
        raise AssertionError("Betfair resolver must not gate non-Betfair issuance")

    monkeypatch.setattr(
        module,
        "resolve_betfair_standard_limit_price_bound",
        forbidden_resolver,
    )

    assert module._provider_request_payloads(bound) == []


def test_product_issuance_survives_restart_with_exact_provider_request_identity(
    monkeypatch, tmp_path: Path
) -> None:
    bound, approval, store, issued = _issue(monkeypatch, tmp_path)

    restarted = SupervisedPlanIssuanceStore(
        store.workspace,
        authority_root=store.authority_root,
    )
    loaded = restarted.load(bound.execution_plan.plan_id)

    assert loaded.bound == bound
    assert loaded.approval == approval
    assert loaded.transaction_id == issued.transaction_id
    assert loaded.workspace_instance_id == issued.workspace_instance_id
    assert loaded.semantic_binding_sha256 == issued.semantic_binding_sha256
    assert len(loaded.provider_requests) == len(bound.execution_plan.actions)
    assert loaded.provider_requests[0]["action_id"] == bound.execution_plan.actions[0].action_id
    assert len(loaded.provider_requests[0]["instruction_sha256"]) == 64


def test_exact_replay_is_idempotent_and_does_not_mint_new_transaction(
    monkeypatch, tmp_path: Path
) -> None:
    bound, approval, store, issued = _issue(monkeypatch, tmp_path)

    replayed = store.issue(bound=bound, approval=approval)

    assert replayed.transaction_id == issued.transaction_id
    assert replayed.semantic_binding_sha256 == issued.semantic_binding_sha256


def test_tampered_issuance_bytes_fail_against_independent_machine_authority(
    monkeypatch, tmp_path: Path
) -> None:
    bound, _approval, store, _issued = _issue(monkeypatch, tmp_path)
    path = store._path(bound.execution_plan.plan_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["issuance"]["provider_requests"][0]["instruction_sha256"] = "0" * 64
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        SupervisedPlanIssuanceError,
        match="stale, tampered, rolled back, or unproven",
    ):
        store.load(bound.execution_plan.plan_id)


def test_deleted_issuance_fails_as_rollback_while_machine_authority_survives(
    monkeypatch, tmp_path: Path
) -> None:
    bound, _approval, store, _issued = _issue(monkeypatch, tmp_path)
    store._path(bound.execution_plan.plan_id).unlink()

    with pytest.raises(
        SupervisedPlanIssuanceError,
        match="deleted or rolled back",
    ):
        store.load(bound.execution_plan.plan_id)


def test_provider_request_projection_drift_after_issuance_fails_reload(
    monkeypatch, tmp_path: Path
) -> None:
    import autosport.supervised_plan_issuance as module

    bound, _approval, store, _issued = _issue(monkeypatch, tmp_path)
    original = module.resolve_betfair_standard_limit_price_bound

    def drifted(*, bound, action_id):
        evidence = original(bound=bound, action_id=action_id)
        forged = object.__new__(BetfairStandardLimitPriceBoundEvidence)
        for field in fields(BetfairStandardLimitPriceBoundEvidence):
            object.__setattr__(forged, field.name, getattr(evidence, field.name))
        object.__setattr__(forged, "instruction_sha256", "1" * 64)
        return forged

    monkeypatch.setattr(
        module,
        "resolve_betfair_standard_limit_price_bound",
        drifted,
    )

    with pytest.raises(
        SupervisedPlanIssuanceError,
        match="provider request identity no longer matches canonical adapter",
    ):
        store.load(bound.execution_plan.plan_id)


def test_product_verifier_reloads_issuance_instead_of_accepting_caller_bound(
    monkeypatch, tmp_path: Path
) -> None:
    bound, approval, store, _issued = _issue(monkeypatch, tmp_path)
    action = bound.execution_plan.actions[0]
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

    verified = _verify_product(
        evidence=evidence,
        ledger=ledger,
        issuance_store=store,
        execution_plan_id=bound.execution_plan.plan_id,
        action_id=action.action_id,
    )

    assert verified is not evidence
    assert verified.evidence_id == evidence.evidence_id
    assert (
        verified.status
        is BetfairStandardLimitPriceBoundStatus.PROVIDER_BOUND_ZERO_ADVERSE_PRICE_DETERIORATION
    )
    assert verified.matchme_applicability_proven is True
    assert verified.zero_adverse_price_deterioration is True
    assert verified.execution_feasibility_proven is False
    assert verified.realized_price_exact is False


def test_product_verifier_rejects_unissued_plan_identity(
    monkeypatch, tmp_path: Path
) -> None:
    bound, approval, store, _issued = _issue(monkeypatch, tmp_path)
    action = bound.execution_plan.actions[0]
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

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="durable product supervised-plan issuance is missing or invalid",
    ):
        _verify_product(
            evidence=evidence,
            ledger=ledger,
            issuance_store=store,
            execution_plan_id="supervised-v2-" + "0" * 64,
            action_id=action.action_id,
        )


def test_product_verifier_rejects_self_consistent_unrelated_workspace(
    monkeypatch, tmp_path: Path
) -> None:
    bound, approval, store, _issued = _issue(monkeypatch, tmp_path)
    action = bound.execution_plan.actions[0]
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
    product_workspace = tmp_path / "actual-product-workspace"
    product_workspace.mkdir()

    with _active_runtime_profile(product_workspace) as runtime_profile:
        with pytest.raises(
            BetfairStandardLimitPriceBoundError,
            match="issuance store is outside the active runtime workspace",
        ):
            verify_product_betfair_standard_limit_price_bound(
                evidence=evidence,
                ledger=ledger,
                issuance_store=store,
                runtime_profile=runtime_profile,
                execution_plan_id=bound.execution_plan.plan_id,
                action_id=action.action_id,
            )


def test_product_verifier_rejects_ledger_outside_active_workspace(
    monkeypatch, tmp_path: Path
) -> None:
    bound, approval, store, _issued = _issue(monkeypatch, tmp_path)
    action = bound.execution_plan.actions[0]
    outside = tmp_path / "other-root"
    ledger = RealExecutionLedger(outside / "execution-ledger.jsonl")
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

    with _active_runtime_profile(store.workspace) as runtime_profile:
        with pytest.raises(
            BetfairStandardLimitPriceBoundError,
            match="execution ledger is outside the active runtime workspace",
        ):
            verify_product_betfair_standard_limit_price_bound(
                evidence=evidence,
                ledger=ledger,
                issuance_store=store,
                runtime_profile=runtime_profile,
                execution_plan_id=bound.execution_plan.plan_id,
                action_id=action.action_id,
            )


def test_product_verifier_rejects_copied_runtime_profile(
    monkeypatch, tmp_path: Path
) -> None:
    bound, approval, store, _issued = _issue(monkeypatch, tmp_path)
    action = bound.execution_plan.actions[0]
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

    with _active_runtime_profile(store.workspace) as runtime_profile:
        copied = replace(runtime_profile)
        with pytest.raises(
            BetfairStandardLimitPriceBoundError,
            match="canonical product runtime authority is missing or changed",
        ):
            verify_product_betfair_standard_limit_price_bound(
                evidence=evidence,
                ledger=ledger,
                issuance_store=store,
                runtime_profile=copied,
                execution_plan_id=bound.execution_plan.plan_id,
                action_id=action.action_id,
            )


def test_product_verifier_rechecks_runtime_profile_before_positive_return(
    monkeypatch, tmp_path: Path
) -> None:
    bound, approval, store, _issued = _issue(monkeypatch, tmp_path)
    action = bound.execution_plan.actions[0]
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

    with _active_runtime_profile(store.workspace) as runtime_profile:
        original_init = RealExecutionLedger.__init__

        def revoke_during_reopen(self, path):
            original_init(self, path)
            trusted_runtime_profile.revoke_trusted_runtime_code_profile(
                runtime_profile
            )

        monkeypatch.setattr(
            RealExecutionLedger,
            "__init__",
            revoke_during_reopen,
        )
        with pytest.raises(
            BetfairStandardLimitPriceBoundError,
            match="canonical product runtime authority is missing or changed",
        ):
            verify_product_betfair_standard_limit_price_bound(
                evidence=evidence,
                ledger=ledger,
                issuance_store=store,
                runtime_profile=runtime_profile,
                execution_plan_id=bound.execution_plan.plan_id,
                action_id=action.action_id,
            )


def test_product_verifier_does_not_inherit_caller_selected_issuance_authority_root(
    monkeypatch, tmp_path: Path
) -> None:
    bound, approval, _goal = _bound(_profile())
    default_handle = _store(monkeypatch, tmp_path, bound)
    caller_store = SupervisedPlanIssuanceStore(
        default_handle.workspace,
        authority_root=tmp_path / "caller-selected-machine-authority",
    )
    caller_store.issue(bound=bound, approval=approval)
    action = bound.execution_plan.actions[0]
    ledger = RealExecutionLedger(
        default_handle.workspace / "execution-ledger.jsonl"
    )
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

    with _active_runtime_profile(default_handle.workspace) as runtime_profile:
        with pytest.raises(
            BetfairStandardLimitPriceBoundError,
            match="durable product supervised-plan issuance is missing or invalid",
        ):
            verify_product_betfair_standard_limit_price_bound(
                evidence=evidence,
                ledger=ledger,
                issuance_store=caller_store,
                runtime_profile=runtime_profile,
                execution_plan_id=bound.execution_plan.plan_id,
                action_id=action.action_id,
            )
