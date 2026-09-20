"""Canonical production entrypoint for later-session champion activation.

`ChampionAgentEpisode` owns the durable causal episode/AgentLoop mechanics.  This
module owns no new semantics; it makes the integrated #626 semantic resolver a
mandatory precondition for cross-session activation so caller-authored
`DeploymentScope` objects cannot authorize the production path.
"""

from __future__ import annotations

from pathlib import Path

from .agent_loop import AgentLoopRuntime
from .champion_agent_episode import ChampionAgentEpisode, ChampionAgentEpisodeError
from .champion_policy import load_champion_policy
from .deployment_runtime_authority import DeploymentRuntimeAuthorityStore
from .learning_environment import EnvironmentCheckpoint, EnvironmentIdentity
from .policy_deployment import ActivationBinding, load_deployment_authority
from .policy_deployment_semantic_bridge import (
    CrossSessionSemanticInputs,
    PolicyDeploymentSemanticBridgeError,
    validate_canonical_activation_binding,
)
from .scientific_registry import ScientificRegistry
from .storage import SQLiteMarketStore
from .strategy_model_factory import FactoryArtifactStore


class CanonicalChampionAgentEpisodeError(ChampionAgentEpisodeError):
    """Canonical cross-session champion activation cannot be proven."""


def initialize_canonical_champion_episode(
    path: str | Path,
    registry: ScientificRegistry,
    artifact_store: FactoryArtifactStore,
    *,
    identity: EnvironmentIdentity,
    training_identity: EnvironmentIdentity,
    activation_binding: ActivationBinding,
    semantic_inputs: CrossSessionSemanticInputs,
    market_store: SQLiteMarketStore,
    runtime_authority_store: DeploymentRuntimeAuthorityStore,
    canonical_strategy_id: str,
    config_sha256: str,
    episode_key: str,
    admissible_actions: frozenset[str],
    loop_id: str,
    economic_goal_fingerprint: str,
    risk_fingerprint: str,
    source_sha256: str,
    at: str,
) -> ChampionAgentEpisode:
    """Create a later-session episode only after canonical semantics re-resolve.

    The champion policy is first reconstructed against its immutable training
    environment.  The exact training/deployment semantic authorities are then
    independently resolved from canonical stores and pinned.  Only the derived
    scope is passed to the existing AgentLoop activation seam.
    """

    if not isinstance(identity, EnvironmentIdentity):
        raise TypeError("identity must be EnvironmentIdentity")
    if not isinstance(training_identity, EnvironmentIdentity):
        raise TypeError("training_identity must be EnvironmentIdentity")
    if not isinstance(activation_binding, ActivationBinding):
        raise TypeError("activation_binding must be ActivationBinding")

    try:
        policy = load_champion_policy(
            registry,
            artifact_store,
            as_of=activation_binding.activation_at,
            canonical_strategy_id=canonical_strategy_id,
            environment_id=training_identity.environment_id,
            protocol_id=training_identity.protocol_id,
            config_sha256=config_sha256,
            admissible_actions=admissible_actions,
        )
        resolved = validate_canonical_activation_binding(
            activation_binding,
            semantic_inputs=semantic_inputs,
            market_store=market_store,
            runtime_authority_store=runtime_authority_store,
            loop_path=path,
            require_existing_semantic_binding=False,
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
    except (PolicyDeploymentSemanticBridgeError, OSError, RuntimeError, ValueError) as exc:
        raise CanonicalChampionAgentEpisodeError(
            "canonical cross-session deployment authority is not proven"
        ) from exc

    return ChampionAgentEpisode.initialize_pristine(
        path,
        registry,
        artifact_store,
        identity=identity,
        as_of=activation_binding.activation_at,
        canonical_strategy_id=canonical_strategy_id,
        config_sha256=config_sha256,
        episode_key=episode_key,
        admissible_actions=admissible_actions,
        loop_id=loop_id,
        economic_goal_fingerprint=economic_goal_fingerprint,
        risk_fingerprint=risk_fingerprint,
        source_sha256=source_sha256,
        at=at,
        training_identity=training_identity,
        deployment_scope=resolved.deployment_scope,
        activation_binding=activation_binding,
    )


def resume_canonical_champion_episode(
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
    semantic_inputs: CrossSessionSemanticInputs,
    market_store: SQLiteMarketStore,
    runtime_authority_store: DeploymentRuntimeAuthorityStore,
) -> ChampionAgentEpisode:
    """Resume only if the exact semantic authorities still match the durable pin."""

    if not isinstance(identity, EnvironmentIdentity):
        raise TypeError("identity must be EnvironmentIdentity")
    if not isinstance(checkpoint, EnvironmentCheckpoint):
        raise TypeError("checkpoint must be EnvironmentCheckpoint")

    try:
        snapshot = AgentLoopRuntime(path).snapshot()
        if snapshot.activation_binding_id is None:
            raise CanonicalChampionAgentEpisodeError(
                "canonical cross-session resume requires an activation binding"
            )
        durable = load_deployment_authority(
            path,
            expected_binding_id=snapshot.activation_binding_id,
        )
        if durable.deployment_identity != identity:
            raise CanonicalChampionAgentEpisodeError(
                "deployment identity conflicts with durable activation authority"
            )
        policy = load_champion_policy(
            registry,
            artifact_store,
            as_of=durable.binding.activation_at,
            canonical_strategy_id=canonical_strategy_id,
            environment_id=durable.training_identity.environment_id,
            protocol_id=durable.training_identity.protocol_id,
            config_sha256=config_sha256,
            admissible_actions=admissible_actions,
        )
        resolved = validate_canonical_activation_binding(
            durable.binding,
            semantic_inputs=semantic_inputs,
            market_store=market_store,
            runtime_authority_store=runtime_authority_store,
            loop_path=path,
            require_existing_semantic_binding=True,
            policy=policy,
            training_identity=durable.training_identity,
            deployment_identity=identity,
            registry=registry,
            artifact_store=artifact_store,
            canonical_strategy_id=canonical_strategy_id,
            admissible_actions=admissible_actions,
            economic_goal_fingerprint=snapshot.economic_goal_fingerprint,
            risk_fingerprint=snapshot.risk_fingerprint,
        )
        if resolved.deployment_scope != durable.scope:
            raise CanonicalChampionAgentEpisodeError(
                "durable deployment scope differs from canonical re-resolution"
            )
    except CanonicalChampionAgentEpisodeError:
        raise
    except (PolicyDeploymentSemanticBridgeError, OSError, RuntimeError, ValueError) as exc:
        raise CanonicalChampionAgentEpisodeError(
            "canonical cross-session deployment authority is not proven on resume"
        ) from exc

    return ChampionAgentEpisode.resume(
        path,
        registry,
        artifact_store,
        identity=identity,
        checkpoint=checkpoint,
        as_of=as_of,
        canonical_strategy_id=canonical_strategy_id,
        config_sha256=config_sha256,
        episode_key=episode_key,
        admissible_actions=admissible_actions,
        training_identity=durable.training_identity,
        deployment_scope=durable.scope,
        activation_binding=durable.binding,
    )


__all__ = [
    "CanonicalChampionAgentEpisodeError",
    "initialize_canonical_champion_episode",
    "resume_canonical_champion_episode",
]
