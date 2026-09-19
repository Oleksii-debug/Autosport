"""End-to-end seam from causal policy updates into the canonical research factory.

This module deliberately delegates evaluation, experiment memory and promotion/rejection
to the existing Strategy/Model Factory and ScientificRegistry.  It does not duplicate
those authorities and it never grants execution or financial authority to a policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .champion_policy import persist_policy_state
from .policy_evaluation import (
    PolicyEvaluationCase,
    PolicyEvaluationConfig,
    evaluate_policy_pair,
)
from .strategy_model_factory import (
    ExperimentRunner,
    FactoryCandidateSpec,
    FactoryRunResult,
    PromotionRule,
    TrainingPoint,
)
from .transparent_bandit_policy import BanditPolicyState, PolicyUpdateEvidence


@dataclass(frozen=True, slots=True)
class PolicyRetestSpec:
    """Typed bridge binding one updated policy identity to one governed factory run."""

    experiment_id: str
    model_version_id: str
    evaluation_bundle_id: str
    promotion_decision_id: str
    canonical_strategy_id: str
    dataset_snapshot_id: str
    feature_set_id: str
    source_sha256: str
    evaluator_source_sha256: str
    created_at: str
    completed_at: str
    decided_at: str
    predecessor_strategy_version_id: str
    predecessor_model_version_id: str | None = None

    def factory_spec(self, policy: BanditPolicyState) -> FactoryCandidateSpec:
        if not isinstance(policy, BanditPolicyState):
            raise TypeError("policy must be BanditPolicyState")
        return FactoryCandidateSpec(
            self.experiment_id,
            self.model_version_id,
            policy.policy_id,
            self.evaluation_bundle_id,
            self.promotion_decision_id,
            self.canonical_strategy_id,
            policy.protocol_id,
            self.dataset_snapshot_id,
            self.feature_set_id,
            self.source_sha256,
            policy.environment_id,
            self.evaluator_source_sha256,
            policy.seed,
            self.created_at,
            self.completed_at,
            self.decided_at,
            self.predecessor_strategy_version_id,
            self.predecessor_model_version_id,
        )


def _validate_exact_policy_successor(
    predecessor_policy: BanditPolicyState,
    challenger_policy: BanditPolicyState,
    update_evidence: PolicyUpdateEvidence,
) -> None:
    """Fail closed unless the challenger is exactly one durable policy update later."""

    if challenger_policy.generation != predecessor_policy.generation + 1:
        raise ValueError("challenger policy must be exactly one generation after predecessor")
    if update_evidence.update_index != challenger_policy.generation:
        raise ValueError("policy update evidence index does not match challenger generation")

    predecessor_actions = set(predecessor_policy.applied_action_ids)
    challenger_actions = set(challenger_policy.applied_action_ids)
    if not predecessor_actions.issubset(challenger_actions):
        raise ValueError("challenger policy discarded predecessor action history")
    if challenger_actions - predecessor_actions != {update_evidence.action_id}:
        raise ValueError("policy update evidence action does not match challenger history delta")

    predecessor_rewards = set(predecessor_policy.applied_reward_ids)
    challenger_rewards = set(challenger_policy.applied_reward_ids)
    if not predecessor_rewards.issubset(challenger_rewards):
        raise ValueError("challenger policy discarded predecessor reward history")
    if challenger_rewards - predecessor_rewards != {update_evidence.reward_id}:
        raise ValueError("policy update evidence reward does not match challenger history delta")

    predecessor_estimates = {
        estimate.action_type: estimate for estimate in predecessor_policy.estimates
    }
    challenger_estimates = {
        estimate.action_type: estimate for estimate in challenger_policy.estimates
    }
    if predecessor_estimates.keys() != challenger_estimates.keys():
        raise ValueError("challenger policy action universe differs from predecessor")

    advanced_estimates = 0
    for action_type, predecessor_estimate in predecessor_estimates.items():
        challenger_estimate = challenger_estimates[action_type]
        observation_delta = challenger_estimate.observations - predecessor_estimate.observations
        if observation_delta == 0:
            if challenger_estimate.reward_sum != predecessor_estimate.reward_sum:
                raise ValueError("challenger policy changed reward state without an observation")
            continue
        if observation_delta != 1:
            raise ValueError("challenger policy estimate advanced by more than one observation")
        advanced_estimates += 1
    if advanced_estimates != 1:
        raise ValueError("challenger policy must advance exactly one action estimate")


def _validate_causal_policy_successor(
    predecessor_policy: BanditPolicyState,
    challenger_policy: BanditPolicyState,
    update_evidence: PolicyUpdateEvidence,
) -> None:
    """Recompute the exact update from its immutable causal witnesses."""

    action = update_evidence.action
    reward = update_evidence.reward
    transition = update_evidence.transition
    if action is None or reward is None or transition is None:
        raise ValueError("policy update evidence lacks complete canonical causal witnesses")

    expected_challenger, expected_evidence = predecessor_policy.update(
        action=action,
        reward=reward,
        transition=transition,
    )
    if expected_challenger != challenger_policy:
        raise ValueError("challenger policy is not the exact causal policy successor")
    if expected_evidence != update_evidence:
        raise ValueError("policy update evidence does not match the exact causal policy update")


def run_policy_retest(
    runner: ExperimentRunner,
    *,
    predecessor_policy: BanditPolicyState,
    challenger_policy: BanditPolicyState,
    update_evidence: PolicyUpdateEvidence,
    spec: PolicyRetestSpec,
    points: Sequence[TrainingPoint] = (),
    rule: PromotionRule,
    evaluation_cases: Sequence[PolicyEvaluationCase] | None = None,
) -> FactoryRunResult:
    """Retest one exact policy successor through the canonical factory/evidence path."""
    if not isinstance(runner, ExperimentRunner):
        raise TypeError("runner must be ExperimentRunner")
    if not isinstance(predecessor_policy, BanditPolicyState):
        raise TypeError("predecessor_policy must be BanditPolicyState")
    if not isinstance(challenger_policy, BanditPolicyState):
        raise TypeError("challenger_policy must be BanditPolicyState")
    if not isinstance(update_evidence, PolicyUpdateEvidence):
        raise TypeError("update_evidence must be PolicyUpdateEvidence")
    if not isinstance(spec, PolicyRetestSpec):
        raise TypeError("spec must be PolicyRetestSpec")
    if not isinstance(rule, PromotionRule):
        raise TypeError("rule must be PromotionRule")

    if challenger_policy.predecessor_policy_id != predecessor_policy.policy_id:
        raise ValueError("challenger policy predecessor identity mismatch")
    if update_evidence.predecessor_policy_id != predecessor_policy.policy_id:
        raise ValueError("policy update predecessor identity mismatch")
    if update_evidence.successor_policy_id != challenger_policy.policy_id:
        raise ValueError("policy update successor identity mismatch")
    for name in ("environment_id", "protocol_id", "config_sha256", "seed"):
        if getattr(challenger_policy, name) != getattr(predecessor_policy, name):
            raise ValueError(f"policy update illegally rebound immutable {name}")
        if getattr(update_evidence, name) != getattr(challenger_policy, name):
            raise ValueError(f"policy update evidence {name} mismatch")
    _validate_exact_policy_successor(predecessor_policy, challenger_policy, update_evidence)
    _validate_causal_policy_successor(predecessor_policy, challenger_policy, update_evidence)

    protocol = runner.registry.get("ResearchProtocol", challenger_policy.protocol_id)
    if protocol is None:
        raise ValueError("challenger policy research protocol is missing from ScientificRegistry")
    binding = protocol.payload.get("binding")
    if type(binding) is not dict:
        raise ValueError("challenger policy research protocol lacks frozen binding")
    if binding.get("code_config_sha256") != challenger_policy.config_sha256:
        raise ValueError("challenger policy config does not match frozen research protocol")
    if protocol.payload.get("environment_sha256") != challenger_policy.environment_id:
        raise ValueError("challenger policy environment does not match frozen research protocol")

    factory_spec = spec.factory_spec(challenger_policy)
    if factory_spec.strategy_version_id != challenger_policy.policy_id:
        raise ValueError("factory candidate must use exact challenger policy identity")
    if factory_spec.environment_sha256 != challenger_policy.environment_id:
        raise ValueError("factory candidate environment identity mismatch")
    if factory_spec.research_protocol_id != challenger_policy.protocol_id:
        raise ValueError("factory candidate protocol identity mismatch")
    if factory_spec.seed != challenger_policy.seed:
        raise ValueError("factory candidate seed identity mismatch")

    if points:
        raise ValueError(
            "baseline TrainingPoint evidence cannot authorize a learned policy retest"
        )
    if evaluation_cases is None:
        raise ValueError(
            "policy-specific causal evaluation cases are required for learned policy retest"
        )
    evaluation_config = PolicyEvaluationConfig.from_frozen_text(
        binding.get("evaluation_design")
    )
    authority = evaluation_config.counterfactual_authority
    if (
        authority is not None
        and authority.evaluator_source_sha256
        != spec.evaluator_source_sha256.lower()
    ):
        raise ValueError(
            "counterfactual authority evaluator identity does not match factory spec"
        )
    evaluation = evaluate_policy_pair(
        predecessor_policy,
        challenger_policy,
        evaluation_cases,
        completed_at=spec.completed_at,
        abstain_action=evaluation_config.abstain_action,
        counterfactual_authority=authority,
    )
    if evaluation.dataset_manifest_sha256 != protocol.payload.get(
        "dataset_manifest_sha256"
    ):
        raise ValueError(
            "policy evaluation cohort does not match frozen research protocol dataset"
        )
    if spec.predecessor_strategy_version_id != predecessor_policy.policy_id:
        raise ValueError(
            "policy retest rollback strategy must be the exact evaluated predecessor policy"
        )

    # Immutable policy artifacts remain evidence only.  The atomic factory transaction
    # below is the sole path that can create promotion authority.
    persist_policy_state(runner.artifact_store, predecessor_policy)
    challenger_artifact_sha256 = persist_policy_state(
        runner.artifact_store, challenger_policy
    )
    return runner.run_policy_candidate(
        factory_spec,
        evaluation,
        rule=rule,
        policy_artifact_sha256=challenger_artifact_sha256,
    )
