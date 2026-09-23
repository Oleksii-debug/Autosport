from dataclasses import replace

import pytest

from autosport.scientific_registry import ScientificRegistry
from autosport.strategy_model_factory import ExperimentRunner
from test_strategy_model_factory import (
    T6,
    T7,
    _candidate_points,
    _candidate_spec,
    _factory_foundation,
)


@pytest.mark.parametrize(
    ("decided_at", "promotion_decision_id", "suffix"),
    (
        (T6, "promotion-v3-backdated", "backdated"),
        (T7, "promotion-a", "same-timestamp-order"),
    ),
)
def test_factory_preflights_promotion_history_order_before_candidate_mutation(
    tmp_path,
    decided_at,
    promotion_decision_id,
    suffix,
):
    registry, registry_path, rule, store, _, _ = _factory_foundation(tmp_path)
    runner = ExperimentRunner(registry, store)

    # Establish a later durable promotion-history entry in the same canonical strategy
    # context. Whether the frozen rule promotes or rejects it, the decision itself is
    # immutable ordering history that a retroactive decision may not be inserted before.
    runner.run_baseline_candidate(
        _candidate_spec(),
        _candidate_points(),
        rule=rule,
    )

    predecessor_strategy_version_id = registry.champion_strategy(
        as_of=decided_at,
        canonical_strategy_id="canonical-factory-strategy",
    )
    assert predecessor_strategy_version_id is not None
    predecessor = registry.get(
        "StrategyVersion", predecessor_strategy_version_id
    )
    assert predecessor is not None
    predecessor_model_version_id = predecessor.payload.get("model_version_id")
    assert isinstance(predecessor_model_version_id, str)

    spec = replace(
        _candidate_spec(),
        experiment_id=f"experiment-{suffix}",
        model_version_id=f"model-{suffix}",
        strategy_version_id=f"strategy-{suffix}",
        evaluation_bundle_id=f"eval-{suffix}",
        promotion_decision_id=promotion_decision_id,
        seed=101 if suffix == "backdated" else 102,
        completed_at=T6,
        decided_at=decided_at,
        predecessor_strategy_version_id=predecessor_strategy_version_id,
        predecessor_model_version_id=predecessor_model_version_id,
    )
    registry_before = registry_path.read_bytes()

    with pytest.raises(
        ValueError,
        match="promotion decision cannot be backdated before durable promotion history",
    ):
        runner.run_baseline_candidate(
            spec,
            _candidate_points(),
            rule=rule,
        )

    assert registry_path.read_bytes() == registry_before
    restarted = ScientificRegistry(registry_path)
    assert restarted.get("ModelVersion", spec.model_version_id) is None
    assert restarted.get("StrategyVersion", spec.strategy_version_id) is None
    assert restarted.get("EvaluationBundle", spec.evaluation_bundle_id) is None
    assert restarted.get("Experiment", spec.experiment_id) is None
    assert restarted.get("PromotionDecision", spec.promotion_decision_id) is None
    assert not store.path_for_testing("model", spec.model_version_id).exists()
    assert not store.path_for_testing("metrics", spec.evaluation_bundle_id).exists()
    assert not store.path_for_testing("evaluation", spec.evaluation_bundle_id).exists()
