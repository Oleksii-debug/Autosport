import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import autosport.agent_loop as agent_loop_module
import autosport.champion_agent_episode as champion_episode_module
from autosport.agent_loop import (
    AgentLoopError,
    AgentLoopPhase,
    AgentLoopRuntime,
    AttributionComponent,
    AttributionEvidenceGrade,
    AttributionFinding,
    AttributionStatus,
    ConflictingAgentLoopEvidenceError,
    ExternalEffectState,
    OutcomeAttribution,
)
from autosport.champion_agent_episode import (
    ChampionAgentEpisode,
    ChampionAgentEpisodeError,
)
from autosport.deployment_runtime_authority import DeploymentRuntimeAuthorityStore
from autosport.learning_environment import (
    EnvironmentIdentity,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
)
from autosport.paper_abstention_learning import PaperAbstentionLearningRuntime
from autosport.policy_deployment import ActivationBinding, DeploymentScope
from autosport.policy_deployment_semantic_bridge import (
    CrossSessionSemanticInputs,
    SemanticResolutionInput,
)
from autosport.storage import SQLiteMarketStore
from autosport.transparent_bandit_policy import BanditPolicyState


CONFIG_SHA = "c" * 64
GOAL_SHA = "a" * 64
RISK_SHA = "b" * 64
SOURCE_SHA = "e" * 64
PROTOCOL_ID = "protocol-cross-session-boundary-v1"
STRATEGY_ID = "canonical-transparent-bandit"
T1 = "2026-09-20T01:00:00Z"
T2 = "2026-09-20T02:00:00Z"
T3 = "2026-09-20T03:00:00Z"
T4 = "2026-09-20T04:00:00Z"
T5 = "2026-09-20T05:00:00Z"
ATTRIBUTION_EVIDENCE_SHA = "d" * 64


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


def _advance_to_decision(session: ChampionAgentEpisode, observation: Observation) -> None:
    session.agent_loop.begin_observation(
        observation,
        environment_identity=session.environment.identity,
        at=T3,
    )
    for phase in (
        AgentLoopPhase.OBSERVE,
        AgentLoopPhase.ASSESS,
        AgentLoopPhase.PLAN,
        AgentLoopPhase.DECIDE,
    ):
        session.agent_loop.advance(expected=phase, at=T3)


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


def test_resume_reloads_policy_at_original_activation_boundary_not_resume_time(tmp_path) -> None:
    training = _identity("training-data", T1)
    deployment = _identity("deployment-data", T2)
    policy = _policy(training)
    scope = _scope()
    binding = _binding(policy, training, deployment, scope)
    path = tmp_path / "activation-time-loop.json"
    (
        semantic_inputs,
        market_store,
        runtime_store,
        require_inputs,
        load_policy,
        validate,
    ) = _resolver_patches(policy, scope)

    with require_inputs, load_policy as initial_load, validate:
        session = ChampionAgentEpisode.initialize_pristine(
            path,
            object(),
            object(),
            identity=deployment,
            as_of=T2,
            canonical_strategy_id=STRATEGY_ID,
            config_sha256=CONFIG_SHA,
            episode_key="activation-time",
            admissible_actions=frozenset({"WAIT"}),
            loop_id="activation-time-loop",
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
    assert initial_load.call_args.kwargs["as_of"] == T2

    checkpoint = session.environment.checkpoint()
    (
        semantic_inputs,
        market_store,
        runtime_store,
        require_inputs,
        load_policy,
        validate,
    ) = _resolver_patches(policy, scope)
    with require_inputs, load_policy as resumed_load, validate:
        reopened = ChampionAgentEpisode.resume(
            path,
            object(),
            object(),
            identity=deployment,
            checkpoint=checkpoint,
            as_of=T3,
            canonical_strategy_id=STRATEGY_ID,
            config_sha256=CONFIG_SHA,
            episode_key="activation-time",
            admissible_actions=frozenset({"WAIT"}),
            semantic_inputs=semantic_inputs,
            market_store=market_store,
            runtime_authority_store=runtime_store,
        )

    assert reopened.activation_binding == binding
    assert resumed_load.call_args.kwargs["as_of"] == binding.activation_at
    assert resumed_load.call_args.kwargs["as_of"] != T3


def test_decision_freezes_durable_activation_binding_identity(tmp_path) -> None:
    training = _identity("training-data", T1)
    deployment = _identity("deployment-data", T2)
    policy = _policy(training)
    scope = _scope()
    binding = _binding(policy, training, deployment, scope)
    path = tmp_path / "decision-attribution-loop.json"
    (
        semantic_inputs,
        market_store,
        runtime_store,
        require_inputs,
        load_policy,
        validate,
    ) = _resolver_patches(policy, scope)

    with require_inputs, load_policy, validate:
        session = ChampionAgentEpisode.initialize_pristine(
            path,
            object(),
            object(),
            identity=deployment,
            as_of=T2,
            canonical_strategy_id=STRATEGY_ID,
            config_sha256=CONFIG_SHA,
            episode_key="decision-attribution",
            admissible_actions=frozenset({"WAIT"}),
            loop_id="decision-attribution-loop",
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

    observation = Observation(
        environment_id=deployment.environment_id,
        observed_at=T2,
        available_at=T2,
        evidence=(("market_state", "causal-snapshot"),),
    )
    _advance_to_decision(session, observation)
    action = session.decide(
        observation,
        decision_at=T3,
        parameters=(("operator_note", "bounded"),),
    )

    assert ("activation_binding_id", binding.binding_id) in action.parameters
    assert ("operator_note", "bounded") in action.parameters

    with pytest.raises(
        ChampionAgentEpisodeError,
        match="caller cannot override activation_binding_id",
    ):
        session.decide(
            observation,
            decision_at=T3,
            parameters=(("activation_binding_id", "0" * 64),),
        )

    missing_binding = replace(
        action,
        parameters=(("operator_note", "bounded"),),
    )
    with pytest.raises(
        ConflictingAgentLoopEvidenceError,
        match="action activation binding conflicts with AgentLoop deployment identity",
    ):
        session.agent_loop.commit_action(
            missing_binding,
            episode=session.environment.episode,
            observation=observation,
            effect_state=ExternalEffectState.PAPER_ONLY,
            at=T3,
        )

    relabeled = replace(
        action,
        parameters=tuple(
            ("activation_binding_id", "0" * 64)
            if key == "activation_binding_id"
            else (key, value)
            for key, value in action.parameters
        ),
    )
    with pytest.raises(
        ConflictingAgentLoopEvidenceError,
        match="action activation binding conflicts with AgentLoop deployment identity",
    ):
        session.agent_loop.commit_action(
            relabeled,
            episode=session.environment.episode,
            observation=observation,
            effect_state=ExternalEffectState.PAPER_ONLY,
            at=T3,
        )
    assert session.agent_loop.snapshot().phase is AgentLoopPhase.ACT_OR_ABSTAIN

    commit = session.agent_loop.commit_action(
        action,
        episode=session.environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.PAPER_ONLY,
        at=T3,
    )
    assert commit.newly_committed is True

    reopened_loop = AgentLoopRuntime(path)
    assert reopened_loop.snapshot().activation_binding_id == binding.binding_id
    with pytest.raises(
        ConflictingAgentLoopEvidenceError,
        match="action activation binding conflicts with AgentLoop deployment identity",
    ):
        reopened_loop.commit_action(
            relabeled,
            episode=session.environment.episode,
            observation=observation,
            effect_state=ExternalEffectState.PAPER_ONLY,
            at=T3,
        )

    durable_state = json.loads(path.read_text(encoding="utf-8"))
    relabeled_parameters = [[key, value] for key, value in relabeled.parameters]
    durable_decision = durable_state["decisions"][0]
    durable_decision["action_id"] = relabeled.action_id
    durable_decision["parameters"] = relabeled_parameters
    durable_decision["parameters_sha256"] = agent_loop_module._digest(
        relabeled_parameters
    )
    durable_decision["decision_payload_id"] = agent_loop_module._digest(
        {
            "action_type": relabeled.action_type,
            "parameters": relabeled_parameters,
        }
    )
    durable_state["current"]["action_id"] = relabeled.action_id
    durable_state["state_sha256"] = agent_loop_module._digest(
        {
            key: value
            for key, value in durable_state.items()
            if key != "state_sha256"
        }
    )
    path.write_text(
        json.dumps(
            durable_state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        ConflictingAgentLoopEvidenceError,
        match="action activation binding conflicts with AgentLoop deployment identity",
    ):
        AgentLoopRuntime(path)


def test_resolution_and_attribution_preserve_originating_deployment_binding(
    tmp_path,
) -> None:
    training = _identity("training-data", T1)
    deployment = _identity("deployment-data", T2)
    policy = _policy(training)
    scope = _scope()
    binding = _binding(policy, training, deployment, scope)
    path = tmp_path / "resolution-attribution-loop.json"
    (
        semantic_inputs,
        market_store,
        runtime_store,
        require_inputs,
        load_policy,
        validate,
    ) = _resolver_patches(policy, scope)

    with require_inputs, load_policy, validate:
        session = ChampionAgentEpisode.initialize_pristine(
            path,
            object(),
            object(),
            identity=deployment,
            as_of=T2,
            canonical_strategy_id=STRATEGY_ID,
            config_sha256=CONFIG_SHA,
            episode_key="resolution-attribution",
            admissible_actions=frozenset({"WAIT"}),
            loop_id="resolution-attribution-loop",
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

    observation = Observation(
        environment_id=deployment.environment_id,
        observed_at=T2,
        available_at=T2,
        evidence=(("market_state", "deployment-causal-snapshot"),),
    )
    _advance_to_decision(session, observation)
    action = session.decide(observation, decision_at=T3)
    session.agent_loop.commit_action(
        action,
        episode=session.environment.episode,
        observation=observation,
        effect_state=ExternalEffectState.PAPER_ONLY,
        at=T3,
    )

    outcome = Outcome(
        environment_id=deployment.environment_id,
        action_id=action.action_id,
        revealed_at=T4,
        truth=EvidenceTruth.OBSERVED,
        evidence=(("settlement", "paper-result"),),
    )
    reward = RewardEvidence(
        environment_id=deployment.environment_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal("0.25"),
        available_at=T4,
        truth=EvidenceTruth.OBSERVED,
        evidence=(("reward_rule", "paper-economic-reward-v1"),),
    )
    transition = session.environment.resolve(
        action.action_id,
        outcome=outcome,
        reward=reward,
        resolved_at=T4,
    )
    session.agent_loop.record_resolution(
        transition,
        outcome=outcome,
        reward=reward,
        at=T4,
    )
    session.agent_loop.advance(expected=AgentLoopPhase.EVALUATE, at=T4)

    attribution = OutcomeAttribution(
        environment_id=deployment.environment_id,
        episode_id=session.environment.episode.episode_id,
        transition_id=transition.transition_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward_id=reward.reward_id,
        reward_value=reward.reward,
        truth=reward.truth,
        simulation_model_id=None,
        attributed_at=T5,
        findings=(
            AttributionFinding(
                component=AttributionComponent.IDENTITY,
                status=AttributionStatus.SUPPORTED,
                evidence_grade=AttributionEvidenceGrade.FACTUAL_MECHANICAL,
                evidence_sha256=ATTRIBUTION_EVIDENCE_SHA,
                evidence_available_at=T4,
                contribution=Decimal("0.00"),
                reason_code="ORIGINATING_DEPLOYMENT_ACTION_ID",
            ),
        ),
    )
    session.agent_loop.record_attribution(attribution, at=T5)

    reopened = AgentLoopRuntime(path)
    snapshot = reopened.snapshot()
    durable_state = json.loads(path.read_text(encoding="utf-8"))
    decision = durable_state["decisions"][0]
    resolution = durable_state["resolutions"][0]
    stored_attribution = durable_state["attributions"][0]

    assert snapshot.activation_binding_id == binding.binding_id
    assert ["activation_binding_id", binding.binding_id] in decision["parameters"]
    assert decision["action_id"] == action.action_id
    assert resolution["action_id"] == decision["action_id"]
    assert stored_attribution["action_id"] == resolution["action_id"]
    assert stored_attribution["transition_id"] == resolution["transition_id"]


def test_legacy_agent_loop_schema_cannot_claim_deployment_binding(tmp_path) -> None:
    training = _identity("training-data", T1)
    deployment = _identity("deployment-data", T2)
    policy = _policy(training)
    scope = _scope()
    binding = _binding(policy, training, deployment, scope)
    path = tmp_path / "legacy-schema-binding-loop.json"
    (
        semantic_inputs,
        market_store,
        runtime_store,
        require_inputs,
        load_policy,
        validate,
    ) = _resolver_patches(policy, scope)

    with require_inputs, load_policy, validate:
        ChampionAgentEpisode.initialize_pristine(
            path,
            object(),
            object(),
            identity=deployment,
            as_of=T2,
            canonical_strategy_id=STRATEGY_ID,
            config_sha256=CONFIG_SHA,
            episode_key="legacy-schema-binding",
            admissible_actions=frozenset({"WAIT"}),
            loop_id="legacy-schema-binding-loop",
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

    durable_state = json.loads(path.read_text(encoding="utf-8"))
    durable_state["schema_version"] = 2
    durable_state.pop("checkpoint_history")
    durable_state["state_sha256"] = agent_loop_module._digest(
        {
            key: value
            for key, value in durable_state.items()
            if key != "state_sha256"
        }
    )
    path.write_text(
        json.dumps(
            durable_state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        AgentLoopError,
        match="legacy AgentLoop cannot carry deployment activation binding",
    ):
        AgentLoopRuntime(path)


def test_legacy_episode_cannot_mint_reserved_activation_binding_parameter(tmp_path) -> None:
    identity = _identity("legacy-data", T1)
    policy = _policy(identity)
    path = tmp_path / "legacy-binding-mint-loop.json"

    with patch(
        "autosport.champion_agent_episode.load_champion_policy",
        return_value=policy,
    ):
        session = ChampionAgentEpisode.initialize_pristine(
            path,
            object(),
            object(),
            identity=identity,
            as_of=T1,
            canonical_strategy_id=STRATEGY_ID,
            config_sha256=CONFIG_SHA,
            episode_key="legacy-binding-mint",
            admissible_actions=frozenset({"WAIT"}),
            loop_id="legacy-binding-mint-loop",
            economic_goal_fingerprint=GOAL_SHA,
            risk_fingerprint=RISK_SHA,
            source_sha256=SOURCE_SHA,
            at=T1,
        )

    observation = Observation(
        environment_id=identity.environment_id,
        observed_at=T1,
        available_at=T1,
        evidence=(("market_state", "legacy-causal-snapshot"),),
    )
    _advance_to_decision(session, observation)

    with pytest.raises(
        ChampionAgentEpisodeError,
        match="caller cannot override activation_binding_id",
    ):
        session.decide(
            observation,
            decision_at=T3,
            parameters=(("activation_binding_id", "0" * 64),),
        )


def test_resume_rejects_conflicting_caller_activation_authority(tmp_path) -> None:
    training = _identity("training-data", T1)
    deployment = _identity("deployment-data", T2)
    policy = _policy(training)
    scope = _scope()
    binding = _binding(policy, training, deployment, scope)
    conflicting = replace(binding, promotion_decision_id="promotion-v2")
    path = tmp_path / "conflicting-resume-loop.json"
    (
        semantic_inputs,
        market_store,
        runtime_store,
        require_inputs,
        load_policy,
        validate,
    ) = _resolver_patches(policy, scope)

    with require_inputs, load_policy, validate:
        session = ChampionAgentEpisode.initialize_pristine(
            path,
            object(),
            object(),
            identity=deployment,
            as_of=T2,
            canonical_strategy_id=STRATEGY_ID,
            config_sha256=CONFIG_SHA,
            episode_key="conflicting-resume",
            admissible_actions=frozenset({"WAIT"}),
            loop_id="conflicting-resume-loop",
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

    checkpoint = session.environment.checkpoint()
    (
        semantic_inputs,
        market_store,
        runtime_store,
        require_inputs,
        load_policy,
        validate,
    ) = _resolver_patches(policy, scope)
    with require_inputs, load_policy, validate:
        with pytest.raises(
            ChampionAgentEpisodeError,
            match="caller deployment authority conflicts with durable record",
        ):
            ChampionAgentEpisode.resume(
                path,
                object(),
                object(),
                identity=deployment,
                checkpoint=checkpoint,
                as_of=T3,
                canonical_strategy_id=STRATEGY_ID,
                config_sha256=CONFIG_SHA,
                episode_key="conflicting-resume",
                admissible_actions=frozenset({"WAIT"}),
                training_identity=training,
                deployment_scope=scope,
                activation_binding=conflicting,
                semantic_inputs=semantic_inputs,
                market_store=market_store,
                runtime_authority_store=runtime_store,
            )

def test_later_episode_and_abstention_reuse_durable_activation_generation(
    tmp_path,
) -> None:
    training = _identity("training-data", T1)
    deployment = _identity("deployment-data", T2)
    policy = _policy(training)
    scope = _scope()
    binding = _binding(policy, training, deployment, scope)
    path = tmp_path / "later-episode-abstention-loop.json"
    (
        semantic_inputs,
        market_store,
        runtime_store,
        require_inputs,
        load_policy,
        validate,
    ) = _resolver_patches(policy, scope)

    with require_inputs, load_policy as resolved_policy, validate:
        session = ChampionAgentEpisode.initialize_pristine(
            path,
            object(),
            object(),
            identity=deployment,
            as_of=T3,
            canonical_strategy_id=STRATEGY_ID,
            config_sha256=CONFIG_SHA,
            episode_key="later-deployment-episode",
            admissible_actions=frozenset({"WAIT"}),
            loop_id="later-deployment-episode-loop",
            economic_goal_fingerprint=GOAL_SHA,
            risk_fingerprint=RISK_SHA,
            source_sha256=SOURCE_SHA,
            at=T3,
            training_identity=training,
            activation_binding=binding,
            semantic_inputs=semantic_inputs,
            market_store=market_store,
            runtime_authority_store=runtime_store,
        )

    assert resolved_policy.call_args.kwargs["as_of"] == binding.activation_at
    assert session.agent_loop.snapshot().activation_binding_id == binding.binding_id

    observation = Observation(
        environment_id=deployment.environment_id,
        observed_at=T2,
        available_at=T2,
        evidence=(("market_state", "later-episode-causal-snapshot"),),
    )
    abstention = PaperAbstentionLearningRuntime(
        environment=session.environment,
        agent_loop=session.agent_loop,
    )
    action = abstention.begin_abstention(
        observation=observation,
        action_type="WAIT",
        decision_at=T3,
        parameters=(("operator_note", "bounded"),),
        at=T3,
    )

    assert ("activation_binding_id", binding.binding_id) in action.parameters
    assert ("operator_note", "bounded") in action.parameters
    assert session.agent_loop.snapshot().action_id == action.action_id



def test_canonical_resolver_boundary_rejects_subclass_capability_handles() -> None:
    resolution = SemanticResolutionInput(
        market_event_dedupe_key="canonical-event",
        feature_set_id="canonical-feature",
        runtime_authority_id="1" * 64,
    )
    inputs = CrossSessionSemanticInputs(
        training=resolution,
        deployment=resolution,
    )
    canonical_market = object.__new__(SQLiteMarketStore)
    canonical_runtime = object.__new__(DeploymentRuntimeAuthorityStore)

    class HostileInputs(CrossSessionSemanticInputs):
        pass

    class HostileResolutionInput(SemanticResolutionInput):
        pass

    class HostileMarketStore(SQLiteMarketStore):
        def events(self):
            raise AssertionError("hostile market-store dispatch executed")

    class HostileRuntimeStore(DeploymentRuntimeAuthorityStore):
        def get(self, _runtime_authority_id):
            raise AssertionError("hostile runtime-store dispatch executed")

    hostile_resolution = HostileResolutionInput(
        market_event_dedupe_key="canonical-event",
        feature_set_id="canonical-feature",
        runtime_authority_id="1" * 64,
    )
    nested_hostile_inputs = CrossSessionSemanticInputs(
        training=hostile_resolution,
        deployment=resolution,
    )

    cases = (
        (
            object.__new__(HostileInputs),
            canonical_market,
            canonical_runtime,
        ),
        (
            nested_hostile_inputs,
            canonical_market,
            canonical_runtime,
        ),
        (
            inputs,
            object.__new__(HostileMarketStore),
            canonical_runtime,
        ),
        (
            inputs,
            canonical_market,
            object.__new__(HostileRuntimeStore),
        ),
    )
    for semantic_inputs, market_store, runtime_store in cases:
        with pytest.raises(
            ChampionAgentEpisodeError,
            match="canonical semantic resolver inputs",
        ):
            champion_episode_module._require_canonical_inputs(
                semantic_inputs=semantic_inputs,
                market_store=market_store,
                runtime_authority_store=runtime_store,
            )
