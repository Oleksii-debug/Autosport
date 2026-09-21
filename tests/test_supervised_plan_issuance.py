from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autosport.betfair_standard_limit_price_bound import (
    resolve_betfair_standard_limit_price_bound,
)
from autosport.betfair_standard_limit_price_bound_product_verifier import (
    verify_product_betfair_standard_limit_price_bound,
)
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
    authority_root = tmp_path / "machine-authority"
    workspace.mkdir()
    return SupervisedPlanIssuanceStore(
        workspace,
        authority_root=authority_root,
    )


def _issue(monkeypatch, tmp_path: Path):
    bound, approval, _goal = _bound(_profile())
    store = _store(monkeypatch, tmp_path, bound)
    issued = store.issue(bound=bound, approval=approval)
    return bound, approval, store, issued


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


def test_product_verifier_reloads_issuance_instead_of_accepting_caller_bound(
    monkeypatch, tmp_path: Path
) -> None:
    bound, approval, store, _issued = _issue(monkeypatch, tmp_path)
    action = bound.execution_plan.actions[0]
    ledger = RealExecutionLedger(tmp_path / "execution-ledger.jsonl")
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

    verified = verify_product_betfair_standard_limit_price_bound(
        evidence=evidence,
        ledger=ledger,
        issuance_store=store,
        execution_plan_id=bound.execution_plan.plan_id,
        action_id=action.action_id,
    )

    assert verified is not evidence
    assert verified.evidence_id == evidence.evidence_id


def test_product_verifier_rejects_unissued_plan_identity(
    monkeypatch, tmp_path: Path
) -> None:
    bound, approval, store, _issued = _issue(monkeypatch, tmp_path)
    action = bound.execution_plan.actions[0]
    ledger = RealExecutionLedger(tmp_path / "execution-ledger.jsonl")
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

    with pytest.raises(SupervisedPlanIssuanceError):
        verify_product_betfair_standard_limit_price_bound(
            evidence=evidence,
            ledger=ledger,
            issuance_store=store,
            execution_plan_id="supervised-v2-" + "0" * 64,
            action_id=action.action_id,
        )
