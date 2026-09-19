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
from .policy_deployment import (
    ActivationBinding,
    DeploymentScope,
    load_deployment_authority,
    persist_deployment_authority,
    validate_activation_binding,
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
    training_identity: EnvironmentIdentity | None = None
    deployment_scope: DeploymentScope | None = None
    activation_binding: ActivationBinding | None = None

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
        deployment_values = (
            self.training_identity,
            self.deployment_scope,
            self.activation_binding,
        )
        if all(value is None for value in deployment_values):
            if self.policy.environment_id != self.environment.environment_id:
                raise ChampionAgentEpisodeError("policy/environment identity mismatch")
            if snapshot.activation_binding_id is not None:
                raise ChampionAgentEpisodeError(
                    "legacy episode cannot carry deployment activation binding"
                )
        elif any(value is None for value in deployment_values):
            raise ChampionAgentEpisodeError(
                "cross-session deployment authority must be complete"
            )
        else:
            assert self.training_identity is not None
            assert self.activation_binding is not None
            if self.policy.environment_id != self.training_identity.environment_id:
                raise ChampionAgentEpisodeError(
                    "policy no longer binds immutable training environment"
                )
            if (
                self.environment.environment_id
                != self.activation_binding.deployment_environment_id
            ):
                raise ChampionAgentEpisodeError(
                    "episode does not bind deployment environment"
                )
            if snapshot.activation_binding_id != self.activation_binding.binding_id:
                raise ChampionAgentEpisodeError(
                    "AgentLoop does not bind deployment activation"
                )
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
        training_identity: EnvironmentIdentity | None = None,
        deployment_scope: DeploymentScope | None = None,
        activation_binding: ActivationBinding | None = None,
    ) -> "ChampionAgentEpisode":
        """Start a new paper/shadow episode from canonical champion authority."""

        if not isinstance(identity, EnvironmentIdentity):
            raise TypeError("identity must be EnvironmentIdentity")
        if _instant(as_of, "as_of") > _instant(at, "at"):
            raise ChampionAgentEpisodeError(
                "champion evidence is not available at AgentLoop initialization"
            )
        deployment_values = (
            training_identity,
            deployment_scope,
            activation_binding,
        )
        if any(value is not None for value in deployment_values) and any(
            value is None for value in deployment_values
        ):
            raise ChampionAgentEpisodeError(
                "cross-session deployment authority must be complete"
            )
        if activation_binding is None:
            authority_identity = identity
            authority_as_of = as_of
            effective_episode_key = episode_key
            binding_id = None
        else:
            assert training_identity is not None
            assert deployment_scope is not None
            if _instant(activation_binding.activation_at, "activation_at") != _instant(
                at, "at"
            ):
                raise ChampionAgentEpisodeError(
                    "activation binding time must equal AgentLoop initialization"
                )
            authority_identity = training_identity
            authority_as_of = activation_binding.activation_at
            effective_episode_key = (
                f"{episode_key}|deployment:{activation_binding.binding_id}"
            )
            binding_id = activation_binding.binding_id
        policy = load_champion_policy(
            registry,
            artifact_store,
            as_of=authority_as_of,
            canonical_strategy_id=canonical_strategy_id,
            environment_id=authority_identity.environment_id,
            protocol_id=authority_identity.protocol_id,
            config_sha256=config_sha256,
            admissible_actions=admissible_actions,
        )
        if activation_binding is not None:
            assert training_identity is not None
            assert deployment_scope is not None
            validate_activation_binding(
                activation_binding,
                scope=deployment_scope,
                policy=policy,
                training_identity=training_identity,
                deployment_identity=identity,
                registry=registry,
                artifact_store=artifact_store,
                canonical_strategy_id=canonical_strategy_id,
                admissible_actions=admissible_actions,
                economic_goal_fingerprint=economic_goal_fingerprint,
                risk_fingerprint=risk_fingerprint,
            )
            persist_deployment_authority(
                path,
                scope=deployment_scope,
                binding=activation_binding,
                training_identity=training_identity,
                deployment_identity=identity,
            )
        environment = CausalLearningEnvironment(
            identity,
            episode_key=effective_episode_key,
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
            activation_binding_id=binding_id,
        )
        return cls(
            policy,
            environment,
            agent_loop,
            training_identity,
            deployment_scope,
            activation_binding,
        )

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
        training_identity: EnvironmentIdentity | None = None,
        deployment_scope: DeploymentScope | None = None,
        activation_binding: ActivationBinding | None = None,
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
        deployment_values = (
            training_identity,
            deployment_scope,
            activation_binding,
        )
        caller_supplied_authority = any(
            value is not None for value in deployment_values
        )
        if caller_supplied_authority and any(
            value is None for value in deployment_values
        ):
            raise ChampionAgentEpisodeError(
                "cross-session deployment authority must be complete"
            )
        if snapshot.activation_binding_id is None:
            if caller_supplied_authority:
                raise ChampionAgentEpisodeError(
                    "legacy AgentLoop cannot accept deployment authority on resume"
                )
            authority_identity = identity
            authority_as_of = as_of
            effective_episode_key = episode_key
        else:
            durable_authority = load_deployment_authority(
                path,
                expected_binding_id=snapshot.activation_binding_id,
            )
            if durable_authority.deployment_identity != identity:
                raise ChampionAgentEpisodeError(
                    "deployment identity conflicts with durable activation authority"
                )
            if caller_supplied_authority and (
                training_identity != durable_authority.training_identity
                or deployment_scope != durable_authority.scope
                or activation_binding != durable_authority.binding
            ):
                raise ChampionAgentEpisodeError(
                    "caller deployment authority conflicts with durable record"
                )
            training_identity = durable_authority.training_identity
            deployment_scope = durable_authority.scope
            activation_binding = durable_authority.binding
            if _instant(as_of, "as_of") < _instant(
                activation_binding.activation_at, "activation_at"
            ):
                raise ChampionAgentEpisodeError(
                    "resume as_of predates deployment activation"
                )
            authority_identity = training_identity
            authority_as_of = activation_binding.activation_at
            effective_episode_key = (
                f"{episode_key}|deployment:{activation_binding.binding_id}"
            )
        policy = load_champion_policy(
            registry,
            artifact_store,
            as_of=authority_as_of,
            canonical_strategy_id=canonical_strategy_id,
            environment_id=authority_identity.environment_id,
            protocol_id=authority_identity.protocol_id,
            config_sha256=config_sha256,
            admissible_actions=admissible_actions,
        )
        if activation_binding is not None:
            assert training_identity is not None
            assert deployment_scope is not None
            validate_activation_binding(
                activation_binding,
                scope=deployment_scope,
                policy=policy,
                training_identity=training_identity,
                deployment_identity=identity,
                registry=registry,
                artifact_store=artifact_store,
                canonical_strategy_id=canonical_strategy_id,
                admissible_actions=admissible_actions,
                economic_goal_fingerprint=snapshot.economic_goal_fingerprint,
                risk_fingerprint=snapshot.risk_fingerprint,
            )
        environment = CausalLearningEnvironment.resume(
            identity,
            episode_key=effective_episode_key,
            policy_id=policy.policy_id,
            admissible_actions=admissible_actions,
            checkpoint=checkpoint,
        )
        return cls(
            policy,
            environment,
            agent_loop,
            training_identity,
            deployment_scope,
            activation_binding,
        )

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
        effective_parameters = parameters
        if self.activation_binding is not None:
            if any(key == "activation_binding_id" for key, _ in parameters):
                raise ChampionAgentEpisodeError(
                    "caller cannot override activation_binding_id"
                )
            effective_parameters = tuple(
                sorted(
                    (
                        *parameters,
                        ("activation_binding_id", self.activation_binding.binding_id),
                    )
                )
            )
        return self.environment.act(
            observation,
            action_type=action_type,
            decision_at=decision_at,
            parameters=effective_parameters,
        )


__all__ = ["ChampionAgentEpisode", "ChampionAgentEpisodeError"]
