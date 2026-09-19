"""Bind one scientifically promoted policy to a fresh causal AgentLoop episode.

This is the production activation seam between durable scientific memory and the
next paper/shadow decision.  It intentionally does not own risk, execution effects,
provider access, reward construction, or promotion authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .agent_loop import AgentLoopPhase, AgentLoopRuntime
from .champion_policy import load_champion_policy
from .learning_environment import (
    Action,
    CausalLearningEnvironment,
    EnvironmentCheckpoint,
    EnvironmentIdentity,
    Observation,
)
from .scientific_registry import ScientificRegistry
from .strategy_model_factory import FactoryArtifactStore
from .transparent_bandit_policy import BanditPolicyState


class ChampionAgentEpisodeError(RuntimeError):
    """Champion activation conflicts with the causal episode or AgentLoop."""


def _instant(value: object, name: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise ChampionAgentEpisodeError(f"{name} must be canonical ISO-8601 text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ChampionAgentEpisodeError(
            f"{name} must be timezone-aware ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ChampionAgentEpisodeError(
            f"{name} must be timezone-aware ISO-8601"
        )
    return parsed


@dataclass(frozen=True, slots=True)
class ChampionAgentEpisode:
    """Exact promoted policy, causal environment, and durable loop identity."""

    policy: BanditPolicyState
    environment: CausalLearningEnvironment
    agent_loop: AgentLoopRuntime

    def __post_init__(self) -> None:
        if not isinstance(self.policy, BanditPolicyState):
            raise TypeError("policy must be BanditPolicyState")
        if not isinstance(self.environment, CausalLearningEnvironment):
            raise TypeError("environment must be CausalLearningEnvironment")
        if not isinstance(self.agent_loop, AgentLoopRuntime):
            raise TypeError("agent_loop must be AgentLoopRuntime")

        episode = self.environment.episode
        snapshot = self.agent_loop.snapshot()
        policy_actions = frozenset(item.action_type for item in self.policy.estimates)
        episode_actions = frozenset(episode.admissible_actions)
        if self.policy.environment_id != self.environment.environment_id:
            raise ChampionAgentEpisodeError("policy/environment identity mismatch")
        if episode.policy_id != self.policy.policy_id:
            raise ChampionAgentEpisodeError("episode does not bind champion policy")
        if not episode_actions.issubset(policy_actions):
            raise ChampionAgentEpisodeError(
                "episode actions widen the champion policy universe"
            )
        if (
            snapshot.environment_id != episode.environment_id
            or snapshot.episode_id != episode.episode_id
            or snapshot.policy_id != episode.policy_id
        ):
            raise ChampionAgentEpisodeError(
                "AgentLoop identity does not bind champion episode"
            )
        if snapshot.config_sha256 != self.policy.config_sha256:
            raise ChampionAgentEpisodeError(
                "AgentLoop config does not bind champion policy"
            )

    @classmethod
    def initialize_pristine(
        cls,
        path: str | Path,
        registry: ScientificRegistry,
        artifact_store: FactoryArtifactStore,
        *,
        identity: EnvironmentIdentity,
        as_of: str,
        canonical_strategy_id: str,
        config_sha256: str,
        episode_key: str,
        admissible_actions: frozenset[str],
        loop_id: str,
        economic_goal_fingerprint: str,
        risk_fingerprint: str,
        source_sha256: str,
        at: str,
    ) -> "ChampionAgentEpisode":
        """Start a new paper/shadow episode from canonical champion authority."""

        if not isinstance(identity, EnvironmentIdentity):
            raise TypeError("identity must be EnvironmentIdentity")
        if _instant(as_of, "as_of") > _instant(at, "at"):
            raise ChampionAgentEpisodeError(
                "champion evidence is not available at AgentLoop initialization"
            )
        policy = load_champion_policy(
            registry,
            artifact_store,
            as_of=as_of,
            canonical_strategy_id=canonical_strategy_id,
            environment_id=identity.environment_id,
            protocol_id=identity.protocol_id,
            config_sha256=config_sha256,
            admissible_actions=admissible_actions,
        )
        environment = CausalLearningEnvironment(
            identity,
            episode_key=episode_key,
            policy_id=policy.policy_id,
            admissible_actions=admissible_actions,
        )
        agent_loop = AgentLoopRuntime.initialize_pristine(
            path,
            loop_id=loop_id,
            environment_checkpoint=environment.checkpoint(),
            policy_id=policy.policy_id,
            economic_goal_fingerprint=economic_goal_fingerprint,
            risk_fingerprint=risk_fingerprint,
            source_sha256=source_sha256,
            config_sha256=config_sha256,
            at=at,
        )
        return cls(policy, environment, agent_loop)

    @classmethod
    def resume(
        cls,
        path: str | Path,
        registry: ScientificRegistry,
        artifact_store: FactoryArtifactStore,
        *,
        identity: EnvironmentIdentity,
        checkpoint: EnvironmentCheckpoint,
        as_of: str,
        canonical_strategy_id: str,
        config_sha256: str,
        episode_key: str,
        admissible_actions: frozenset[str],
    ) -> "ChampionAgentEpisode":
        """Rebuild one exact champion episode from its durable checkpoint."""

        if not isinstance(identity, EnvironmentIdentity):
            raise TypeError("identity must be EnvironmentIdentity")
        if not isinstance(checkpoint, EnvironmentCheckpoint):
            raise TypeError("checkpoint must be EnvironmentCheckpoint")
        agent_loop = AgentLoopRuntime(path)
        snapshot = agent_loop.snapshot()
        if checkpoint.checkpoint_id != snapshot.environment_checkpoint_id:
            raise ChampionAgentEpisodeError(
                "checkpoint does not match durable AgentLoop checkpoint"
            )
        policy = load_champion_policy(
            registry,
            artifact_store,
            as_of=as_of,
            canonical_strategy_id=canonical_strategy_id,
            environment_id=identity.environment_id,
            protocol_id=identity.protocol_id,
            config_sha256=config_sha256,
            admissible_actions=admissible_actions,
        )
        environment = CausalLearningEnvironment.resume(
            identity,
            episode_key=episode_key,
            policy_id=policy.policy_id,
            admissible_actions=admissible_actions,
            checkpoint=checkpoint,
        )
        return cls(policy, environment, agent_loop)

    def decide(
        self,
        observation: Observation,
        *,
        decision_at: str,
        parameters: tuple[tuple[str, str], ...] = (),
    ) -> Action:
        """Choose inside current external authority and create canonical action."""

        snapshot = self.agent_loop.snapshot()
        if snapshot.phase is not AgentLoopPhase.ACT_OR_ABSTAIN:
            raise ChampionAgentEpisodeError(
                "champion decision requires AgentLoop ACT_OR_ABSTAIN phase"
            )
        if snapshot.observation_id != observation.observation_id:
            raise ChampionAgentEpisodeError(
                "observation does not bind durable AgentLoop evidence"
            )
        admitted = frozenset(self.environment.episode.admissible_actions)
        action_type = self.policy.choose(admissible_actions=admitted)
        return self.environment.act(
            observation,
            action_type=action_type,
            decision_at=decision_at,
            parameters=parameters,
        )


__all__ = ["ChampionAgentEpisode", "ChampionAgentEpisodeError"]
