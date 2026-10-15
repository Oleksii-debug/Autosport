"""Canonical production entrypoint for later-session champion activation.

`ChampionAgentEpisode` owns the durable causal episode/AgentLoop mechanics and now
owns the actual canonical semantic re-resolution boundary.  This module is the
supported product facade: it supplies the canonical stores/handles required by that
boundary and never mints a caller-transferable capability.
"""

from __future__ import annotations

from pathlib import Path

from .champion_agent_episode import ChampionAgentEpisode, ChampionAgentEpisodeError
from .champion_eligibility import ChampionEligibilityDecision
from .deployment_runtime_authority import DeploymentRuntimeAuthorityStore
from .learning_environment import EnvironmentCheckpoint, EnvironmentIdentity
from .policy_deployment import ActivationBinding
from .policy_deployment_semantic_bridge import CrossSessionSemanticInputs
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
    eligibility_decision: ChampionEligibilityDecision,
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
    """Create a later-session episode through the canonical semantic boundary."""

    if not isinstance(identity, EnvironmentIdentity):
        raise TypeError("identity must be EnvironmentIdentity")
    if not isinstance(training_identity, EnvironmentIdentity):
        raise TypeError("training_identity must be EnvironmentIdentity")
    if not isinstance(activation_binding, ActivationBinding):
        raise TypeError("activation_binding must be ActivationBinding")

    try:
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
            activation_binding=activation_binding,
            eligibility_decision=eligibility_decision,
            semantic_inputs=semantic_inputs,
            market_store=market_store,
            runtime_authority_store=runtime_authority_store,
        )
    except ChampionAgentEpisodeError as exc:
        raise CanonicalChampionAgentEpisodeError(
            "canonical cross-session deployment authority is not proven"
        ) from exc


def resume_canonical_champion_episode(
    path: str | Path,
    registry: ScientificRegistry,
    artifact_store: FactoryArtifactStore,
    *,
    identity: EnvironmentIdentity,
    checkpoint: EnvironmentCheckpoint,
    eligibility_decision: ChampionEligibilityDecision,
    as_of: str,
    canonical_strategy_id: str,
    config_sha256: str,
    episode_key: str,
    admissible_actions: frozenset[str],
    semantic_inputs: CrossSessionSemanticInputs,
    market_store: SQLiteMarketStore,
    runtime_authority_store: DeploymentRuntimeAuthorityStore,
) -> ChampionAgentEpisode:
    """Resume only through fresh canonical semantic re-resolution."""

    if not isinstance(identity, EnvironmentIdentity):
        raise TypeError("identity must be EnvironmentIdentity")
    if not isinstance(checkpoint, EnvironmentCheckpoint):
        raise TypeError("checkpoint must be EnvironmentCheckpoint")

    try:
        return ChampionAgentEpisode.resume(
            path,
            registry,
            artifact_store,
            identity=identity,
            checkpoint=checkpoint,
            eligibility_decision=eligibility_decision,
            as_of=as_of,
            canonical_strategy_id=canonical_strategy_id,
            config_sha256=config_sha256,
            episode_key=episode_key,
            admissible_actions=admissible_actions,
            semantic_inputs=semantic_inputs,
            market_store=market_store,
            runtime_authority_store=runtime_authority_store,
        )
    except ChampionAgentEpisodeError as exc:
        raise CanonicalChampionAgentEpisodeError(
            "canonical cross-session deployment authority is not proven on resume"
        ) from exc


__all__ = [
    "CanonicalChampionAgentEpisodeError",
    "initialize_canonical_champion_episode",
    "resume_canonical_champion_episode",
]
