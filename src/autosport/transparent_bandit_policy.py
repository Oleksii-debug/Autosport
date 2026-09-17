"""Transparent bounded learner for the causal paper/shadow environment.

The policy owns only deterministic action preference state.  It cannot manufacture
admissible actions, money, stake, risk limits, execution authority, outcomes or
rewards.  Updates are accepted only from an exact already-resolved causal transition.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from .learning_environment import (
    Action,
    EvidenceTruth,
    LearningEnvironmentError,
    RewardEvidence,
    Transition,
)


POLICY_SCHEMA: Final = "autosport.transparent_bandit_policy"
POLICY_SCHEMA_VERSION: Final = 1


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise LearningEnvironmentError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8", errors="strict")
    return value


def _sha256(name: str, value: object) -> str:
    text = _text(name, value)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise LearningEnvironmentError(f"{name} must be lowercase SHA-256 hex")
    return text


def _exact_decimal(name: str, value: object) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise LearningEnvironmentError(f"{name} must be a finite exact Decimal")
    return value


def _stable_hash(payload: object) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise LearningEnvironmentError("policy identity payload is not canonical") from exc
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ActionEstimate:
    """Exact transparent empirical reward accumulator for one action type."""

    action_type: str
    observations: int
    reward_sum: Decimal

    def __post_init__(self) -> None:
        _text("action_type", self.action_type)
        if isinstance(self.observations, bool) or not isinstance(self.observations, int):
            raise LearningEnvironmentError("observations must be an integer")
        if self.observations < 0:
            raise LearningEnvironmentError("observations must be non-negative")
        _exact_decimal("reward_sum", self.reward_sum)
        if self.observations == 0 and self.reward_sum != Decimal("0"):
            raise LearningEnvironmentError("unobserved action estimate must have zero reward_sum")

    @property
    def mean_reward(self) -> Decimal:
        if self.observations == 0:
            return Decimal("0")
        return self.reward_sum / Decimal(self.observations)

    def to_payload(self) -> dict[str, object]:
        return {
            "action_type": self.action_type,
            "observations": self.observations,
            "reward_sum": str(self.reward_sum),
        }


@dataclass(frozen=True, slots=True)
class BanditPolicyState:
    """Immutable restart-safe policy state; selection is deterministic and bounded."""

    environment_id: str
    protocol_id: str
    config_sha256: str
    seed: int
    generation: int
    estimates: tuple[ActionEstimate, ...]
    applied_action_ids: tuple[str, ...] = ()
    applied_reward_ids: tuple[str, ...] = ()
    predecessor_policy_id: str | None = None
    schema: str = POLICY_SCHEMA
    schema_version: int = POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != POLICY_SCHEMA or self.schema_version != POLICY_SCHEMA_VERSION:
            raise LearningEnvironmentError("unsupported transparent policy schema")
        _sha256("environment_id", self.environment_id)
        _text("protocol_id", self.protocol_id)
        _sha256("config_sha256", self.config_sha256)
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise LearningEnvironmentError("seed must be a non-negative integer")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise LearningEnvironmentError("generation must be an integer")
        if self.generation < 0:
            raise LearningEnvironmentError("generation must be non-negative")
        if type(self.estimates) is not tuple or not self.estimates:
            raise LearningEnvironmentError("estimates must be a non-empty tuple")
        if any(not isinstance(item, ActionEstimate) for item in self.estimates):
            raise LearningEnvironmentError("estimates must contain ActionEstimate values")
        action_types = tuple(item.action_type for item in self.estimates)
        if action_types != tuple(sorted(action_types)) or len(action_types) != len(set(action_types)):
            raise LearningEnvironmentError("policy action types must be sorted and unique")
        for name, identities in (
            ("applied_action_ids", self.applied_action_ids),
            ("applied_reward_ids", self.applied_reward_ids),
        ):
            if type(identities) is not tuple:
                raise LearningEnvironmentError(f"{name} must be a tuple")
            for identity in identities:
                _sha256(name, identity)
            if identities != tuple(sorted(identities)) or len(identities) != len(set(identities)):
                raise LearningEnvironmentError(f"{name} must be sorted and unique")
        if len(self.applied_action_ids) != len(self.applied_reward_ids):
            raise LearningEnvironmentError("applied action/reward histories must have equal length")
        if self.generation != len(self.applied_action_ids):
            raise LearningEnvironmentError("generation must equal applied update count")
        if self.generation == 0:
            if self.predecessor_policy_id is not None:
                raise LearningEnvironmentError("initial policy cannot have predecessor_policy_id")
        else:
            _sha256("predecessor_policy_id", self.predecessor_policy_id)

    @classmethod
    def initial(
        cls,
        *,
        environment_id: str,
        protocol_id: str,
        config_sha256: str,
        seed: int,
        action_types: frozenset[str],
    ) -> "BanditPolicyState":
        if not isinstance(action_types, frozenset) or not action_types:
            raise LearningEnvironmentError("action_types must be a non-empty frozenset")
        canonical = sorted(_text("action_type", value) for value in action_types)
        if len(canonical) != len(set(canonical)):
            raise LearningEnvironmentError("action_types must be unique")
        return cls(
            environment_id=_sha256("environment_id", environment_id),
            protocol_id=_text("protocol_id", protocol_id),
            config_sha256=_sha256("config_sha256", config_sha256),
            seed=seed,
            generation=0,
            estimates=tuple(ActionEstimate(name, 0, Decimal("0")) for name in canonical),
        )

    @property
    def policy_id(self) -> str:
        return _stable_hash(
            {
                "schema": self.schema,
                "schema_version": self.schema_version,
                "environment_id": self.environment_id,
                "protocol_id": self.protocol_id,
                "config_sha256": self.config_sha256,
                "seed": self.seed,
                "generation": self.generation,
                "estimates": [item.to_payload() for item in self.estimates],
                "applied_action_ids": list(self.applied_action_ids),
                "applied_reward_ids": list(self.applied_reward_ids),
                "predecessor_policy_id": self.predecessor_policy_id,
            }
        )

    def choose(self, *, admissible_actions: frozenset[str]) -> str:
        """Choose only among owner-supplied actions; deterministic ties are lexical."""
        if not isinstance(admissible_actions, frozenset) or not admissible_actions:
            raise LearningEnvironmentError("admissible_actions must be a non-empty frozenset")
        admitted = {_text("admissible action", value) for value in admissible_actions}
        estimates = {item.action_type: item for item in self.estimates}
        unknown = admitted - set(estimates)
        if unknown:
            raise LearningEnvironmentError(
                "admissible action set contains action outside immutable policy identity"
            )
        ranked = sorted(
            admitted,
            key=lambda action_type: (-estimates[action_type].mean_reward, action_type),
        )
        return ranked[0]

    def update(
        self,
        *,
        action: Action,
        reward: RewardEvidence,
        transition: Transition,
    ) -> tuple["BanditPolicyState", "PolicyUpdateEvidence"]:
        if not isinstance(action, Action):
            raise TypeError("action must be Action")
        if not isinstance(reward, RewardEvidence):
            raise TypeError("reward must be RewardEvidence")
        if not isinstance(transition, Transition):
            raise TypeError("transition must be Transition")
        if action.environment_id != self.environment_id:
            raise LearningEnvironmentError("action belongs to another policy environment")
        if reward.environment_id != self.environment_id or transition.environment_id != self.environment_id:
            raise LearningEnvironmentError("reward/transition belongs to another policy environment")
        if reward.action_id != action.action_id or transition.action_id != action.action_id:
            raise LearningEnvironmentError("policy update does not bind the exact action")
        if transition.reward_id != reward.reward_id:
            raise LearningEnvironmentError("policy update does not bind the exact resolved reward")
        if action.action_id in self.applied_action_ids:
            raise LearningEnvironmentError("action already has a durable policy update")
        if reward.reward_id in self.applied_reward_ids:
            raise LearningEnvironmentError("reward already has a durable policy update")

        estimates = {item.action_type: item for item in self.estimates}
        current = estimates.get(action.action_type)
        if current is None:
            raise LearningEnvironmentError("resolved action is outside immutable policy identity")
        estimates[action.action_type] = ActionEstimate(
            action.action_type,
            current.observations + 1,
            current.reward_sum + reward.reward,
        )
        successor = BanditPolicyState(
            environment_id=self.environment_id,
            protocol_id=self.protocol_id,
            config_sha256=self.config_sha256,
            seed=self.seed,
            generation=self.generation + 1,
            estimates=tuple(estimates[name] for name in sorted(estimates)),
            applied_action_ids=tuple(sorted((*self.applied_action_ids, action.action_id))),
            applied_reward_ids=tuple(sorted((*self.applied_reward_ids, reward.reward_id))),
            predecessor_policy_id=self.policy_id,
        )
        evidence = PolicyUpdateEvidence(
            environment_id=self.environment_id,
            protocol_id=self.protocol_id,
            config_sha256=self.config_sha256,
            seed=self.seed,
            update_index=successor.generation,
            predecessor_policy_id=self.policy_id,
            successor_policy_id=successor.policy_id,
            transition_id=transition.transition_id,
            action_id=action.action_id,
            reward_id=reward.reward_id,
            reward_truth=reward.truth,
            simulation_model_id=reward.simulation_model_id,
        )
        return successor, evidence


@dataclass(frozen=True, slots=True)
class PolicyUpdateEvidence:
    """Immutable witness that one exact causal reward produced one policy successor."""

    environment_id: str
    protocol_id: str
    config_sha256: str
    seed: int
    update_index: int
    predecessor_policy_id: str
    successor_policy_id: str
    transition_id: str
    action_id: str
    reward_id: str
    reward_truth: EvidenceTruth
    simulation_model_id: str | None

    def __post_init__(self) -> None:
        _sha256("environment_id", self.environment_id)
        _text("protocol_id", self.protocol_id)
        _sha256("config_sha256", self.config_sha256)
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise LearningEnvironmentError("seed must be a non-negative integer")
        if isinstance(self.update_index, bool) or not isinstance(self.update_index, int):
            raise LearningEnvironmentError("update_index must be an integer")
        if self.update_index <= 0:
            raise LearningEnvironmentError("update_index must be positive")
        for name in (
            "predecessor_policy_id",
            "successor_policy_id",
            "transition_id",
            "action_id",
            "reward_id",
        ):
            _sha256(name, getattr(self, name))
        if not isinstance(self.reward_truth, EvidenceTruth):
            raise LearningEnvironmentError("reward_truth must be EvidenceTruth")
        if self.reward_truth is EvidenceTruth.SIMULATED:
            _text("simulation_model_id", self.simulation_model_id)
        elif self.simulation_model_id is not None:
            raise LearningEnvironmentError("observed policy update cannot carry simulation_model_id")

    @property
    def update_id(self) -> str:
        return _stable_hash(
            {
                "environment_id": self.environment_id,
                "protocol_id": self.protocol_id,
                "config_sha256": self.config_sha256,
                "seed": self.seed,
                "update_index": self.update_index,
                "predecessor_policy_id": self.predecessor_policy_id,
                "successor_policy_id": self.successor_policy_id,
                "transition_id": self.transition_id,
                "action_id": self.action_id,
                "reward_id": self.reward_id,
                "reward_truth": self.reward_truth.value,
                "simulation_model_id": self.simulation_model_id,
            }
        )
