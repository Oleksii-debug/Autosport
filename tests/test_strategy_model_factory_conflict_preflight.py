import pytest

from autosport.scientific_registry import ScientificRegistry, StrategyVersion
from autosport.strategy_model_factory import ExperimentRunner
from test_strategy_model_factory import (
    SHA_A,
    SHA_B,
    SHA_D,
    _candidate_points,
    _candidate_spec,
    _factory_foundation,
)


def test_factory_preflights_strategy_identity_conflict_before_candidate_mutation(tmp_path):
    registry, registry_path, rule, store, _, _ = _factory_foundation(tmp_path)
    spec = _candidate_spec()

    registry.append(
        StrategyVersion(
            spec.strategy_version_id,
            spec.canonical_strategy_id,
            SHA_A,
            SHA_D,
            SHA_B,
            spec.created_at,
            model_version_id=spec.model_version_id,
            predecessor_strategy_version_id=spec.predecessor_strategy_version_id,
        )
    )
    registry_before = registry_path.read_bytes()

    with pytest.raises(ValueError, match=r"StrategyVersion:strategy-v2"):
        ExperimentRunner(registry, store).run_baseline_candidate(
            spec,
            _candidate_points(),
            rule=rule,
        )

    assert registry_path.read_bytes() == registry_before
    restarted = ScientificRegistry(registry_path)
    assert restarted.get("StrategyVersion", spec.strategy_version_id) is not None
    assert restarted.get("ModelVersion", spec.model_version_id) is None
    assert restarted.get("EvaluationBundle", spec.evaluation_bundle_id) is None
    assert restarted.get("Experiment", spec.experiment_id) is None
    assert restarted.get("PromotionDecision", spec.promotion_decision_id) is None
    assert not store.path_for_testing("model", spec.model_version_id).exists()
    assert not store.path_for_testing("metrics", spec.evaluation_bundle_id).exists()
    assert not store.path_for_testing("evaluation", spec.evaluation_bundle_id).exists()
