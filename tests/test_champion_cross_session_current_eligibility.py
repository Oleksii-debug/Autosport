from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autosport.champion_agent_episode import (
    ChampionAgentEpisode,
    ChampionAgentEpisodeError,
)
from autosport.champion_eligibility import (
    ChampionEligibilityDecision,
    ChampionEligibilityStatus,
)
from autosport.champion_policy import (
    ChampionPolicyError,
    load_champion_policy,
    persist_policy_state,
)
from autosport.learning_environment import CausalLearningEnvironment, EnvironmentIdentity
from autosport.policy_deployment import ActivationBinding, DeploymentScope
from autosport.scientific_registry import ScientificRegistry
from autosport.strategy_model_factory import FactoryArtifactStore
from tests.test_champion_agent_episode import (
    CONFIG_SHA256,
    GOAL_SHA256,
    MODEL_ID,
    PROTOCOL_ID,
    RISK_SHA256,
    SOURCE_SHA256,
    STRATEGY_ID,
    T0,
    T1,
    T2,
    T3,
    T4,
    _learned_champion,
    _patched_authority,
)


T5 = "2026-09-19T13:30:00Z"


class _StopAfterPolicyLoad(RuntimeError):
    pass


def _eligibility_decision(identity, champion):
    return ChampionEligibilityDecision(
        status=ChampionEligibilityStatus.ELIGIBLE,
        canonical_strategy_id=STRATEGY_ID,
        strategy_version_id=champion.policy_id,
        model_version_id=MODEL_ID,
        environment_sha256=identity.environment_id,
        protocol_id=PROTOCOL_ID,
        config_sha256=CONFIG_SHA256,
        sport="football",
        league="league-a",
        regime="paper",
        finding_ids=("finding-current",),
        finding_record_sha256s=("f" * 64,),
        window_start=T0,
        window_end=T1,
        evaluated_at=T2,
        valid_until=T5,
        minimum_samples=1,
        minimum_effective_sample_size=1,
        effective_sample_size=1,
        degraded_streak=0,
        recovery_streak=2,
        admissible_actions=("WAIT",),
    )


def _cross_session_fixture(tmp_path):
    training = EnvironmentIdentity(
        "lawful-provider:paper",
        "champion-agent-config-v1",
        "paper-dataset-training-v1",
        PROTOCOL_ID,
        T1,
        17,
    )
    deployment = EnvironmentIdentity(
        "lawful-provider:paper",
        "champion-agent-config-v1",
        "paper-dataset-later-v2",
        PROTOCOL_ID,
        T3,
        17,
    )
    _, champion = _learned_champion(training)
    registry = ScientificRegistry.initialize_pristine(tmp_path / "registry.json")
    store = FactoryArtifactStore(tmp_path / "artifacts")
    persist_policy_state(store, champion)
    scope = DeploymentScope(
        canonical_strategy_id=STRATEGY_ID,
        sport_domain="football",
        competition_scope="league-a",
        market_semantics_id="match-odds-v1",
        provider_source_class="lawful-provider",
        feature_schema_id="features-v1",
        protocol_id=PROTOCOL_ID,
        action_semantics_id="action-v1",
        reward_definition_id="reward-v1",
        config_sha256=CONFIG_SHA256,
    )
    binding = ActivationBinding(
        policy_id=champion.policy_id,
        policy_artifact_sha256=store.sha256(
            "transparent-bandit-policy", champion.policy_id
        ),
        training_environment_id=training.environment_id,
        training_data_id=training.data_id,
        training_dataset_record_sha256="1" * 64,
        training_cutoff_ts=training.cutoff_ts,
        promotion_decision_id="promotion-champion-agent-v1",
        promotion_decision_record_sha256="6" * 64,
        promotion_evidence_id="5" * 64,
        promotion_evidence_record_sha256="4" * 64,
        evaluation_bundle_id="evaluation-champion-agent-v1",
        evaluation_bundle_record_sha256="7" * 64,
        deployment_scope_id=scope.scope_id,
        deployment_environment_id=deployment.environment_id,
        deployment_data_id=deployment.data_id,
        deployment_dataset_record_sha256="2" * 64,
        dataset_lineage_proof_sha256="9" * 64,
        deployment_cutoff_ts=deployment.cutoff_ts,
        snapshot_available_at=T3,
        activation_at=T4,
        admissible_actions=("WAIT",),
        economic_goal_fingerprint=GOAL_SHA256,
        risk_fingerprint=RISK_SHA256,
    )
    return training, deployment, champion, registry, store, scope, binding


def test_policy_loader_uses_current_eligibility_cutoff_without_rewriting_promotion_cutoff(
    tmp_path,
):
    identity = EnvironmentIdentity(
        "lawful-provider:paper",
        "champion-agent-config-v1",
        "paper-evidence-v1",
        PROTOCOL_ID,
        T4,
        17,
    )
    _, champion = _learned_champion(identity)
    registry = ScientificRegistry.initialize_pristine(tmp_path / "registry.json")
    store = FactoryArtifactStore(tmp_path / "artifacts")
    persist_policy_state(store, champion)
    decision = _eligibility_decision(identity, champion)

    authority = _patched_authority(champion)
    with authority[0], authority[1], authority[2], patch(
        "autosport.champion_policy.validate_activation_eligibility"
    ) as validator:
        loaded = load_champion_policy(
            registry,
            store,
            as_of=T2,
            canonical_strategy_id=STRATEGY_ID,
            environment_id=identity.environment_id,
            protocol_id=PROTOCOL_ID,
            config_sha256=CONFIG_SHA256,
            admissible_actions=frozenset({"WAIT"}),
            eligibility_decision=decision,
            eligibility_as_of=T5,
        )

    assert loaded.policy_id == champion.policy_id
    assert validator.call_count == 1
    assert validator.call_args.kwargs["as_of"] == T5
    assert validator.call_args.kwargs["expected_strategy_version_id"] == champion.policy_id
    assert validator.call_args.kwargs["expected_model_version_id"] == MODEL_ID


def test_policy_loader_rejects_eligibility_cutoff_without_decision(tmp_path):
    identity = EnvironmentIdentity(
        "lawful-provider:paper",
        "champion-agent-config-v1",
        "paper-evidence-v1",
        PROTOCOL_ID,
        T4,
        17,
    )
    _, champion = _learned_champion(identity)
    registry = ScientificRegistry.initialize_pristine(tmp_path / "registry.json")
    store = FactoryArtifactStore(tmp_path / "artifacts")
    persist_policy_state(store, champion)
    authority = _patched_authority(champion)

    with authority[0], authority[1], authority[2], pytest.raises(
        ChampionPolicyError,
        match="eligibility_as_of requires eligibility_decision",
    ):
        load_champion_policy(
            registry,
            store,
            as_of=T2,
            canonical_strategy_id=STRATEGY_ID,
            environment_id=identity.environment_id,
            protocol_id=PROTOCOL_ID,
            config_sha256=CONFIG_SHA256,
            admissible_actions=frozenset({"WAIT"}),
            eligibility_as_of=T5,
        )


def test_cross_session_start_fails_closed_without_current_eligibility(tmp_path):
    (
        training,
        deployment,
        _champion,
        registry,
        store,
        scope,
        binding,
    ) = _cross_session_fixture(tmp_path)

    with patch(
        "autosport.champion_agent_episode._require_canonical_inputs",
        return_value=(object(), object(), object()),
    ), patch("autosport.champion_agent_episode.load_champion_policy") as loader:
        with pytest.raises(
            ChampionAgentEpisodeError,
            match="requires current champion eligibility evidence",
        ):
            ChampionAgentEpisode.initialize_pristine(
                tmp_path / "agent-loop.json",
                registry,
                store,
                identity=deployment,
                as_of=T4,
                canonical_strategy_id=STRATEGY_ID,
                config_sha256=CONFIG_SHA256,
                episode_key="cross-session",
                admissible_actions=frozenset({"WAIT"}),
                loop_id="cross-session-loop",
                economic_goal_fingerprint=GOAL_SHA256,
                risk_fingerprint=RISK_SHA256,
                source_sha256=SOURCE_SHA256,
                at=T4,
                training_identity=training,
                deployment_scope=scope,
                activation_binding=binding,
                semantic_inputs=object(),
                market_store=object(),
                runtime_authority_store=object(),
            )

    loader.assert_not_called()


def test_cross_session_start_forwards_current_cutoff_to_policy_gate(tmp_path):
    (
        training,
        deployment,
        champion,
        registry,
        store,
        scope,
        binding,
    ) = _cross_session_fixture(tmp_path)
    decision = _eligibility_decision(training, champion)

    with patch(
        "autosport.champion_agent_episode._require_canonical_inputs",
        return_value=(object(), object(), object()),
    ), patch(
        "autosport.champion_agent_episode.load_champion_policy",
        side_effect=_StopAfterPolicyLoad,
    ) as loader:
        with pytest.raises(_StopAfterPolicyLoad):
            ChampionAgentEpisode.initialize_pristine(
                tmp_path / "agent-loop.json",
                registry,
                store,
                identity=deployment,
                as_of=T4,
                canonical_strategy_id=STRATEGY_ID,
                config_sha256=CONFIG_SHA256,
                episode_key="cross-session",
                admissible_actions=frozenset({"WAIT"}),
                loop_id="cross-session-loop",
                economic_goal_fingerprint=GOAL_SHA256,
                risk_fingerprint=RISK_SHA256,
                source_sha256=SOURCE_SHA256,
                at=T4,
                training_identity=training,
                deployment_scope=scope,
                activation_binding=binding,
                semantic_inputs=object(),
                market_store=object(),
                runtime_authority_store=object(),
                eligibility_decision=decision,
            )

    assert loader.call_args.kwargs["as_of"] == binding.activation_at
    assert loader.call_args.kwargs["eligibility_as_of"] == T4
    assert loader.call_args.kwargs["eligibility_decision"] is decision


def test_cross_session_resume_requires_fresh_eligibility_and_uses_resume_cutoff(tmp_path):
    (
        training,
        deployment,
        champion,
        registry,
        store,
        scope,
        binding,
    ) = _cross_session_fixture(tmp_path)
    environment = CausalLearningEnvironment(
        deployment,
        episode_key=f"cross-session|deployment:{binding.binding_id}",
        policy_id=champion.policy_id,
        admissible_actions=frozenset({"WAIT"}),
    )
    checkpoint = environment.checkpoint()
    snapshot = SimpleNamespace(
        environment_checkpoint_id=checkpoint.checkpoint_id,
        activation_binding_id=binding.binding_id,
        economic_goal_fingerprint=GOAL_SHA256,
        risk_fingerprint=RISK_SHA256,
    )
    fake_loop = SimpleNamespace(snapshot=lambda: snapshot)

    common_patches = (
        patch("autosport.champion_agent_episode.AgentLoopRuntime", return_value=fake_loop),
        patch(
            "autosport.champion_agent_episode._require_canonical_inputs",
            return_value=(object(), object(), object()),
        ),
    )
    with common_patches[0], common_patches[1], patch(
        "autosport.champion_agent_episode.load_deployment_authority"
    ) as durable_loader, patch(
        "autosport.champion_agent_episode.load_champion_policy"
    ) as policy_loader:
        with pytest.raises(
            ChampionAgentEpisodeError,
            match="resume requires current champion eligibility evidence",
        ):
            ChampionAgentEpisode.resume(
                tmp_path / "agent-loop.json",
                registry,
                store,
                identity=deployment,
                checkpoint=checkpoint,
                as_of=T5,
                canonical_strategy_id=STRATEGY_ID,
                config_sha256=CONFIG_SHA256,
                episode_key="cross-session",
                admissible_actions=frozenset({"WAIT"}),
                semantic_inputs=object(),
                market_store=object(),
                runtime_authority_store=object(),
            )
    durable_loader.assert_not_called()
    policy_loader.assert_not_called()

    decision = _eligibility_decision(training, champion)
    durable = SimpleNamespace(
        deployment_identity=deployment,
        training_identity=training,
        scope=scope,
        binding=binding,
    )
    with patch(
        "autosport.champion_agent_episode.AgentLoopRuntime", return_value=fake_loop
    ), patch(
        "autosport.champion_agent_episode._require_canonical_inputs",
        return_value=(object(), object(), object()),
    ), patch(
        "autosport.champion_agent_episode.load_deployment_authority",
        return_value=durable,
    ), patch(
        "autosport.champion_agent_episode.load_champion_policy",
        side_effect=_StopAfterPolicyLoad,
    ) as policy_loader:
        with pytest.raises(_StopAfterPolicyLoad):
            ChampionAgentEpisode.resume(
                tmp_path / "agent-loop.json",
                registry,
                store,
                identity=deployment,
                checkpoint=checkpoint,
                as_of=T5,
                canonical_strategy_id=STRATEGY_ID,
                config_sha256=CONFIG_SHA256,
                episode_key="cross-session",
                admissible_actions=frozenset({"WAIT"}),
                semantic_inputs=object(),
                market_store=object(),
                runtime_authority_store=object(),
                eligibility_decision=decision,
            )

    assert policy_loader.call_args.kwargs["as_of"] == binding.activation_at
    assert policy_loader.call_args.kwargs["eligibility_as_of"] == T5
    assert policy_loader.call_args.kwargs["eligibility_decision"] is decision
