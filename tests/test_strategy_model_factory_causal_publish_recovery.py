from pathlib import Path

import pytest

import autosport.strategy_model_factory as factory_module
from autosport.scientific_registry import ScientificRegistry
from autosport.strategy_model_factory import (
    ExperimentRunner,
    FactoryArtifactStore,
    MeanBaselineModel,
    training_points_manifest_sha256,
)
from test_strategy_model_factory import (
    T0,
    T1,
    T2,
    _candidate_points,
    _candidate_spec,
    _factory_foundation,
)


class _RecordingMeanFactory:
    model_family = "mean-baseline-v1"

    def __init__(self, final_model_id: str) -> None:
        self.final_model_id = final_model_id
        self.final_points = None

    def fit(self, model_id, points, *, training_cutoff):
        if model_id == self.final_model_id:
            self.final_points = tuple(points)
        return MeanBaselineModel.fit(
            model_id,
            points,
            training_cutoff=training_cutoff,
        )


def test_final_model_adapter_receives_only_frozen_causal_training_population(tmp_path):
    registry, _, rule, store, _, _ = _factory_foundation(tmp_path)
    spec = _candidate_spec()
    points = _candidate_points()
    model_factory = _RecordingMeanFactory(spec.model_version_id)

    ExperimentRunner(
        registry,
        store,
        baseline_model_factory=model_factory,
    ).run_baseline_candidate(spec, points, rule=rule)

    assert model_factory.final_points is not None
    assert tuple(point.observed_at for point in model_factory.final_points) == (T0, T1)
    assert all(point.target_reveal_at <= T2 for point in model_factory.final_points)
    model_payload = store.read("model", spec.model_version_id)
    assert model_payload["training_count"] == 2
    assert model_payload["training_points_manifest_sha256"] == training_points_manifest_sha256(
        points
    )


def test_interrupted_artifact_publication_is_recovered_before_identical_retry(
    tmp_path,
    monkeypatch,
):
    _, registry_path, rule, store, _, _ = _factory_foundation(tmp_path)
    spec = _candidate_spec()
    points = _candidate_points()
    real_atomic_write_json = factory_module.atomic_write_json
    crashed = False

    def crash_before_registry_publish(path, payload):
        nonlocal crashed
        if Path(path) == registry_path and not crashed:
            crashed = True
            raise SystemExit("simulated abrupt process loss before registry publication")
        return real_atomic_write_json(path, payload)

    monkeypatch.setattr(factory_module, "atomic_write_json", crash_before_registry_publish)

    with pytest.raises(SystemExit, match="simulated abrupt process loss"):
        ExperimentRunner(
            ScientificRegistry(registry_path),
            FactoryArtifactStore(store.root),
        ).run_baseline_candidate(spec, points, rule=rule)

    restarted_before_recovery = ScientificRegistry(registry_path)
    assert restarted_before_recovery.get("ModelVersion", spec.model_version_id) is None
    assert restarted_before_recovery.get("Experiment", spec.experiment_id) is None
    assert store.path_for_testing("model", spec.model_version_id).exists()
    assert store.path_for_testing("metrics", spec.evaluation_bundle_id).exists()
    assert store.path_for_testing("evaluation", spec.evaluation_bundle_id).exists()
    transaction_path = registry_path.parent / ".factory-publish-transaction-v1.json"
    assert transaction_path.exists()

    monkeypatch.setattr(factory_module, "atomic_write_json", real_atomic_write_json)
    result = ExperimentRunner(
        ScientificRegistry(registry_path),
        FactoryArtifactStore(store.root),
    ).run_baseline_candidate(spec, points, rule=rule)

    restarted = ScientificRegistry(registry_path)
    assert result.model_version_id == spec.model_version_id
    assert restarted.get("ModelVersion", spec.model_version_id) is not None
    assert restarted.get("StrategyVersion", spec.strategy_version_id) is not None
    assert restarted.get("EvaluationBundle", spec.evaluation_bundle_id) is not None
    assert restarted.get("Experiment", spec.experiment_id) is not None
    assert restarted.get("PromotionDecision", spec.promotion_decision_id) is not None
    assert not transaction_path.exists()
