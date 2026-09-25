from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import pytest

import autosport.product_paper_decision_cycle as cycle_module
from autosport.product_paper_decision_cycle import (
    ProductPaperDecisionCycle,
    ProductPaperDecisionCycleError,
)


class _Authority:
    def __init__(self, contract, risk_policy) -> None:
        self.contract = contract
        self.risk_policy = risk_policy

    def __eq__(self, other: object) -> bool:
        return (
            type(other) is _Authority
            and other.contract is self.contract
            and other.risk_policy is self.risk_policy
        )


class _IntentFactory:
    strategy_version_id = "strategy-v1"

    def __call__(self, *_args, **_kwargs):
        return ()


def _resolver_cycle(tmp_path: Path, authority: _Authority) -> ProductPaperDecisionCycle:
    cycle = object.__new__(ProductPaperDecisionCycle)
    cycle.runtime = SimpleNamespace(workspace=tmp_path)
    cycle.authority = authority
    cycle.intent_factory = _IntentFactory()
    cycle.scientific_registry = object()
    cycle.execution_config = object()
    cycle.max_quote_age = timedelta(seconds=4)
    return cycle


def _install_durable_authority_fakes(monkeypatch, tmp_path: Path):
    goal = SimpleNamespace(
        goal_id="goal-v1",
        revision=1,
        bankroll_id="bankroll-v1",
        currency="USD",
        max_quote_age_seconds=Decimal("5"),
    )
    risk_policy = object()
    activation = SimpleNamespace(
        economic_goal_contract_sha256="a" * 64,
        goal_id=goal.goal_id,
        goal_revision=goal.revision,
        bankroll_id=goal.bankroll_id,
        currency=goal.currency,
        risk_policy_provenance_sha256="b" * 64,
    )
    calls: dict[str, object] = {}

    class _ActivationStore:
        def __init__(self, workspace) -> None:
            assert Path(workspace) == tmp_path

        def load(self):
            calls["activation_load"] = True
            return activation

        def verify(self, **kwargs):
            calls["activation_verify"] = kwargs
            return activation

    class _GoalStore:
        def __init__(self, workspace) -> None:
            assert Path(workspace) == tmp_path

        def load(self):
            calls["goal_load"] = True
            return goal

    class _RiskStore:
        def __init__(self, workspace) -> None:
            assert Path(workspace) == tmp_path

        def load(self, **kwargs):
            calls["risk_load"] = kwargs
            return risk_policy

    monkeypatch.setattr(cycle_module, "ProductDecisionActivationStore", _ActivationStore)
    monkeypatch.setattr(cycle_module, "EconomicGoalStore", _GoalStore)
    monkeypatch.setattr(cycle_module, "PaperRiskPolicyStore", _RiskStore)
    monkeypatch.setattr(cycle_module, "EconomicDecisionAuthority", _Authority)
    monkeypatch.setattr(
        cycle_module,
        "provenance_for",
        lambda value: SimpleNamespace(
            contract_sha256="a" * 64 if value is goal else "f" * 64
        ),
    )
    return goal, risk_policy, activation, calls


def test_resolver_reconstructs_fresh_authority_from_durable_start(
    tmp_path,
    monkeypatch,
) -> None:
    goal, risk_policy, _activation, calls = _install_durable_authority_fakes(
        monkeypatch, tmp_path
    )
    supplied = _Authority(goal, risk_policy)
    cycle = _resolver_cycle(tmp_path, supplied)

    resolved = cycle._resolve_product_authority()

    assert resolved == supplied
    assert resolved is not supplied
    assert calls["activation_load"] is True
    assert calls["goal_load"] is True
    assert calls["risk_load"] == {
        "economic_goal": goal,
        "expected_policy_provenance_sha256": "b" * 64,
    }
    verify = calls["activation_verify"]
    assert verify["scientific_registry"] is cycle.scientific_registry
    assert verify["strategy_version_id"] == "strategy-v1"
    assert verify["economic_goal"] is goal
    assert verify["risk_policy"] is risk_policy
    assert verify["execution_config"] is cycle.execution_config
    assert verify["intent_producer"] is cycle_module.BuiltInIntentProducer.REGISTERED_STRATEGY


def test_resolver_rejects_caller_authority_that_differs_from_durable_start(
    tmp_path,
    monkeypatch,
) -> None:
    goal, risk_policy, _activation, _calls = _install_durable_authority_fakes(
        monkeypatch, tmp_path
    )
    supplied = _Authority(
        SimpleNamespace(max_quote_age_seconds=Decimal("5")),
        object(),
    )
    cycle = _resolver_cycle(tmp_path, supplied)

    with pytest.raises(
        ProductPaperDecisionCycleError,
        match="caller-supplied decision authority does not match durable supported START authority",
    ):
        cycle._resolve_product_authority()

    assert supplied.contract is not goal
    assert supplied.risk_policy is not risk_policy


def test_supported_tick_passes_only_reresolved_authority_to_decision_cycle() -> None:
    cycle = object.__new__(ProductPaperDecisionCycle)
    cycle._cycle_lock = Lock()
    product_tick = SimpleNamespace()
    status = SimpleNamespace(state=cycle_module.SessionState.RUNNING)
    cycle.runtime = SimpleNamespace(
        tick=lambda: product_tick,
        status=lambda: status,
    )
    durable_authority = object()
    decision = SimpleNamespace(status="NO_CHANGE")
    captured: dict[str, object] = {}

    cycle._require_running_runtime = lambda: None
    cycle._resolve_product_authority = lambda: durable_authority

    def _run_decision_cycle(*, authority=None):
        captured["authority"] = authority
        return decision

    cycle._run_decision_cycle = _run_decision_cycle

    result = cycle.tick()

    assert captured["authority"] is durable_authority
    assert result.product_tick is product_tick
    assert result.decision is decision
    assert result.skipped_reason is None
