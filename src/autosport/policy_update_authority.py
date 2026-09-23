"""Fail-closed authority gate for product policy updates.

The transparent bandit learner is a generic causal learner. Product policy learning
must additionally bind to ``PolicyUtilityEvidence`` before state can change. Utility
schema v1 is contract-only, therefore this gate records the attempt and returns the
unchanged policy rather than falling back to raw reward.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Final

from .learning_environment import (
    Action,
    LearningEnvironmentError,
    RewardEvidence,
    Transition,
)
from .policy_utility_evidence import PolicyUtilityError, PolicyUtilityEvidence
from .transparent_bandit_policy import BanditPolicyState


SCHEMA: Final = "autosport.policy_update_authority"
SCHEMA_VERSION: Final = 1
UTILITY_AUTHORITY_UNRESOLVED: Final = "utility_authority_unresolved"


@dataclass(frozen=True, slots=True)
class UtilityBoundUpdateEvidence:
    """Immutable witness that one utility-bound update was blocked without mutation."""

    environment_id: str
    episode_id: str
    transition_id: str
    action_id: str
    reward_id: str
    utility_evidence_id: str
    utility_semantic_key: str
    predecessor_policy_id: str
    successor_policy_id: str
    reason_codes: tuple[str, ...]
    schema: str = SCHEMA
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != SCHEMA or self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported utility-bound update schema")
        if self.predecessor_policy_id != self.successor_policy_id:
            raise ValueError("blocked utility-bound update cannot change policy identity")
        if not self.reason_codes or self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("reason_codes must be a sorted unique non-empty tuple")

    @property
    def evidence_id(self) -> str:
        payload = {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "environment_id": self.environment_id,
            "episode_id": self.episode_id,
            "transition_id": self.transition_id,
            "action_id": self.action_id,
            "reward_id": self.reward_id,
            "utility_evidence_id": self.utility_evidence_id,
            "utility_semantic_key": self.utility_semantic_key,
            "predecessor_policy_id": self.predecessor_policy_id,
            "successor_policy_id": self.successor_policy_id,
            "reason_codes": list(self.reason_codes),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _validate_canonical_causal_witnesses(
    *,
    policy: BanditPolicyState,
    action: Action,
    reward: RewardEvidence,
    transition: Transition,
) -> None:
    """Reuse the generic learner's non-mutating causal admission boundary exactly."""

    if action.environment_id != policy.environment_id:
        raise LearningEnvironmentError("action belongs to another policy environment")
    if (
        reward.environment_id != policy.environment_id
        or transition.environment_id != policy.environment_id
    ):
        raise LearningEnvironmentError("reward/transition belongs to another policy environment")
    if reward.action_id != action.action_id or transition.action_id != action.action_id:
        raise LearningEnvironmentError("policy update does not bind the exact action")
    if transition.observation_id != action.observation_id:
        raise LearningEnvironmentError("policy update does not bind the exact observation")
    if transition.outcome_id != reward.outcome_id:
        raise LearningEnvironmentError("policy update does not bind the exact outcome")
    if transition.reward_id != reward.reward_id:
        raise LearningEnvironmentError("policy update does not bind the exact resolved reward")
    if action.action_id in policy.applied_action_ids:
        raise LearningEnvironmentError("action already has a durable policy update")
    if reward.reward_id in policy.applied_reward_ids:
        raise LearningEnvironmentError("reward already has a durable policy update")
    if action.action_type not in {estimate.action_type for estimate in policy.estimates}:
        raise LearningEnvironmentError("resolved action is outside immutable policy identity")


def attempt_utility_bound_update(
    *,
    policy: BanditPolicyState,
    action: Action,
    reward: RewardEvidence,
    transition: Transition,
    utility: PolicyUtilityEvidence,
) -> tuple[BanditPolicyState, UtilityBoundUpdateEvidence]:
    """Return an unchanged policy until owner-utility authority is resolved.

    Authority-bearing inputs must be the exact canonical concrete types. Accepting
    subclasses here would let a caller replace trusted properties/method dispatch
    while still passing ``isinstance`` checks, bypassing the exact-capability fence
    already enforced by the terminalizer composition boundary.
    """

    if type(policy) is not BanditPolicyState:
        raise TypeError("policy must be exact BanditPolicyState")
    if type(action) is not Action or type(reward) is not RewardEvidence:
        raise TypeError("action/reward evidence must use exact canonical types")
    if type(transition) is not Transition or type(utility) is not PolicyUtilityEvidence:
        raise TypeError("transition/utility evidence must use exact canonical types")

    _validate_canonical_causal_witnesses(
        policy=policy,
        action=action,
        reward=reward,
        transition=transition,
    )

    reasons: set[str] = set()
    if utility.environment_id != policy.environment_id:
        reasons.add("utility_environment_mismatch")
    if utility.action_id != action.action_id:
        reasons.add("utility_action_mismatch")
    if utility.outcome_id != reward.outcome_id:
        reasons.add("utility_outcome_mismatch")
    if utility.reward_id != reward.reward_id:
        reasons.add("utility_reward_mismatch")
    if utility.transition_id != transition.transition_id:
        reasons.add("utility_transition_mismatch")
    if utility.episode_id != transition.episode_id:
        reasons.add("utility_episode_mismatch")
    if utility.policy_id != policy.policy_id:
        reasons.add("utility_policy_mismatch")
    if utility.config_sha256 != policy.config_sha256:
        reasons.add("utility_config_mismatch")

    try:
        utility.require_policy_update_eligible()
    except PolicyUtilityError:
        reasons.add(UTILITY_AUTHORITY_UNRESOLVED)
    else:  # schema v1 cannot authorize a product policy update
        reasons.add(UTILITY_AUTHORITY_UNRESOLVED)

    evidence = UtilityBoundUpdateEvidence(
        environment_id=policy.environment_id,
        episode_id=transition.episode_id,
        transition_id=transition.transition_id,
        action_id=action.action_id,
        reward_id=reward.reward_id,
        utility_evidence_id=utility.evidence_id,
        utility_semantic_key=utility.semantic_key,
        predecessor_policy_id=policy.policy_id,
        successor_policy_id=policy.policy_id,
        reason_codes=tuple(sorted(reasons)),
    )
    return policy, evidence
