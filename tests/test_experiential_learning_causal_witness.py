from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.experiential_learning import PolicyRetestSpec, run_policy_retest
from autosport.learning_environment import Action, EvidenceTruth, RewardEvidence, Transition
from autosport.strategy_model_factory import ExperimentRunner, PromotionRule
from autosport.transparent_bandit_policy import ActionEstimate, BanditPolicyState


ENVIRONMENT_ID = "a" * 64
CONFIG_ID = "b" * 64


def _resolved_update():
    predecessor = BanditPolicyState.initial(
        environment_id=ENVIRONMENT_ID,
        protocol_id="protocol-causal-witness",
        config_sha256=CONFIG_ID,
        seed=23,
        action_types=frozenset({"PASS", "WAIT"}),
    )
    action = Action(
        environment_id=ENVIRONMENT_ID,
        observation_id="1" * 64,
        action_type="WAIT",
        decided_at="2026-09-17T12:00:00Z",
    )
    reward = RewardEvidence(
        environment_id=ENVIRONMENT_ID,
        action_id=action.action_id,
        outcome_id="2" * 64,
        reward=Decimal("1.25"),
        available_at="2026-09-17T12:00:01Z",
        truth=EvidenceTruth.OBSERVED,
    )
    transition = Transition(
        environment_id=ENVIRONMENT_ID,
        episode_id="3" * 64,
        step_index=1,
        observation_id=action.observation_id,
        action_id=action.action_id,
        outcome_id=reward.outcome_id,
        reward_id=reward.reward_id,
        decision_at=action.decided_at,
        resolved_at="2026-09-17T12:00:02Z",
    )
    challenger, evidence = predecessor.update(
        action=action,
        reward=reward,
        transition=transition,
    )
    return predecessor, challenger, evidence


def _spec() -> PolicyRetestSpec:
    return PolicyRetestSpec(
        experiment_id="experiment-causal-witness",
        model_version_id="model-causal-witness",
        evaluation_bundle_id="eval-causal-witness",
        promotion_decision_id="promotion-causal-witness",
        canonical_strategy_id="canonical-strategy",
        dataset_snapshot_id="dataset-causal-witness",
        feature_set_id="features-causal-witness",
        source_sha256="4" * 64,
        evaluator_source_sha256="5" * 64,
        created_at="2026-09-17T12:01:00Z",
        completed_at="2026-09-17T12:02:00Z",
        decided_at="2026-09-17T12:03:00Z",
        predecessor_strategy_version_id="strategy-predecessor",
    )


def _invoke(predecessor, challenger, evidence) -> None:
    run_policy_retest(
        object.__new__(ExperimentRunner),
        predecessor_policy=predecessor,
        challenger_policy=challenger,
        update_evidence=evidence,
        spec=_spec(),
        points=(),
        rule=PromotionRule("mse", 0.05, (("max_squared_error", 1.0),)),
    )


def test_factory_bridge_rejects_forged_reward_sum_with_matching_history_ids():
    predecessor, _, evidence = _resolved_update()
    forged = BanditPolicyState(
        environment_id=predecessor.environment_id,
        protocol_id=predecessor.protocol_id,
        config_sha256=predecessor.config_sha256,
        seed=predecessor.seed,
        generation=1,
        estimates=(
            ActionEstimate("PASS", 0, Decimal("0")),
            ActionEstimate("WAIT", 1, Decimal("999")),
        ),
        applied_action_ids=(evidence.action_id,),
        applied_reward_ids=(evidence.reward_id,),
        predecessor_policy_id=predecessor.policy_id,
    )
    forged_evidence = replace(evidence, successor_policy_id=forged.policy_id)

    with pytest.raises(ValueError, match="exact causal policy successor"):
        _invoke(predecessor, forged, forged_evidence)


def test_factory_bridge_rejects_reward_applied_to_wrong_action_estimate():
    predecessor, _, evidence = _resolved_update()
    forged = BanditPolicyState(
        environment_id=predecessor.environment_id,
        protocol_id=predecessor.protocol_id,
        config_sha256=predecessor.config_sha256,
        seed=predecessor.seed,
        generation=1,
        estimates=(
            ActionEstimate("PASS", 1, Decimal("1.25")),
            ActionEstimate("WAIT", 0, Decimal("0")),
        ),
        applied_action_ids=(evidence.action_id,),
        applied_reward_ids=(evidence.reward_id,),
        predecessor_policy_id=predecessor.policy_id,
    )
    forged_evidence = replace(evidence, successor_policy_id=forged.policy_id)

    with pytest.raises(ValueError, match="exact causal policy successor"):
        _invoke(predecessor, forged, forged_evidence)
