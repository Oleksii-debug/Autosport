"""End-to-end seam from causal policy updates into the canonical research factory.

This module deliberately delegates evaluation, experiment memory and promotion/rejection
to the existing Strategy/Model Factory and ScientificRegistry.  It does not duplicate
those authorities and it never grants execution or financial authority to a policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

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


def run_policy_retest(
    runner: ExperimentRunner,
    *,
    predecessor_policy: BanditPolicyState,
    challenger_policy: BanditPolicyState,
    update_evidence: PolicyUpdateEvidence,
    spec: PolicyRetestSpec,
    points: Sequence[TrainingPoint],
    rule: PromotionRule,
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

    factory_spec = spec.factory_spec(challenger_policy)
    if factory_spec.strategy_version_id != challenger_policy.policy_id:
        raise ValueError("factory candidate must use exact challenger policy identity")
    if factory_spec.environment_sha256 != challenger_policy.environment_id:
        raise ValueError("factory candidate environment identity mismatch")
    if factory_spec.research_protocol_id != challenger_policy.protocol_id:
        raise ValueError("factory candidate protocol identity mismatch")
    if factory_spec.seed != challenger_policy.seed:
        raise ValueError("factory candidate seed identity mismatch")

    return runner.run_baseline_candidate(
        factory_spec,
        points,
        rule=rule,
    )
