from decimal import Decimal

import pytest

from autosport.experiential_learning import PolicyRetestSpec, run_policy_retest
from autosport.learning_environment import EvidenceTruth
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
        source_sha256="2" * 64,
        evaluator_source_sha256="3" * 64,
        created_at="2026-09-17T12:00:00Z",
        completed_at="2026-09-17T12:01:00Z",
        decided_at="2026-09-17T12:02:00Z",
        predecessor_strategy_version_id="strategy-predecessor",
    )


def _evidence(
    predecessor: BanditPolicyState,
    challenger: BanditPolicyState,
    *,
    update_index: int,
    action_id: str,
    reward_id: str,
) -> PolicyUpdateEvidence:
    return PolicyUpdateEvidence(
        environment_id=ENVIRONMENT_ID,
        protocol_id=predecessor.protocol_id,
        config_sha256=CONFIG_ID,
        seed=23,
        update_index=update_index,
        predecessor_policy_id=predecessor.policy_id,
        successor_policy_id=challenger.policy_id,
        transition_id=TRANSITION_ID,
        action_id=action_id,
        reward_id=reward_id,
        reward_truth=EvidenceTruth.OBSERVED,
        simulation_model_id=None,
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
    evidence = _evidence(
        predecessor,
        challenger,
        update_index=2,
        action_id=ACTION_2,
        reward_id=REWARD_2,
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
    evidence = _evidence(
        predecessor,
        challenger,
        update_index=1,
        action_id=ACTION_2,
        reward_id=REWARD_2,
    )

    with pytest.raises(ValueError, match="action does not match challenger history delta"):
        _invoke(predecessor, challenger, evidence)


def test_factory_bridge_requires_exactly_one_estimate_advance():
    predecessor = _predecessor()
    challenger = BanditPolicyState(
        environment_id=ENVIRONMENT_ID,
        protocol_id=predecessor.protocol_id,
        config_sha256=CONFIG_ID,
        seed=23,
        generation=1,
        estimates=(
            ActionEstimate("PASS", 0, Decimal("0")),
            ActionEstimate("WAIT", 0, Decimal("0")),
        ),
        applied_action_ids=(ACTION_1,),
        applied_reward_ids=(REWARD_1,),
        predecessor_policy_id=predecessor.policy_id,
    )
    evidence = _evidence(
        predecessor,
        challenger,
        update_index=1,
        action_id=ACTION_1,
        reward_id=REWARD_1,
    )

    with pytest.raises(ValueError, match="advance exactly one action estimate"):
        _invoke(predecessor, challenger, evidence)
