from decimal import Decimal

from autosport.experiential_learning import PolicyRetestSpec, run_policy_retest
from autosport.learning_environment import Action, EvidenceTruth, RewardEvidence, Transition
from autosport.scientific_registry import ScientificRegistry
from autosport.strategy_model_factory import ExperimentRunner, PromotionVerdict
from autosport.transparent_bandit_policy import BanditPolicyState
from test_strategy_model_factory import (
    SHA_B,
    SHA_C,
    SHA_D,
    T4,
    T6,
    T7,
    _bad_candidate_points,
    _factory_foundation,
)


def test_policy_successor_retest_persists_negative_scientific_memory(tmp_path):
    points = _bad_candidate_points()
    registry, registry_path, rule, store, _, _ = _factory_foundation(
        tmp_path, points=points
    )
    runner = ExperimentRunner(registry, store)

    predecessor = BanditPolicyState.initial(
        environment_id=SHA_D,
        protocol_id="protocol-factory",
        config_sha256=SHA_B,
        seed=11,
        action_types=frozenset({"WAIT"}),
    )
    action = Action(
        environment_id=SHA_D,
        observation_id="1" * 64,
        action_type="WAIT",
        decided_at=T4,
    )
    reward = RewardEvidence(
        environment_id=SHA_D,
        action_id=action.action_id,
        outcome_id="2" * 64,
        reward=Decimal("-0.25"),
        available_at=T4,
        truth=EvidenceTruth.OBSERVED,
    )
    transition = Transition(
        environment_id=SHA_D,
        episode_id="3" * 64,
        step_index=1,
        observation_id=action.observation_id,
        action_id=action.action_id,
        outcome_id=reward.outcome_id,
        reward_id=reward.reward_id,
        decision_at=T4,
        resolved_at=T4,
    )
    challenger, update_evidence = predecessor.update(
        action=action,
        reward=reward,
        transition=transition,
    )

    result = run_policy_retest(
        runner,
        predecessor_policy=predecessor,
        challenger_policy=challenger,
        update_evidence=update_evidence,
        spec=PolicyRetestSpec(
            experiment_id="experiment-v2",
            model_version_id="model-v2",
            evaluation_bundle_id="eval-v2",
            promotion_decision_id="promotion-v2",
            canonical_strategy_id="canonical-factory-strategy",
            dataset_snapshot_id="dataset-factory",
            feature_set_id="features-factory",
            source_sha256=SHA_C,
            evaluator_source_sha256=SHA_C,
            created_at=T4,
            completed_at=T6,
            decided_at=T7,
            predecessor_strategy_version_id="strategy-v1",
            predecessor_model_version_id="model-v1",
        ),
        points=points,
        rule=rule,
    )

    assert result.verdict is PromotionVerdict.REJECT
    assert result.strategy_version_id == challenger.policy_id
    assert store.exists("transparent-bandit-policy", challenger.policy_id)

    reopened = ScientificRegistry(registry_path)
    strategy = reopened.get("StrategyVersion", challenger.policy_id)
    assert strategy is not None
    assert strategy.payload["environment_sha256"] == challenger.environment_id
    assert strategy.payload["config_sha256"] == challenger.config_sha256

    experiment = reopened.get("Experiment", "experiment-v2")
    assert experiment is not None
    assert experiment.payload["strategy_version_id"] == challenger.policy_id
    assert experiment.payload["outcome"] == "NEGATIVE"

    decision = reopened.get("PromotionDecision", "promotion-v2")
    assert decision is not None
    assert decision.payload["action"] == "REJECT"
    assert decision.payload["candidate_strategy_version_id"] == challenger.policy_id

    postmortem = reopened.get("Postmortem", "experiment-v2:postmortem")
    assert postmortem is not None
    assert postmortem.payload["classification"] == "NEGATIVE"
    assert postmortem.payload["retest_conditions"]

    reproducibility = reopened.reproducibility_bundle("experiment-v2")
    assert len(reproducibility["bundle_sha256"]) == 64
