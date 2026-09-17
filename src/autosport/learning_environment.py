"""Causal, restart-safe experiential-learning environment contracts.

This module is deliberately paper/shadow-only.  It owns causal environment identity,
observation/action/outcome/reward evidence and checkpoint semantics, but it does not
own EconomicGoal, RiskPolicy, PaperBook, execution authority or model promotion.
Learned policies can choose only from an externally supplied admissible action set.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Final


ENVIRONMENT_SCHEMA: Final = "autosport.learning_environment"
ENVIRONMENT_SCHEMA_VERSION: Final = 1
_EMPTY_CHAIN_SHA256: Final = hashlib.sha256(b"").hexdigest()


class LearningEnvironmentError(ValueError):
    """Raised when causal environment evidence is invalid or conflicts."""


class EvidenceTruth(str, Enum):
    """Mechanical truth boundary for factual versus counterfactual evidence."""

    OBSERVED = "observed"
    SIMULATED = "simulated"


Metadata = tuple[tuple[str, str], ...]


def _canonical_text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise LearningEnvironmentError(f"{name} must be a non-empty canonical string")
    if "\x00" in value:
        raise LearningEnvironmentError(f"{name} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise LearningEnvironmentError(f"{name} must be valid UTF-8 text") from exc
    return value


def _timestamp(name: str, value: object) -> datetime:
    text = _canonical_text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LearningEnvironmentError(f"{name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LearningEnvironmentError(f"{name} must be timezone-aware ISO-8601")
    return parsed


def _sha256_hex(name: str, value: object) -> str:
    text = _canonical_text(name, value)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise LearningEnvironmentError(f"{name} must be lowercase SHA-256 hex")
    return text


def _metadata(name: str, value: object) -> Metadata:
    if type(value) is not tuple:
        raise LearningEnvironmentError(f"{name} must be a tuple of canonical key/value pairs")
    normalized: list[tuple[str, str]] = []
    for entry in value:
        if type(entry) is not tuple or len(entry) != 2:
            raise LearningEnvironmentError(
                f"{name} entries must be two-item canonical tuples"
            )
        key = _canonical_text(f"{name} key", entry[0])
        item = _canonical_text(f"{name} value", entry[1])
        normalized.append((key, item))
    if normalized != sorted(normalized):
        raise LearningEnvironmentError(f"{name} must be sorted by key/value")
    keys = [key for key, _ in normalized]
    if len(keys) != len(set(keys)):
        raise LearningEnvironmentError(f"{name} keys must be unique")
    return tuple(normalized)


def _canonical_set(name: str, value: object) -> frozenset[str]:
    if not isinstance(value, frozenset) or not value:
        raise LearningEnvironmentError(f"{name} must be a non-empty frozenset")
    for item in value:
        _canonical_text(f"{name} member", item)
    return value


def _canonical_decimal(name: str, value: object) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise LearningEnvironmentError(f"{name} must be a finite exact Decimal")
    return value


def _stable_hash(payload: object) -> str:
    try:
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise LearningEnvironmentError("environment identity payload is not canonical") from exc
    return hashlib.sha256(raw).hexdigest()


def _metadata_payload(value: Metadata) -> list[list[str]]:
    return [[key, item] for key, item in value]


@dataclass(frozen=True, slots=True)
class EnvironmentIdentity:
    """Immutable identity of one causal environment configuration and data cutoff."""

    source_id: str
    config_id: str
    data_id: str
    protocol_id: str
    cutoff_ts: str
    seed: int
    schema: str = ENVIRONMENT_SCHEMA
    schema_version: int = ENVIRONMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != ENVIRONMENT_SCHEMA:
            raise LearningEnvironmentError("unsupported learning environment schema")
        if self.schema_version != ENVIRONMENT_SCHEMA_VERSION:
            raise LearningEnvironmentError("unsupported learning environment schema version")
        _canonical_text("source_id", self.source_id)
        _canonical_text("config_id", self.config_id)
        _canonical_text("data_id", self.data_id)
        _canonical_text("protocol_id", self.protocol_id)
        _timestamp("cutoff_ts", self.cutoff_ts)
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise LearningEnvironmentError("seed must be a non-negative integer")

    @property
    def environment_id(self) -> str:
        return _stable_hash(
            {
                "schema": self.schema,
                "schema_version": self.schema_version,
                "source_id": self.source_id,
                "config_id": self.config_id,
                "data_id": self.data_id,
                "protocol_id": self.protocol_id,
                "cutoff_ts": self.cutoff_ts,
                "seed": self.seed,
            }
        )


@dataclass(frozen=True, slots=True)
class Observation:
    """Evidence visible to a policy only after ``available_at``."""

    environment_id: str
    observed_at: str
    available_at: str
    evidence: Metadata

    def __post_init__(self) -> None:
        _sha256_hex("environment_id", self.environment_id)
        observed = _timestamp("observed_at", self.observed_at)
        available = _timestamp("available_at", self.available_at)
        if available < observed:
            raise LearningEnvironmentError("observation cannot be available before observed_at")
        _metadata("observation evidence", self.evidence)

    @property
    def observation_id(self) -> str:
        return _stable_hash(
            {
                "environment_id": self.environment_id,
                "observed_at": self.observed_at,
                "available_at": self.available_at,
                "evidence": _metadata_payload(self.evidence),
            }
        )


@dataclass(frozen=True, slots=True)
class Action:
    """One policy choice from an externally supplied admissible action set."""

    environment_id: str
    observation_id: str
    action_type: str
    decided_at: str
    parameters: Metadata = ()

    def __post_init__(self) -> None:
        _sha256_hex("environment_id", self.environment_id)
        _sha256_hex("observation_id", self.observation_id)
        _canonical_text("action_type", self.action_type)
        _timestamp("decided_at", self.decided_at)
        _metadata("action parameters", self.parameters)

    @property
    def action_id(self) -> str:
        return _stable_hash(
            {
                "environment_id": self.environment_id,
                "observation_id": self.observation_id,
                "action_type": self.action_type,
                "decided_at": self.decided_at,
                "parameters": _metadata_payload(self.parameters),
            }
        )


@dataclass(frozen=True, slots=True)
class Outcome:
    """Observed factual or explicitly simulated/counterfactual action outcome."""

    environment_id: str
    action_id: str
    revealed_at: str
    truth: EvidenceTruth
    evidence: Metadata
    simulation_model_id: str | None = None

    def __post_init__(self) -> None:
        _sha256_hex("environment_id", self.environment_id)
        _sha256_hex("action_id", self.action_id)
        _timestamp("revealed_at", self.revealed_at)
        if not isinstance(self.truth, EvidenceTruth):
            raise LearningEnvironmentError("outcome truth must be EvidenceTruth")
        _metadata("outcome evidence", self.evidence)
        if self.truth is EvidenceTruth.SIMULATED:
            _canonical_text("simulation_model_id", self.simulation_model_id)
        elif self.simulation_model_id is not None:
            raise LearningEnvironmentError(
                "observed outcome cannot carry simulation_model_id"
            )

    @property
    def outcome_id(self) -> str:
        return _stable_hash(
            {
                "environment_id": self.environment_id,
                "action_id": self.action_id,
                "revealed_at": self.revealed_at,
                "truth": self.truth.value,
                "evidence": _metadata_payload(self.evidence),
                "simulation_model_id": self.simulation_model_id,
            }
        )


@dataclass(frozen=True, slots=True)
class RewardEvidence:
    """Exact reward value plus the factual/simulated evidence boundary that produced it."""

    environment_id: str
    action_id: str
    outcome_id: str
    reward: Decimal
    available_at: str
    truth: EvidenceTruth
    evidence: Metadata = ()
    simulation_model_id: str | None = None

    def __post_init__(self) -> None:
        _sha256_hex("environment_id", self.environment_id)
        _sha256_hex("action_id", self.action_id)
        _sha256_hex("outcome_id", self.outcome_id)
        _canonical_decimal("reward", self.reward)
        _timestamp("reward available_at", self.available_at)
        if not isinstance(self.truth, EvidenceTruth):
            raise LearningEnvironmentError("reward truth must be EvidenceTruth")
        _metadata("reward evidence", self.evidence)
        if self.truth is EvidenceTruth.SIMULATED:
            _canonical_text("reward simulation_model_id", self.simulation_model_id)
        elif self.simulation_model_id is not None:
            raise LearningEnvironmentError(
                "observed reward cannot carry simulation_model_id"
            )

    @property
    def reward_id(self) -> str:
        return _stable_hash(
            {
                "environment_id": self.environment_id,
                "action_id": self.action_id,
                "outcome_id": self.outcome_id,
                "reward": str(self.reward),
                "available_at": self.available_at,
                "truth": self.truth.value,
                "evidence": _metadata_payload(self.evidence),
                "simulation_model_id": self.simulation_model_id,
            }
        )


@dataclass(frozen=True, slots=True)
class Transition:
    """One causally valid OBSERVE -> ACT -> OUTCOME/REWARD transition."""

    environment_id: str
    episode_id: str
    step_index: int
    observation_id: str
    action_id: str
    outcome_id: str
    reward_id: str
    decision_at: str
    resolved_at: str

    def __post_init__(self) -> None:
        for name in (
            "environment_id",
            "episode_id",
            "observation_id",
            "action_id",
            "outcome_id",
            "reward_id",
        ):
            _sha256_hex(name, getattr(self, name))
        if isinstance(self.step_index, bool) or not isinstance(self.step_index, int):
            raise LearningEnvironmentError("step_index must be an integer")
        if self.step_index <= 0:
            raise LearningEnvironmentError("step_index must be positive")
        decision = _timestamp("decision_at", self.decision_at)
        resolved = _timestamp("resolved_at", self.resolved_at)
        if resolved < decision:
            raise LearningEnvironmentError("transition cannot resolve before decision_at")

    @property
    def transition_id(self) -> str:
        return _stable_hash(
            {
                "environment_id": self.environment_id,
                "episode_id": self.episode_id,
                "step_index": self.step_index,
                "observation_id": self.observation_id,
                "action_id": self.action_id,
                "outcome_id": self.outcome_id,
                "reward_id": self.reward_id,
                "decision_at": self.decision_at,
                "resolved_at": self.resolved_at,
            }
        )


@dataclass(frozen=True, slots=True)
class Episode:
    """Immutable policy/admissible-action identity for one environment episode."""

    environment_id: str
    episode_key: str
    policy_id: str
    admissible_actions: tuple[str, ...]

    def __post_init__(self) -> None:
        _sha256_hex("environment_id", self.environment_id)
        _canonical_text("episode_key", self.episode_key)
        _canonical_text("policy_id", self.policy_id)
        if type(self.admissible_actions) is not tuple or not self.admissible_actions:
            raise LearningEnvironmentError("admissible_actions must be a non-empty tuple")
        for action_type in self.admissible_actions:
            _canonical_text("admissible action", action_type)
        if self.admissible_actions != tuple(sorted(self.admissible_actions)):
            raise LearningEnvironmentError("admissible_actions must be sorted")
        if len(self.admissible_actions) != len(set(self.admissible_actions)):
            raise LearningEnvironmentError("admissible_actions must be unique")

    @property
    def episode_id(self) -> str:
        return _stable_hash(
            {
                "environment_id": self.environment_id,
                "episode_key": self.episode_key,
                "policy_id": self.policy_id,
                "admissible_actions": list(self.admissible_actions),
            }
        )


@dataclass(frozen=True, slots=True)
class EnvironmentCheckpoint:
    """Restart-safe chain witness for one fully resolved episode prefix."""

    environment_id: str
    episode_id: str
    policy_id: str
    step_index: int
    chain_sha256: str
    last_transition_id: str | None
    committed_action_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _sha256_hex("environment_id", self.environment_id)
        _sha256_hex("episode_id", self.episode_id)
        _canonical_text("policy_id", self.policy_id)
        if isinstance(self.step_index, bool) or not isinstance(self.step_index, int):
            raise LearningEnvironmentError("checkpoint step_index must be an integer")
        if self.step_index < 0:
            raise LearningEnvironmentError("checkpoint step_index must be non-negative")
        _sha256_hex("chain_sha256", self.chain_sha256)
        if self.last_transition_id is not None:
            _sha256_hex("last_transition_id", self.last_transition_id)
        if type(self.committed_action_ids) is not tuple:
            raise LearningEnvironmentError("committed_action_ids must be a tuple")
        for action_id in self.committed_action_ids:
            _sha256_hex("committed action_id", action_id)
        if self.committed_action_ids != tuple(sorted(self.committed_action_ids)):
            raise LearningEnvironmentError("committed_action_ids must be sorted")
        if len(self.committed_action_ids) != len(set(self.committed_action_ids)):
            raise LearningEnvironmentError("committed_action_ids must be unique")
        if self.step_index == 0:
            if self.last_transition_id is not None or self.committed_action_ids:
                raise LearningEnvironmentError("empty checkpoint cannot contain committed actions")
            if self.chain_sha256 != _EMPTY_CHAIN_SHA256:
                raise LearningEnvironmentError("empty checkpoint chain hash is invalid")
        else:
            if self.last_transition_id is None:
                raise LearningEnvironmentError("non-empty checkpoint requires last_transition_id")
            if len(self.committed_action_ids) != self.step_index:
                raise LearningEnvironmentError(
                    "checkpoint committed_action_ids must match step_index"
                )

    @property
    def checkpoint_id(self) -> str:
        return _stable_hash(
            {
                "environment_id": self.environment_id,
                "episode_id": self.episode_id,
                "policy_id": self.policy_id,
                "step_index": self.step_index,
                "chain_sha256": self.chain_sha256,
                "last_transition_id": self.last_transition_id,
                "committed_action_ids": list(self.committed_action_ids),
            }
        )


@dataclass(frozen=True, slots=True)
class _PendingAction:
    observation: Observation
    action: Action


class CausalLearningEnvironment:
    """Minimal deterministic causal environment state machine.

    The class intentionally does not choose an action or compute reward.  A caller
    supplies an externally admissible action set, a policy supplies one member, and
    authoritative outcome/reward evidence can be attached only after its reveal
    boundary.  This keeps financial/risk authority outside the learned policy.
    """

    def __init__(
        self,
        identity: EnvironmentIdentity,
        *,
        episode_key: str,
        policy_id: str,
        admissible_actions: frozenset[str],
    ) -> None:
        if not isinstance(identity, EnvironmentIdentity):
            raise TypeError("identity must be EnvironmentIdentity")
        actions = _canonical_set("admissible_actions", admissible_actions)
        self.identity = identity
        self.episode = Episode(
            environment_id=identity.environment_id,
            episode_key=_canonical_text("episode_key", episode_key),
            policy_id=_canonical_text("policy_id", policy_id),
            admissible_actions=tuple(sorted(actions)),
        )
        self._pending: dict[str, _PendingAction] = {}
        self._committed_action_ids: set[str] = set()
        self._step_index = 0
        self._chain_sha256 = _EMPTY_CHAIN_SHA256
        self._last_transition_id: str | None = None

    @property
    def environment_id(self) -> str:
        return self.identity.environment_id

    def act(
        self,
        observation: Observation,
        *,
        action_type: str,
        decision_at: str,
        parameters: Metadata = (),
    ) -> Action:
        if not isinstance(observation, Observation):
            raise TypeError("observation must be Observation")
        if observation.environment_id != self.environment_id:
            raise LearningEnvironmentError("observation belongs to another environment")
        action_name = _canonical_text("action_type", action_type)
        if action_name not in self.episode.admissible_actions:
            raise LearningEnvironmentError("action is outside the externally admissible set")
        decision_time = _timestamp("decision_at", decision_at)
        available_time = _timestamp("observation available_at", observation.available_at)
        if available_time > decision_time:
            raise LearningEnvironmentError("future observation evidence is not available at decision time")
        action = Action(
            environment_id=self.environment_id,
            observation_id=observation.observation_id,
            action_type=action_name,
            decided_at=decision_at,
            parameters=_metadata("action parameters", parameters),
        )
        if action.action_id in self._committed_action_ids:
            raise LearningEnvironmentError("action identity was already committed before restart")
        existing = self._pending.get(action.action_id)
        pending = _PendingAction(observation=observation, action=action)
        if existing is not None:
            if existing != pending:
                raise LearningEnvironmentError("action identity conflicts with pending evidence")
            return existing.action
        self._pending[action.action_id] = pending
        return action

    def resolve(
        self,
        action_id: str,
        *,
        outcome: Outcome,
        reward: RewardEvidence,
        resolved_at: str,
    ) -> Transition:
        _sha256_hex("action_id", action_id)
        pending = self._pending.get(action_id)
        if pending is None:
            raise LearningEnvironmentError("unknown or already resolved action identity")
        if not isinstance(outcome, Outcome) or not isinstance(reward, RewardEvidence):
            raise TypeError("outcome and reward must be canonical environment evidence")
        action = pending.action
        if outcome.environment_id != self.environment_id or reward.environment_id != self.environment_id:
            raise LearningEnvironmentError("resolution evidence belongs to another environment")
        if outcome.action_id != action.action_id or reward.action_id != action.action_id:
            raise LearningEnvironmentError("resolution evidence does not bind the action identity")
        if reward.outcome_id != outcome.outcome_id:
            raise LearningEnvironmentError("reward does not bind the exact outcome identity")
        if reward.truth is not outcome.truth:
            raise LearningEnvironmentError("reward truth label conflicts with outcome truth")
        if reward.simulation_model_id != outcome.simulation_model_id:
            raise LearningEnvironmentError("reward simulation identity conflicts with outcome")

        decision_time = _timestamp("action decided_at", action.decided_at)
        reveal_time = _timestamp("outcome revealed_at", outcome.revealed_at)
        reward_time = _timestamp("reward available_at", reward.available_at)
        resolution_time = _timestamp("resolved_at", resolved_at)
        if reveal_time < decision_time or reward_time < decision_time:
            raise LearningEnvironmentError("outcome/reward leaks before the action decision")
        if reward_time < reveal_time:
            raise LearningEnvironmentError(
                "reward evidence cannot be available before outcome reveal"
            )
        if resolution_time < reveal_time or resolution_time < reward_time:
            raise LearningEnvironmentError("outcome/reward is not yet available at resolve time")

        transition = Transition(
            environment_id=self.environment_id,
            episode_id=self.episode.episode_id,
            step_index=self._step_index + 1,
            observation_id=pending.observation.observation_id,
            action_id=action.action_id,
            outcome_id=outcome.outcome_id,
            reward_id=reward.reward_id,
            decision_at=action.decided_at,
            resolved_at=resolved_at,
        )
        next_chain = hashlib.sha256(
            bytes.fromhex(self._chain_sha256) + bytes.fromhex(transition.transition_id)
        ).hexdigest()
        self._step_index = transition.step_index
        self._chain_sha256 = next_chain
        self._last_transition_id = transition.transition_id
        self._committed_action_ids.add(action.action_id)
        del self._pending[action.action_id]
        return transition

    def checkpoint(self) -> EnvironmentCheckpoint:
        if self._pending:
            raise LearningEnvironmentError(
                "cannot checkpoint with unresolved actions; resolve or discard explicitly"
            )
        return EnvironmentCheckpoint(
            environment_id=self.environment_id,
            episode_id=self.episode.episode_id,
            policy_id=self.episode.policy_id,
            step_index=self._step_index,
            chain_sha256=self._chain_sha256,
            last_transition_id=self._last_transition_id,
            committed_action_ids=tuple(sorted(self._committed_action_ids)),
        )

    @classmethod
    def resume(
        cls,
        identity: EnvironmentIdentity,
        *,
        episode_key: str,
        policy_id: str,
        admissible_actions: frozenset[str],
        checkpoint: EnvironmentCheckpoint,
    ) -> "CausalLearningEnvironment":
        if not isinstance(checkpoint, EnvironmentCheckpoint):
            raise TypeError("checkpoint must be EnvironmentCheckpoint")
        environment = cls(
            identity,
            episode_key=episode_key,
            policy_id=policy_id,
            admissible_actions=admissible_actions,
        )
        if checkpoint.environment_id != environment.environment_id:
            raise LearningEnvironmentError("checkpoint environment identity mismatch")
        if checkpoint.episode_id != environment.episode.episode_id:
            raise LearningEnvironmentError("checkpoint episode identity mismatch")
        if checkpoint.policy_id != environment.episode.policy_id:
            raise LearningEnvironmentError("checkpoint policy identity mismatch")
        environment._step_index = checkpoint.step_index
        environment._chain_sha256 = checkpoint.chain_sha256
        environment._last_transition_id = checkpoint.last_transition_id
        environment._committed_action_ids = set(checkpoint.committed_action_ids)
        return environment
