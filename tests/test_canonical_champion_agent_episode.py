from types import SimpleNamespace
from unittest.mock import patch

import autosport.champion_agent_episode as champion_module
from autosport.canonical_champion_agent_episode import (
    initialize_canonical_champion_episode,
    resume_canonical_champion_episode,
)
from autosport.learning_environment import EnvironmentIdentity
from autosport.policy_deployment import ActivationBinding, DeploymentScope
from autosport.policy_deployment_semantic_bridge import (
    CrossSessionSemanticInputs,
    SemanticResolutionInput,
)


T1 = "2026-09-20T01:00:00Z"
T2 = "2026-09-20T02:00:00Z"
CONFIG_SHA = "c" * 64
GOAL_SHA = "a" * 64
RISK_SHA = "b" * 64
SOURCE_SHA = "e" * 64


def _identity(data_id: str, cutoff: str) -> EnvironmentIdentity:
    return EnvironmentIdentity(
        "lawful-provider:paper",
        "config-v1",
        data_id,
        "protocol-v1",
        cutoff,
        7,
    )


def _scope() -> DeploymentScope:
    return DeploymentScope(
        canonical_strategy_id="strategy-v1",
        sport_domain="football",
        competition_scope="league:test",
        market_semantics_id="match-odds-v1",
        provider_source_class="lawful-provider",
        feature_schema_id="f" * 64,
        protocol_id="protocol-v1",
        action_semantics_id="8" * 64,
        reward_definition_id="paper-settlement-learning-reward-v1",
        config_sha256=CONFIG_SHA,
    )


def _binding(training: EnvironmentIdentity, deployment: EnvironmentIdentity) -> ActivationBinding:
    return ActivationBinding(
        policy_id="0" * 64,
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
        deployment_scope_id=_scope().scope_id,
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


def _semantic_inputs() -> CrossSessionSemanticInputs:
    return CrossSessionSemanticInputs(
        SemanticResolutionInput("training-event", "features-v1", "1" * 64),
        SemanticResolutionInput("deployment-event", "features-v1", "2" * 64),
    )


def test_general_module_exposes_no_caller_mintable_cross_session_capability() -> None:
    assert not hasattr(champion_module, "_mint_canonical_cross_session_authority")
    assert not hasattr(champion_module, "_CanonicalCrossSessionAuthority")
    assert not hasattr(champion_module, "_CANONICAL_CROSS_SESSION_MARKER")


def test_initialize_routes_resolver_inputs_into_actual_agent_boundary(tmp_path) -> None:
    training = _identity("training-data", T1)
    deployment = _identity("deployment-data", T2)
    binding = _binding(training, deployment)
    semantic_inputs = _semantic_inputs()
    market_store = object()
    runtime_store = object()
    expected = object()

    with patch(
        "autosport.canonical_champion_agent_episode.ChampionAgentEpisode.initialize_pristine",
        return_value=expected,
    ) as initialize:
        actual = initialize_canonical_champion_episode(
            tmp_path / "agent-loop.json",
            object(),
            object(),
            identity=deployment,
            training_identity=training,
            activation_binding=binding,
            semantic_inputs=semantic_inputs,
            market_store=market_store,
            runtime_authority_store=runtime_store,
            canonical_strategy_id="strategy-v1",
            config_sha256=CONFIG_SHA,
            episode_key="episode-v1",
            admissible_actions=frozenset({"WAIT"}),
            loop_id="loop-v1",
            economic_goal_fingerprint=GOAL_SHA,
            risk_fingerprint=RISK_SHA,
            source_sha256=SOURCE_SHA,
            at=T2,
        )

    assert actual is expected
    initialize.assert_called_once()
    kwargs = initialize.call_args.kwargs
    assert kwargs["training_identity"] == training
    assert kwargs["activation_binding"] == binding
    assert kwargs["semantic_inputs"] == semantic_inputs
    assert kwargs["market_store"] is market_store
    assert kwargs["runtime_authority_store"] is runtime_store
    assert "deployment_scope" not in kwargs
    assert "_canonical_cross_session_authority" not in kwargs


def test_resume_routes_resolver_inputs_into_actual_agent_boundary(tmp_path) -> None:
    deployment = _identity("deployment-data", T2)
    checkpoint = SimpleNamespace()
    semantic_inputs = _semantic_inputs()
    market_store = object()
    runtime_store = object()
    expected = object()

    with patch(
        "autosport.canonical_champion_agent_episode.EnvironmentCheckpoint",
        object,
    ), patch(
        "autosport.canonical_champion_agent_episode.ChampionAgentEpisode.resume",
        return_value=expected,
    ) as resume:
        actual = resume_canonical_champion_episode(
            tmp_path / "agent-loop.json",
            object(),
            object(),
            identity=deployment,
            checkpoint=checkpoint,
            as_of=T2,
            canonical_strategy_id="strategy-v1",
            config_sha256=CONFIG_SHA,
            episode_key="episode-v1",
            admissible_actions=frozenset({"WAIT"}),
            semantic_inputs=semantic_inputs,
            market_store=market_store,
            runtime_authority_store=runtime_store,
        )

    assert actual is expected
    resume.assert_called_once()
    kwargs = resume.call_args.kwargs
    assert kwargs["semantic_inputs"] == semantic_inputs
    assert kwargs["market_store"] is market_store
    assert kwargs["runtime_authority_store"] is runtime_store
    assert "_canonical_cross_session_authority" not in kwargs
