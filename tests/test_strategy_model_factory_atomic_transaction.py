from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

from autosport.scientific_registry import ScientificRegistry
from autosport.strategy_model_factory import (
    ExperimentRunner,
    FactoryArtifactStore,
    FactoryCandidateSpec,
)
from autosport.workspace_lock import WorkspaceEconomicLockBusyError
from test_strategy_model_factory import (
    _candidate_points,
    _candidate_spec,
    _factory_foundation,
)


def test_factory_public_type_module_identity_is_stable():
    assert FactoryCandidateSpec.__module__ == "autosport.strategy_model_factory"


def test_concurrent_factory_writers_leave_no_loser_residue(tmp_path):
    _, registry_path, rule, store, _, _ = _factory_foundation(tmp_path)
    base = _candidate_spec()
    first = replace(
        base,
        experiment_id="experiment-race-a",
        model_version_id="model-race-a",
        strategy_version_id="strategy-race",
        evaluation_bundle_id="eval-race-a",
        promotion_decision_id="promotion-race-a",
        seed=201,
    )
    second = replace(
        base,
        experiment_id="experiment-race-b",
        model_version_id="model-race-b",
        strategy_version_id="strategy-race",
        evaluation_bundle_id="eval-race-b",
        promotion_decision_id="promotion-race-b",
        seed=202,
    )
    start = Barrier(2)

    def run(spec):
        runner = ExperimentRunner(
            ScientificRegistry(registry_path),
            FactoryArtifactStore(store.root),
        )
        start.wait()
        try:
            result = runner.run_baseline_candidate(
                spec,
                _candidate_points(),
                rule=rule,
            )
        except (ValueError, WorkspaceEconomicLockBusyError) as exc:
            return "error", spec, exc
        return "ok", spec, result

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, (first, second)))

    successes = [item for item in outcomes if item[0] == "ok"]
    failures = [item for item in outcomes if item[0] == "error"]
    assert len(successes) == 1
    assert len(failures) == 1

    winner = successes[0][1]
    loser = failures[0][1]
    restarted = ScientificRegistry(registry_path)

    assert restarted.get("StrategyVersion", "strategy-race") is not None
    assert restarted.get("ModelVersion", winner.model_version_id) is not None
    assert restarted.get("EvaluationBundle", winner.evaluation_bundle_id) is not None
    assert restarted.get("Experiment", winner.experiment_id) is not None
    assert restarted.get("PromotionDecision", winner.promotion_decision_id) is not None

    assert restarted.get("ModelVersion", loser.model_version_id) is None
    assert restarted.get("EvaluationBundle", loser.evaluation_bundle_id) is None
    assert restarted.get("Experiment", loser.experiment_id) is None
    assert restarted.get("PromotionDecision", loser.promotion_decision_id) is None
    assert not store.path_for_testing("model", loser.model_version_id).exists()
    assert not store.path_for_testing("metrics", loser.evaluation_bundle_id).exists()
    assert not store.path_for_testing("evaluation", loser.evaluation_bundle_id).exists()
