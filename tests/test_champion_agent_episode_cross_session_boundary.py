from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autosport.champion_agent_episode import (
    ChampionAgentEpisode,
    ChampionAgentEpisodeError,
)
from autosport.learning_environment import EnvironmentIdentity
from autosport.policy_deployment import ActivationBinding, DeploymentScope
from autosport.transparent_bandit_policy import BanditPolicyState


CONFIG_SHA = "c" * 64
GOAL_SHA = "a" * 64
RISK_SHA = "b" * 64
SOURCE_SHA = "e" * 64
PROTOCOL_ID = "protocol-cross-session-boundary-v1"
STRATEGY_ID = "canonical-transparent-bandit"
T1 = "2026-09-20T01:00:00Z"
T2 = "2026-09-20T02:00:00Z"


def _identity(data_id: str, cutoff: str) -> EnvironmentIdentity:
    return EnvironmentIdentity(
        "lawful-provider:paper",
        "config-v1",
        data_id,
        PROTOCOL_ID,
        cutoff,
        7,
    )


def _policy(training: EnvironmentIdentity) -> BanditPolicyState:
    return BanditPolicyState.initial(
        environment_id=training.environment_id,
        protocol_id=training.protocol_id,
        config_sha256=CONFIG_SHA,
        seed=training.seed,
        action_types=frozenset({"WAIT"}),
    )


def _scope() -> DeploymentScope:
    return DeploymentScope(
        canonical_strategy_id=STRATEGY_ID,
        sport_domain="football",
        competition_scope="league:test",
        market_semantics_id="match-odds-v1",
        provider_source_class="lawful-provider",
        feature_schema_id="f" * 64,
        protocol_id=PROTOCOL_ID,
        action_semantics_id="8" * 64,
        reward_definition_id="paper-settlement-learning-reward-v1",
        config_sha256=CONFIG_SHA,
    )


def _binding(
    policy: BanditPolicyState,
    training: EnvironmentIdentity,
    deployment: EnvironmentIdentity,
    scope: DeploymentScope,
) -> ActivationBinding:
    return ActivationBinding(
        policy_id=policy.policy_id,
        policy_artifact_sha256="1" * 64,
        training_environment_id=training.environment_id,
        training_data_id=training.data_id,
        training_dataset_record_sha256="2" * 64,
        training_cutoff_ts=training.cutoff_ts,
        promotion_decision_id="promotion-v1",
        promotion_decision_record_sha256="3" * 64,
        promotion_evidence_id="4" * 64,
        promotion_evidence_record_sha256="5" * 64,
        evaluation_bundle_id="evaluation-v1",
        evaluation_bundle_record_sha256="6" * 64,
        deployment_scope_id=scope.scope_id,
        deployment_environment_id=deployment.environment_id,
        deployment_data_id=deployment.data_id,
        deployment_dataset_record_sha256="7" * 64,
        dataset_lineage_proof_sha256="9" * 64,
        deployment_cutoff_ts=deployment.cutoff_ts,
        snapshot_available_at=T2,
        activation_at=T2,
        admissible_actions=("WAIT",),
        economic_goal_fingerprint=GOAL_SHA,
        risk_fingerprint=RISK_SHA,
    )


def _resolver_patches(policy: BanditPolicyState, scope: DeploymentScope):
    semantic_inputs = object()
    market_store = object()
    runtime_store = object()
    require_inputs = patch(
        "autosport.champion_agent_episode._require_canonical_inputs",
        return_value=(semantic_inputs, market_store, runtime_store),
    )
    load_policy = patch(
        "autosport.champion_agent_episode.load_champion_policy",
        return_value=policy,
    )
    validate = patch(
        "autosport.champion_agent_episode.validate_canonical_activation_binding",
        return_value=SimpleNamespace(deployment_scope=scope),
    )
    return semantic_inputs, market_store, runtime_store, require_inputs, load_policy, validate


def test_cross_session_episode_runs_only_after_boundary_re_resolves_semantics(tmp_path) -> None:
    training = _identity("training-data", T1)
    deployment = _identity("deployment-data", T2)
    policy = _policy(training)
    scope = _scope()
    binding = _binding(policy, training, deployment, scope)
    path = tmp_path / "agent-loop.json"
    (
        semantic_inputs,
        market_store,
        runtime_store,
        require_inputs,
        load_policy,
        validate,
    ) = _resolver_patches(policy, scope)

    with require_inputs, load_policy, validate as canonical_validate:
        session = ChampionAgentEpisode.initialize_pristine(
            path,
            object(),
            object(),
            identity=deployment,
            as_of=T2,
            canonical_strategy_id=STRATEGY_ID,
            config_sha256=CONFIG_SHA,
            episode_key="later-session",
            admissible_actions=frozenset({"WAIT"}),
            loop_id="cross-session-loop",
            economic_goal_fingerprint=GOAL_SHA,
            risk_fingerprint=RISK_SHA,
            source_sha256=SOURCE_SHA,
            at=T2,
            training_identity=training,
            activation_binding=binding,
            semantic_inputs=semantic_inputs,
            market_store=market_store,
            runtime_authority_store=runtime_store,
        )

    assert session.training_identity == training
    assert session.deployment_scope == scope
    assert session.activation_binding == binding
    assert session.agent_loop.snapshot().activation_binding_id == binding.binding_id
    kwargs = canonical_validate.call_args.kwargs
    assert kwargs["require_existing_semantic_binding"] is False
    assert kwargs["training_identity"] == training
    assert kwargs["deployment_identity"] == deployment

    checkpoint = session.environment.checkpoint()
    (
        semantic_inputs,
        market_store,
        runtime_store,
        require_inputs,
        load_policy,
        validate,
    ) = _resolver_patches(policy, scope)
    with require_inputs, load_policy, validate as canonical_validate:
        reopened = ChampionAgentEpisode.resume(
            path,
            object(),
            object(),
            identity=deployment,
            checkpoint=checkpoint,
            as_of=T2,
            canonical_strategy_id=STRATEGY_ID,
            config_sha256=CONFIG_SHA,
            episode_key="later-session",
            admissible_actions=frozenset({"WAIT"}),
            semantic_inputs=semantic_inputs,
            market_store=market_store,
            runtime_authority_store=runtime_store,
        )

    assert reopened.activation_binding == binding
    assert canonical_validate.call_args.kwargs["require_existing_semantic_binding"] is True


def test_caller_scope_cannot_override_canonical_resolver_scope(tmp_path) -> None:
    training = _identity("training-data", T1)
    deployment = _identity("deployment-data", T2)
    policy = _policy(training)
    canonical_scope = _scope()
    caller_scope = DeploymentScope(
        canonical_strategy_id=STRATEGY_ID,
        sport_domain="football",
        competition_scope="caller-invented-league",
        market_semantics_id="caller-market",
        provider_source_class="caller-provider",
        feature_schema_id="caller-features",
        protocol_id=PROTOCOL_ID,
        action_semantics_id="caller-actions",
        reward_definition_id="caller-reward",
        config_sha256=CONFIG_SHA,
    )
    binding = _binding(policy, training, deployment, canonical_scope)
    (
        semantic_inputs,
        market_store,
        runtime_store,
        require_inputs,
        load_policy,
        validate,
    ) = _resolver_patches(policy, canonical_scope)

    with require_inputs, load_policy, validate:
        with pytest.raises(
            ChampionAgentEpisodeError,
            match="caller deployment scope conflicts with canonical semantic authority",
        ):
            ChampionAgentEpisode.initialize_pristine(
                tmp_path / "scope-conflict.json",
                object(),
                object(),
                identity=deployment,
                as_of=T2,
                canonical_strategy_id=STRATEGY_ID,
                config_sha256=CONFIG_SHA,
                episode_key="later-session",
                admissible_actions=frozenset({"WAIT"}),
                loop_id="cross-session-loop",
                economic_goal_fingerprint=GOAL_SHA,
                risk_fingerprint=RISK_SHA,
                source_sha256=SOURCE_SHA,
                at=T2,
                training_identity=training,
                deployment_scope=caller_scope,
                activation_binding=binding,
                semantic_inputs=semantic_inputs,
                market_store=market_store,
                runtime_authority_store=runtime_store,
            )
