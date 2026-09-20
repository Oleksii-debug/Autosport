from types import SimpleNamespace
from unittest.mock import patch

from autosport.canonical_champion_agent_episode import (
    initialize_canonical_champion_episode,
    resume_canonical_champion_episode,
)
from autosport.learning_environment import EnvironmentCheckpoint, EnvironmentIdentity
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


def test_initialize_routes_only_canonical_derived_scope_to_agent_loop(tmp_path) -> None:
    training = _identity("training-data", T1)
    deployment = _identity("deployment-data", T2)
    binding = _binding(training, deployment)
    derived_scope = _scope()
    policy = object()
    expected = object()

    with patch(
        "autosport.canonical_champion_agent_episode.load_champion_policy",
        return_value=policy,
    ), patch(
        "autosport.canonical_champion_agent_episode.validate_canonical_activation_binding",
        return_value=SimpleNamespace(deployment_scope=derived_scope),
    ) as canonical_validate, patch(
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
            semantic_inputs=_semantic_inputs(),
            market_store=object(),
            runtime_authority_store=object(),
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
    canonical_validate.assert_called_once()
    assert canonical_validate.call_args.kwargs["policy"] is policy
    assert canonical_validate.call_args.kwargs["require_existing_semantic_binding"] is False
    initialize.assert_called_once()
    assert initialize.call_args.kwargs["deployment_scope"] == derived_scope
    assert initialize.call_args.kwargs["activation_binding"] == binding
    assert initialize.call_args.kwargs["training_identity"] == training
    assert initialize.call_args.kwargs["as_of"] == binding.activation_at


def test_resume_reuses_only_durable_binding_and_rechecks_exact_semantics(tmp_path) -> None:
    training = _identity("training-data", T1)
    deployment = _identity("deployment-data", T2)
    binding = _binding(training, deployment)
    scope = _scope()
    checkpoint = SimpleNamespace()
    snapshot = SimpleNamespace(
        activation_binding_id=binding.binding_id,
        economic_goal_fingerprint=GOAL_SHA,
        risk_fingerprint=RISK_SHA,
    )
    durable = SimpleNamespace(
        binding=binding,
        training_identity=training,
        deployment_identity=deployment,
        scope=scope,
    )
    expected = object()

    with patch(
        "autosport.canonical_champion_agent_episode.EnvironmentCheckpoint",
        object,
    ), patch(
        "autosport.canonical_champion_agent_episode.AgentLoopRuntime"
    ) as runtime, patch(
        "autosport.canonical_champion_agent_episode.load_deployment_authority",
        return_value=durable,
    ), patch(
        "autosport.canonical_champion_agent_episode.load_champion_policy",
        return_value=object(),
    ), patch(
        "autosport.canonical_champion_agent_episode.validate_canonical_activation_binding",
        return_value=SimpleNamespace(deployment_scope=scope),
    ) as canonical_validate, patch(
        "autosport.canonical_champion_agent_episode.ChampionAgentEpisode.resume",
        return_value=expected,
    ) as resume:
        runtime.return_value.snapshot.return_value = snapshot
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
            semantic_inputs=_semantic_inputs(),
            market_store=object(),
            runtime_authority_store=object(),
        )

    assert actual is expected
    canonical_validate.assert_called_once()
    assert canonical_validate.call_args.args[0] == binding
    assert canonical_validate.call_args.kwargs["require_existing_semantic_binding"] is True
    resume.assert_called_once()
    assert resume.call_args.kwargs["training_identity"] == training
    assert resume.call_args.kwargs["deployment_scope"] == scope
    assert resume.call_args.kwargs["activation_binding"] == binding
