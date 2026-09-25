from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
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


def _resolver_cycle(tmp_path: Path, authority) -> ProductPaperDecisionCycle:
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

    def _fake_provenance(value):
        return SimpleNamespace(
            contract_sha256="a" * 64 if value is goal else "f" * 64
        )

    monkeypatch.setattr(cycle_module, "ProductDecisionActivationStore", _ActivationStore)
    monkeypatch.setattr(cycle_module, "EconomicGoalStore", _GoalStore)
    monkeypatch.setattr(cycle_module, "PaperRiskPolicyStore", _RiskStore)
    monkeypatch.setattr(cycle_module, "EconomicDecisionAuthority", _Authority)
    monkeypatch.setattr(cycle_module, "provenance_for", _fake_provenance)

    resolver = cycle_module._make_product_authority_resolver(
        activation_store_class=_ActivationStore,
        economic_goal_store_class=_GoalStore,
        risk_policy_store_class=_RiskStore,
        authority_class=_Authority,
        provenance_resolver=_fake_provenance,
        intent_producer_class=cycle_module.BuiltInIntentProducer,
        path_class=Path,
        decimal_class=Decimal,
    )
    return goal, risk_policy, activation, calls, resolver


def test_resolver_reconstructs_fresh_authority_from_durable_start(
    tmp_path,
    monkeypatch,
) -> None:
    goal, risk_policy, _activation, calls, resolver = (
        _install_durable_authority_fakes(monkeypatch, tmp_path)
    )
    supplied = _Authority(goal, risk_policy)
    cycle = _resolver_cycle(tmp_path, supplied)

    resolved = resolver(cycle)

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
    goal, risk_policy, _activation, _calls, resolver = (
        _install_durable_authority_fakes(monkeypatch, tmp_path)
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
        resolver(cycle)

    assert supplied.contract is not goal
    assert supplied.risk_policy is not risk_policy


def test_production_resolver_rejects_module_store_substitution(
    tmp_path,
    monkeypatch,
) -> None:
    class _ForgedActivationStore:
        pass

    cycle = _resolver_cycle(
        tmp_path,
        SimpleNamespace(contract=SimpleNamespace(max_quote_age_seconds=Decimal("5"))),
    )
    monkeypatch.setattr(
        cycle_module,
        "ProductDecisionActivationStore",
        _ForgedActivationStore,
    )

    with pytest.raises(
        ProductPaperDecisionCycleError,
        match="canonical supported START decision-authority dispatch changed",
    ):
        cycle._resolve_product_authority()


def test_production_resolver_rejects_provenance_function_substitution(
    tmp_path,
    monkeypatch,
) -> None:
    cycle = _resolver_cycle(
        tmp_path,
        SimpleNamespace(contract=SimpleNamespace(max_quote_age_seconds=Decimal("5"))),
    )
    monkeypatch.setattr(
        cycle_module,
        "provenance_for",
        lambda _goal: SimpleNamespace(contract_sha256="0" * 64),
    )

    with pytest.raises(
        ProductPaperDecisionCycleError,
        match="canonical supported START decision-authority dispatch changed",
    ):
        cycle._resolve_product_authority()
