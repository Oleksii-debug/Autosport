from decimal import Decimal

import pytest

from autosport.experiential_learning import PolicyRetestSpec, run_policy_retest
from autosport.learning_environment import (
    Action,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
    Transition,
)
from autosport.strategy_model_factory import ExperimentRunner, PromotionRule
from autosport.transparent_bandit_policy import (
    ActionEstimate,
    BanditPolicyState,
    PolicyUpdateEvidence,
)


ENVIRONMENT_ID = "a" * 64
CONFIG_ID = "b" * 64
ACTION_1 = "c" * 64
ACTION_2 = "d" * 64
REWARD_1 = "e" * 64
REWARD_2 = "f" * 64
TRANSITION_ID = "1" * 64
EPISODE_ID = "2" * 64


def _predecessor() -> BanditPolicyState:
    return BanditPolicyState.initial(
        environment_id=ENVIRONMENT_ID,
        protocol_id="protocol-successor-binding",
        config_sha256=CONFIG_ID,
        seed=23,
        action_types=frozenset({"WAIT", "PASS"}),
    )


def _spec() -> PolicyRetestSpec:
    return PolicyRetestSpec(
        experiment_id="experiment-successor-binding",
        model_version_id="model-successor-binding",
        evaluation_bundle_id="eval-successor-binding",
        promotion_decision_id="promotion-successor-binding",
        canonical_strategy_id="canonical-strategy",
        dataset_snapshot_id="dataset-successor-binding",
        feature_set_id="features-successor-binding",
        source_sha256="3" * 64,
        evaluator_source_sha256="4" * 64,
        created_at="2026-09-17T12:00:00Z",
        completed_at="2026-09-17T12:01:00Z",
        decided_at="2026-09-17T12:02:00Z",
        predecessor_strategy_version_id="strategy-predecessor",
    )


def _causal_witnesses(predecessor: BanditPolicyState):
    observation = Observation(
        environment_id=ENVIRONMENT_ID,
        observed_at="2026-09-17T11:59:00Z",
        available_at="2026-09-17T12:00:00Z",
        evidence=(("source", "fixture"),),
    )
    action = Action(
        environment_id=ENVIRONMENT_ID,
        observation_id=observation.observation_id,
        action_type="WAIT",
        decided_at="2026-09-17T12:00:01Z",
    )
    outcome = Outcome(
        environment_id=ENVIRONMENT_ID,
        action_id=action.action_id,
        revealed_at="2026-09-17T12:00:02Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(("result", "fixture"),),
    )
    reward = RewardEvidence(
        environment_id=ENVIRONMENT_ID,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal("1.25"),
        available_at="2026-09-17T12:00:03Z",
        truth=EvidenceTruth.OBSERVED,
    )
    transition = Transition(
        environment_id=ENVIRONMENT_ID,
        episode_id=EPISODE_ID,
        step_index=1,
        observation_id=observation.observation_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward_id=reward.reward_id,
        decision_at=action.decided_at,
        resolved_at=reward.available_at,
    )
    successor, evidence = predecessor.update(
        action=action,
        reward=reward,
        transition=transition,
    )
    assert evidence.transition_id == transition.transition_id
    return observation, action, reward, transition, successor, evidence


def _rebind_evidence(
    evidence: PolicyUpdateEvidence,
    challenger: BanditPolicyState,
) -> PolicyUpdateEvidence:
    return PolicyUpdateEvidence(
        environment_id=evidence.environment_id,
        protocol_id=evidence.protocol_id,
        config_sha256=evidence.config_sha256,
        seed=evidence.seed,
        update_index=evidence.update_index,
        predecessor_policy_id=evidence.predecessor_policy_id,
        successor_policy_id=challenger.policy_id,
        transition_id=evidence.transition_id,
        action_id=evidence.action_id,
        reward_id=evidence.reward_id,
        reward_truth=evidence.reward_truth,
        simulation_model_id=evidence.simulation_model_id,
        action=evidence.action,
        reward=evidence.reward,
        transition=evidence.transition,
    )


def _invoke(
    predecessor: BanditPolicyState,
    challenger: BanditPolicyState,
    evidence: PolicyUpdateEvidence,
) -> None:
    runner = object.__new__(ExperimentRunner)
    run_policy_retest(
        runner,
        predecessor_policy=predecessor,
        challenger_policy=challenger,
        update_evidence=evidence,
        spec=_spec(),
        points=(),
        rule=PromotionRule("mse", 0.05, (("max_squared_error", 1.0),)),
    )


def test_factory_bridge_rejects_generation_skip_before_registry_access():
    predecessor = _predecessor()
    challenger = BanditPolicyState(
        environment_id=ENVIRONMENT_ID,
        protocol_id=predecessor.protocol_id,
        config_sha256=CONFIG_ID,
        seed=23,
        generation=2,
        estimates=(
            ActionEstimate("PASS", 1, Decimal("0")),
            ActionEstimate("WAIT", 1, Decimal("1")),
        ),
        applied_action_ids=tuple(sorted((ACTION_1, ACTION_2))),
        applied_reward_ids=tuple(sorted((REWARD_1, REWARD_2))),
        predecessor_policy_id=predecessor.policy_id,
    )
    evidence = PolicyUpdateEvidence(
        environment_id=ENVIRONMENT_ID,
        protocol_id=predecessor.protocol_id,
        config_sha256=CONFIG_ID,
        seed=23,
        update_index=2,
        predecessor_policy_id=predecessor.policy_id,
        successor_policy_id=challenger.policy_id,
        transition_id=TRANSITION_ID,
        action_id=ACTION_2,
        reward_id=REWARD_2,
        reward_truth=EvidenceTruth.OBSERVED,
        simulation_model_id=None,
    )

    with pytest.raises(ValueError, match="exactly one generation"):
        _invoke(predecessor, challenger, evidence)


def test_factory_bridge_binds_update_evidence_to_history_delta():
    predecessor = _predecessor()
    challenger = BanditPolicyState(
        environment_id=ENVIRONMENT_ID,
        protocol_id=predecessor.protocol_id,
        config_sha256=CONFIG_ID,
        seed=23,
        generation=1,
        estimates=(
            ActionEstimate("PASS", 0, Decimal("0")),
            ActionEstimate("WAIT", 1, Decimal("1")),
        ),
        applied_action_ids=(ACTION_1,),
        applied_reward_ids=(REWARD_1,),
        predecessor_policy_id=predecessor.policy_id,
    )
    evidence = PolicyUpdateEvidence(
        environment_id=ENVIRONMENT_ID,
        protocol_id=predecessor.protocol_id,
        config_sha256=CONFIG_ID,
        seed=23,
        update_index=1,
        predecessor_policy_id=predecessor.policy_id,
        successor_policy_id=challenger.policy_id,
        transition_id=TRANSITION_ID,
        action_id=ACTION_2,
        reward_id=REWARD_2,
        reward_truth=EvidenceTruth.OBSERVED,
        simulation_model_id=None,
    )

    with pytest.raises(ValueError, match="action does not match challenger history delta"):
        _invoke(predecessor, challenger, evidence)


def test_factory_bridge_rejects_forged_reward_state_with_correct_history():
    predecessor = _predecessor()
    _, _, _, _, canonical_successor, canonical_evidence = _causal_witnesses(predecessor)
    estimates = list(canonical_successor.estimates)
    estimates[1] = ActionEstimate("WAIT", 1, Decimal("999"))
    challenger = BanditPolicyState(
        environment_id=canonical_successor.environment_id,
        protocol_id=canonical_successor.protocol_id,
        config_sha256=canonical_successor.config_sha256,
        seed=canonical_successor.seed,
        generation=canonical_successor.generation,
        estimates=tuple(estimates),
        applied_action_ids=canonical_successor.applied_action_ids,
        applied_reward_ids=canonical_successor.applied_reward_ids,
        predecessor_policy_id=predecessor.policy_id,
    )
    evidence = _rebind_evidence(canonical_evidence, challenger)

    assert challenger.applied_action_ids == canonical_successor.applied_action_ids
    assert challenger.applied_reward_ids == canonical_successor.applied_reward_ids
    with pytest.raises(ValueError, match="exact causal policy successor"):
        _invoke(predecessor, challenger, evidence)


def test_factory_bridge_rejects_wrong_advanced_action_estimate():
    predecessor = _predecessor()
    _, _, _, _, canonical_successor, canonical_evidence = _causal_witnesses(predecessor)
    estimates = list(canonical_successor.estimates)
    wait_estimate = estimates[1]
    estimates[0] = ActionEstimate("PASS", 1, wait_estimate.reward_sum)
    estimates[1] = ActionEstimate("WAIT", 0, Decimal("0"))
    challenger = BanditPolicyState(
        environment_id=canonical_successor.environment_id,
        protocol_id=canonical_successor.protocol_id,
        config_sha256=canonical_successor.config_sha256,
        seed=canonical_successor.seed,
        generation=canonical_successor.generation,
        estimates=tuple(estimates),
        applied_action_ids=canonical_successor.applied_action_ids,
        applied_reward_ids=canonical_successor.applied_reward_ids,
        predecessor_policy_id=predecessor.policy_id,
    )
    evidence = _rebind_evidence(canonical_evidence, challenger)

    with pytest.raises(ValueError, match="exact causal policy successor"):
        _invoke(predecessor, challenger, evidence)
